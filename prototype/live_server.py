#!/usr/bin/env python3
"""Serve the temperature zero-flow prototype with a local realtime stream.

The browser UI consumes ``/stream`` as Server-Sent Events. This server tails the
analyzer JSONL output produced by ``mqtt_stream_analyzer.py`` and forwards
matching frames to the page.
"""

from __future__ import annotations

import argparse
import json
import time
from collections import deque
from http import HTTPStatus
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse


ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent


def iter_backlog(path: Path, *, serial: str | None, limit: int) -> list[dict]:
    if not path.exists() or limit <= 0:
        return []
    rows: deque[dict] = deque(maxlen=limit)
    with path.open("r", encoding="utf-8", errors="ignore") as handle:
        for line in handle:
            record = parse_record(line)
            if record is None:
                continue
            if serial and record.get("serial") != serial:
                continue
            rows.append(record)
    return list(rows)


def parse_record(line: str) -> dict | None:
    line = line.strip()
    if not line:
        return None
    try:
        record = json.loads(line)
    except json.JSONDecodeError:
        return None
    return record if isinstance(record, dict) else None


class LiveHandler(SimpleHTTPRequestHandler):
    server_version = "ZeroFlowPrototype/0.1"

    def __init__(self, *args, directory: str | None = None, **kwargs) -> None:
        super().__init__(*args, directory=directory or str(ROOT), **kwargs)

    def end_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        super().end_headers()

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self.send_response(HTTPStatus.FOUND)
            self.send_header("Location", "/temperature_zero_flow_prototype.html")
            self.end_headers()
            return
        if parsed.path == "/stream":
            self.stream(parsed.query)
            return
        if parsed.path == "/waveform.csv":
            self.serve_waveform_csv()
            return
        if parsed.path == "/status":
            self.status()
            return
        super().do_GET()

    def status(self) -> None:
        body = json.dumps(
            {
                "ok": True,
                "log_path": str(self.server.log_path),  # type: ignore[attr-defined]
                "exists": self.server.log_path.exists(),  # type: ignore[attr-defined]
                "waveform_csv_path": str(self.server.waveform_csv_path),  # type: ignore[attr-defined]
                "waveform_csv_exists": self.server.waveform_csv_path.exists(),  # type: ignore[attr-defined]
            },
            indent=2,
        ).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def serve_waveform_csv(self) -> None:
        path: Path = self.server.waveform_csv_path  # type: ignore[attr-defined]
        if not path.exists():
            body = b""
            self.send_response(HTTPStatus.NOT_FOUND)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        body = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def stream(self, query: str) -> None:
        params = parse_qs(query)
        serial = params.get("serial", [""])[0] or None
        backlog = int(params.get("backlog", ["300"])[0] or 300)
        poll_s = float(params.get("poll_s", ["0.5"])[0] or 0.5)
        path: Path = self.server.log_path  # type: ignore[attr-defined]

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        def send_event(event: str, payload: dict) -> None:
            data = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
            self.wfile.write(f"event: {event}\n".encode("utf-8"))
            self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
            self.wfile.flush()

        try:
            send_event("status", {"status": "connected", "serial": serial, "log_path": str(path)})
            for record in iter_backlog(path, serial=serial, limit=backlog):
                send_event("frame", record)

            offset = path.stat().st_size if path.exists() else 0
            while True:
                if not path.exists():
                    time.sleep(poll_s)
                    continue
                current_size = path.stat().st_size
                if current_size < offset:
                    offset = 0
                with path.open("r", encoding="utf-8", errors="ignore") as handle:
                    handle.seek(offset)
                    while True:
                        line = handle.readline()
                        if not line:
                            break
                        offset = handle.tell()
                        record = parse_record(line)
                        if record is None:
                            continue
                        if serial and record.get("serial") != serial:
                            continue
                        send_event("frame", record)
                time.sleep(poll_s)
        except (BrokenPipeError, ConnectionResetError):
            return


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--log", type=Path, default=REPO / "mqtt_analysis_log.jsonl")
    parser.add_argument(
        "--waveform-csv",
        type=Path,
        default=ROOT / "live_BB8100017587_realtime.csv",
        help="CSV file exposed at /waveform.csv for waveform hover/inspection.",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    server = ThreadingHTTPServer((args.host, args.port), LiveHandler)
    server.log_path = args.log.resolve()  # type: ignore[attr-defined]
    server.waveform_csv_path = args.waveform_csv.resolve()  # type: ignore[attr-defined]
    print(f"Serving prototype: http://{args.host}:{args.port}/temperature_zero_flow_prototype.html")
    print(f"Streaming JSONL:   {server.log_path}")
    print(f"Waveform CSV:      {server.waveform_csv_path}")
    server.serve_forever()


if __name__ == "__main__":
    main()
