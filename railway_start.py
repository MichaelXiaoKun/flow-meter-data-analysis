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


def env_bool(name: str, default: bool) -> bool:
    raw = env_str(name, "")
    if raw is None:
        return default
    if not raw:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def env_str(name: str, default: str = "") -> str:
    raw = os.environ.get(name)
    if raw is None:
        return default
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1]
    return value


def env_path(name: str, default: Path) -> Path:
    raw = env_str(name)
    return Path(raw).expanduser() if raw else default


def data_dir() -> Path:
    raw = env_str("RAILWAY_VOLUME_MOUNT_PATH") or env_str("DATA_DIR")
    path = Path(raw) if raw else Path("/tmp/flow-meter-data")
    path.mkdir(parents=True, exist_ok=True)
    return path


def analyzer_command(data: Path) -> list[str]:
    log_path = env_path("ANALYSIS_LOG_PATH", data / "live_mqtt_analysis.jsonl")
    events_path = env_path("EVENTS_LOG_PATH", data / "live_mqtt_events.jsonl")
    notifications_path = env_path("NOTIFICATIONS_JSONL", data / "live_notifications.jsonl")
    csv_path = env_path("WAVEFORM_CSV_PATH", data / "live_mqtt_waveforms.csv")
    serials_path = env_path("METER_SERIALS_PATH", data / "live_meter_serials.json")
    adapted_model = env_path("ADAPTED_MODEL_PATH", data / "live_adaptive_meter_model.json")
    model_default = adapted_model if adapted_model.exists() else ROOT / "oneclass_meter_model_combined.json"
    model_path = env_path("METER_MODEL_PATH", model_default)

    for path in (log_path, events_path, notifications_path, csv_path, serials_path, adapted_model):
        path.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable,
        str(ROOT / "mqtt_stream_analyzer.py"),
        "--broker",
        env_str("MQTT_BROKER", "mqtt-prod.bluebot.com"),
        "--port",
        env_str("MQTT_PORT", "1883"),
        "--client-id",
        env_str("MQTT_CLIENT_ID", "lens_data_{uuid}"),
        "--sig-topic",
        env_str("MQTT_SIG_TOPIC", "meter/sig/{serial}"),
        "--pub-topic",
        env_str("MQTT_PUB_TOPIC", "meter/pub/{serial}"),
        "--processed-topic",
        env_str("MQTT_PROCESSED_TOPIC", ""),
        "--serials-json",
        str(serials_path),
        "--model",
        str(model_path),
        "--adapted-model-out",
        str(adapted_model),
        "--heartbeat-s",
        env_str("ANALYZER_HEARTBEAT_S", "30"),
        "--save-csv",
        str(csv_path),
        "--log-jsonl",
        str(log_path),
        "--events-jsonl",
        str(events_path),
        "--notifications-jsonl",
        str(notifications_path),
        "--log-mode",
        env_str("ANALYZER_LOG_MODE", "transitions"),
        "--save-every",
        env_str("ANALYZER_SAVE_EVERY", "25"),
    ]

    if env_bool("SELF_TRAIN", True):
        cmd.append("--self-train")
    if env_bool("MQTT_TLS", False):
        cmd.append("--tls")
    if env_bool("EMPTY_STRICT", True):
        cmd.append("--empty-strict")
    mqtt_username = env_str("MQTT_USERNAME")
    mqtt_password = env_str("MQTT_PASSWORD")
    if mqtt_username:
        cmd.extend(["--username", mqtt_username])
    if mqtt_password:
        cmd.extend(["--password", mqtt_password])

    cnn_model = env_path("CNN_MODEL_PATH", ROOT / "cnn_autoencoder_model.pt")
    if env_bool("ENABLE_CNN", False) and cnn_model.exists():
        cmd.extend(
            [
                "--cnn-model",
                str(cnn_model),
                "--cnn-device",
                env_str("CNN_DEVICE", "cpu"),
                "--cnn-health-weight",
                env_str("CNN_HEALTH_WEIGHT", "0"),
            ]
        )

    print("Analyzer output:")
    print(f"  log={log_path}")
    print(f"  events={events_path}")
    print(f"  notifications={notifications_path}")
    print(f"  waveform_csv={csv_path}")
    print(f"  serials={serials_path}")
    print(f"  model={model_path}")
    print(f"  adapted_model={adapted_model}")
    return cmd


def server_command(data: Path) -> list[str]:
    port = env_str("PORT", "8765")
    log_path = env_path("ANALYSIS_LOG_PATH", data / "live_mqtt_analysis.jsonl")
    events_path = env_path("EVENTS_LOG_PATH", data / "live_mqtt_events.jsonl")
    csv_path = env_path("WAVEFORM_CSV_PATH", data / "live_mqtt_waveforms.csv")
    serials_path = env_path("METER_SERIALS_PATH", data / "live_meter_serials.json")
    return [
        sys.executable,
        str(ROOT / "prototype" / "live_server.py"),
        "--host",
        env_str("HOST", "0.0.0.0"),
        "--port",
        port,
        "--log",
        str(log_path),
        "--events-log",
        str(events_path),
        "--waveform-csv",
        str(csv_path),
        "--serials-json",
        str(serials_path),
        "--app-token",
        env_str("APP_TOKEN", ""),
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
    data = data_dir()
    children: list[subprocess.Popen[bytes]] = []

    env = os.environ.copy()
    env.setdefault("PYTHONUNBUFFERED", "1")

    print(f"Railway prototype starting data_dir={data}; meter subscriptions are managed by the UI.")

    if env_bool("START_MQTT_ANALYZER", True):
        analyzer = subprocess.Popen(analyzer_command(data), cwd=ROOT, env=env)
        children.append(analyzer)
    else:
        print("START_MQTT_ANALYZER is disabled; serving UI/SSE only.")

    server = subprocess.Popen(server_command(data), cwd=ROOT, env=env)
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
