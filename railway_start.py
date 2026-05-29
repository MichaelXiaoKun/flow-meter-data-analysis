#!/usr/bin/env python3
"""Railway entrypoint for the live temperature/zero-flow prototype.

This starts two cooperating processes:

* mqtt_stream_analyzer.py tails meter MQTT topics and writes JSONL/CSV output.
* prototype/live_server.py serves the browser UI plus SSE and waveform CSV.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DEFAULT_SERIAL = "BB8100017587"


def env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def env_path(name: str, default: Path) -> Path:
    raw = os.environ.get(name)
    return Path(raw).expanduser() if raw else default


def data_dir() -> Path:
    raw = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or os.environ.get("DATA_DIR")
    path = Path(raw) if raw else Path("/tmp/flow-meter-data")
    path.mkdir(parents=True, exist_ok=True)
    return path


def topic_env(name: str, default: str, serial: str) -> str:
    return os.environ.get(name, default).format(serial=serial)


def analyzer_command(data: Path, serial: str) -> list[str]:
    log_path = env_path("ANALYSIS_LOG_PATH", data / "live_mqtt_analysis.jsonl")
    events_path = env_path("EVENTS_LOG_PATH", data / "live_mqtt_events.jsonl")
    csv_path = env_path("WAVEFORM_CSV_PATH", data / f"live_{serial}_realtime.csv")
    adapted_model = env_path("ADAPTED_MODEL_PATH", data / "live_adaptive_meter_model.json")
    model_default = adapted_model if adapted_model.exists() else ROOT / "oneclass_meter_model_combined.json"
    model_path = env_path("METER_MODEL_PATH", model_default)

    for path in (log_path, events_path, csv_path, adapted_model):
        path.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable,
        str(ROOT / "mqtt_stream_analyzer.py"),
        "--broker",
        os.environ.get("MQTT_BROKER", "mqtt-prod.bluebot.com"),
        "--port",
        os.environ.get("MQTT_PORT", "1883"),
        "--client-id",
        os.environ.get("MQTT_CLIENT_ID", "railway_temp_drift_{uuid}"),
        "--sig-topic",
        topic_env("MQTT_SIG_TOPIC", "meter/sig/{serial}", serial),
        "--pub-topic",
        topic_env("MQTT_PUB_TOPIC", "meter/pub/{serial}", serial),
        "--processed-topic",
        topic_env("MQTT_PROCESSED_TOPIC", "", serial),
        "--model",
        str(model_path),
        "--adapted-model-out",
        str(adapted_model),
        "--heartbeat-s",
        os.environ.get("ANALYZER_HEARTBEAT_S", "30"),
        "--save-csv",
        str(csv_path),
        "--log-jsonl",
        str(log_path),
        "--events-jsonl",
        str(events_path),
        "--log-mode",
        os.environ.get("ANALYZER_LOG_MODE", "transitions"),
        "--save-every",
        os.environ.get("ANALYZER_SAVE_EVERY", "25"),
    ]

    if env_bool("SELF_TRAIN", True):
        cmd.append("--self-train")
    if env_bool("MQTT_TLS", False):
        cmd.append("--tls")
    if os.environ.get("MQTT_USERNAME"):
        cmd.extend(["--username", os.environ["MQTT_USERNAME"]])
    if os.environ.get("MQTT_PASSWORD"):
        cmd.extend(["--password", os.environ["MQTT_PASSWORD"]])

    cnn_model = env_path("CNN_MODEL_PATH", ROOT / "cnn_autoencoder_model.pt")
    if env_bool("ENABLE_CNN", False) and cnn_model.exists():
        cmd.extend(
            [
                "--cnn-model",
                str(cnn_model),
                "--cnn-device",
                os.environ.get("CNN_DEVICE", "cpu"),
                "--cnn-health-weight",
                os.environ.get("CNN_HEALTH_WEIGHT", "0"),
            ]
        )

    print("Analyzer output:")
    print(f"  log={log_path}")
    print(f"  events={events_path}")
    print(f"  waveform_csv={csv_path}")
    print(f"  model={model_path}")
    print(f"  adapted_model={adapted_model}")
    return cmd


def server_command(data: Path, serial: str) -> list[str]:
    port = os.environ.get("PORT", "8765")
    log_path = env_path("ANALYSIS_LOG_PATH", data / "live_mqtt_analysis.jsonl")
    csv_path = env_path("WAVEFORM_CSV_PATH", data / f"live_{serial}_realtime.csv")
    return [
        sys.executable,
        str(ROOT / "prototype" / "live_server.py"),
        "--host",
        os.environ.get("HOST", "0.0.0.0"),
        "--port",
        port,
        "--log",
        str(log_path),
        "--waveform-csv",
        str(csv_path),
        "--app-token",
        os.environ.get("APP_TOKEN", ""),
    ]


def terminate(children: list[subprocess.Popen[bytes]]) -> None:
    for proc in children:
        if proc.poll() is None:
            proc.terminate()
    deadline = time.time() + 8.0
    for proc in children:
        remaining = max(0.0, deadline - time.time())
        try:
            proc.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            proc.kill()


def main() -> int:
    serial = os.environ.get("METER_SERIAL", DEFAULT_SERIAL)
    data = data_dir()
    children: list[subprocess.Popen[bytes]] = []

    env = os.environ.copy()
    env.setdefault("PYTHONUNBUFFERED", "1")

    print(f"Railway prototype starting serial={serial} data_dir={data}")

    if env_bool("START_MQTT_ANALYZER", True):
        analyzer = subprocess.Popen(analyzer_command(data, serial), cwd=ROOT, env=env)
        children.append(analyzer)
    else:
        print("START_MQTT_ANALYZER is disabled; serving UI/SSE only.")

    server = subprocess.Popen(server_command(data, serial), cwd=ROOT, env=env)
    children.append(server)

    def handle_signal(_signum: int, _frame: object) -> None:
        terminate(children)
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    analyzer_warning_printed = False
    while True:
        server_code = server.poll()
        if server_code is not None:
            terminate(children)
            return int(server_code)

        if len(children) > 1:
            analyzer_code = children[0].poll()
            if analyzer_code is not None and not analyzer_warning_printed:
                print(
                    "MQTT analyzer exited; web UI is still running but live data will not update. "
                    f"exit_code={analyzer_code}",
                    file=sys.stderr,
                )
                analyzer_warning_printed = True
        time.sleep(1.0)


if __name__ == "__main__":
    raise SystemExit(main())
