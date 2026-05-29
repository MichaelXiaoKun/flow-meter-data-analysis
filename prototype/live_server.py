#!/usr/bin/env python3
"""Serve the temperature zero-flow prototype with a local realtime stream.

The browser UI consumes ``/stream`` as Server-Sent Events. This server tails the
analyzer JSONL output produced by ``mqtt_stream_analyzer.py`` and forwards
matching frames to the page.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from collections import deque
from http import HTTPStatus
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse


ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent
MAX_METERS = 10
DEFAULT_WAVEFORM_LIMIT = 240

if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from meter_data_store import (  # noqa: E402
    MeterDataQuery,
    SERIES_DEFAULT_MAX_POINTS,
    SERIES_MAX_POINTS_CAP,
    parse_range_seconds,
)


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


def normalize_serial(raw: object) -> str:
    return "".join(ch for ch in str(raw or "").strip().upper() if ch.isalnum() or ch in {"_", "-"})


def normalize_serials(raw_serials: object, limit: int = MAX_METERS) -> list[str]:
    if not isinstance(raw_serials, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in raw_serials:
        serial = normalize_serial(item)
        if not serial or serial in seen:
            continue
        seen.add(serial)
        out.append(serial)
        if len(out) >= limit:
            break
    return out


def read_serials_file(path: Path) -> list[str]:
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    raw_serials = payload.get("serials", []) if isinstance(payload, dict) else payload
    return normalize_serials(raw_serials)


def write_serials_file(path: Path, serials: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{time.time_ns()}.tmp")
    body = {"serials": serials, "updated_at_ms": int(time.time() * 1000)}
    tmp.write_text(json.dumps(body, separators=(",", ":")) + "\n", encoding="utf-8")
    tmp.replace(path)


def compact_jsonl_by_serial(path: Path, serial: str) -> dict[str, int | str]:
    if not path.exists():
        return {"path": str(path), "kept": 0, "removed": 0, "status": "missing"}
    tmp = path.with_suffix(path.suffix + ".tmp")
    kept = 0
    removed = 0
    with path.open("r", encoding="utf-8", errors="ignore") as src, tmp.open("w", encoding="utf-8") as dst:
        for line in src:
            record = parse_record(line)
            if record is not None and record.get("serial") == serial:
                removed += 1
                continue
            dst.write(line)
            kept += 1
    tmp.replace(path)
    return {"path": str(path), "kept": kept, "removed": removed, "status": "ok"}


def compact_csv_by_serial(path: Path, serial: str) -> dict[str, int | str]:
    if not path.exists():
        return {"path": str(path), "kept": 0, "removed": 0, "status": "missing"}
    tmp = path.with_suffix(path.suffix + ".tmp")
    kept = 0
    removed = 0
    with path.open("r", encoding="utf-8", newline="", errors="ignore") as src, tmp.open("w", encoding="utf-8", newline="") as dst:
        reader = csv.reader(src)
        writer = csv.writer(dst)
        try:
            header = next(reader)
        except StopIteration:
            tmp.replace(path)
            return {"path": str(path), "kept": 0, "removed": 0, "status": "empty"}
        writer.writerow(header)
        serial_idx = header.index("serial") if "serial" in header else -1
        for row in reader:
            if serial_idx >= 0 and serial_idx < len(row) and row[serial_idx] == serial:
                removed += 1
                continue
            writer.writerow(row)
            kept += 1
    tmp.replace(path)
    return {"path": str(path), "kept": kept, "removed": removed, "status": "ok"}


def waveform_csv_tail(path: Path, *, serial: str | None, limit: int) -> bytes:
    if not path.exists():
        return b""
    limit = max(1, min(limit, 2000))
    rows: deque[str] = deque(maxlen=limit)
    with path.open("r", encoding="utf-8", errors="ignore") as handle:
        header = handle.readline()
        if not header:
            return b""
        header_cols = header.rstrip("\r\n").split(",")
        serial_idx = header_cols.index("serial") if "serial" in header_cols else -1
        for line in handle:
            if serial and serial_idx >= 0:
                cols = line.split(",", serial_idx + 2)
                if len(cols) <= serial_idx or cols[serial_idx] != serial:
                    continue
            rows.append(line)
    body = header + "".join(rows)
    return body.encode("utf-8")


class LiveHandler(SimpleHTTPRequestHandler):
    server_version = "ZeroFlowPrototype/0.1"

    def __init__(self, *args, directory: str | None = None, **kwargs) -> None:
        super().__init__(*args, directory=directory or str(ROOT), **kwargs)

    def log_message(self, format: str, *args: object) -> None:
        # http.server writes access logs to stderr by default, which Railway marks as errors.
        sys.stdout.write(f"{self.address_string()} - - [{self.log_date_time_string()}] {format % args}\n")
        sys.stdout.flush()

    def end_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        super().end_headers()

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(HTTPStatus.NO_CONTENT)
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self.send_response(HTTPStatus.FOUND)
            self.send_header("Location", "/temperature_zero_flow_prototype.html")
            self.end_headers()
            return
        if parsed.path == "/health":
            self.health()
            return
        if parsed.path == "/stream":
            if not self.is_authorized(parsed.query):
                self.unauthorized()
                return
            self.stream(parsed.query)
            return
        if parsed.path == "/waveform.csv":
            if not self.is_authorized(parsed.query):
                self.unauthorized()
                return
            self.serve_waveform_csv(parsed.query)
            return
        if parsed.path == "/status":
            if not self.is_authorized(parsed.query):
                self.unauthorized()
                return
            self.status()
            return
        if parsed.path.startswith("/api/"):
            if not self.is_authorized(parsed.query):
                self.unauthorized()
                return
            self.api_get(parsed.path, parsed.query)
            return
        if parsed.path == "/meters":
            if not self.is_authorized(parsed.query):
                self.unauthorized()
                return
            self.meters()
            return
        super().do_GET()

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/meters":
            if not self.is_authorized(parsed.query):
                self.unauthorized()
                return
            self.update_meters()
            return
        if parsed.path == "/clear-data":
            if not self.is_authorized(parsed.query):
                self.unauthorized()
                return
            self.clear_data(parsed.query)
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def health(self) -> None:
        body = b'{"ok":true}\n'
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, payload: dict, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def is_authorized(self, query: str) -> bool:
        expected = getattr(self.server, "app_token", "")  # type: ignore[attr-defined]
        if not expected:
            return True
        params = parse_qs(query)
        supplied = params.get("token", [""])[0]
        if supplied == expected:
            return True
        auth = self.headers.get("Authorization", "")
        if auth == f"Bearer {expected}":
            return True
        return self.headers.get("X-App-Token", "") == expected

    def unauthorized(self) -> None:
        body = b'{"ok":false,"error":"unauthorized"}\n'
        self.send_response(HTTPStatus.UNAUTHORIZED)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def status(self) -> None:
        body = json.dumps(
            {
                "ok": True,
                "log_path": str(self.server.log_path),  # type: ignore[attr-defined]
                "exists": self.server.log_path.exists(),  # type: ignore[attr-defined]
                "waveform_csv_path": str(self.server.waveform_csv_path),  # type: ignore[attr-defined]
                "waveform_csv_exists": self.server.waveform_csv_path.exists(),  # type: ignore[attr-defined]
                "events_log_path": str(self.server.events_log_path),  # type: ignore[attr-defined]
                "events_log_exists": self.server.events_log_path.exists(),  # type: ignore[attr-defined]
                "serials_path": str(self.server.serials_path),  # type: ignore[attr-defined]
                "subscribed_serials": read_serials_file(self.server.serials_path),  # type: ignore[attr-defined]
                "database_enabled": bool(getattr(self.server, "database_url", "")),
            },
            indent=2,
        ).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def api_query(self) -> MeterDataQuery:
        return MeterDataQuery(
            database_url=getattr(self.server, "database_url", ""),  # type: ignore[attr-defined]
            log_path=self.server.log_path,  # type: ignore[attr-defined]
            events_path=self.server.events_log_path,  # type: ignore[attr-defined]
        )

    def api_get(self, path: str, query: str) -> None:
        parts = [part for part in path.strip("/").split("/") if part]
        params = parse_qs(query)
        api = self.api_query()

        if len(parts) == 4 and parts[:2] == ["api", "meters"]:
            serial = normalize_serial(parts[2])
            action = parts[3]
            if not serial:
                self.send_json({"ok": False, "error": "serial required"}, HTTPStatus.BAD_REQUEST)
                return
            if action == "latest":
                self.send_json({"ok": True, "serial": serial, "frame": api.latest(serial)})
                return
            if action == "series":
                range_raw = params.get("range", [os.environ.get("SERIES_DEFAULT_RANGE", "24h")])[0]
                range_seconds = parse_range_seconds(range_raw)
                try:
                    max_points = int(params.get("max_points", [os.environ.get("SERIES_MAX_POINTS", str(SERIES_DEFAULT_MAX_POINTS))])[0])
                except ValueError:
                    max_points = SERIES_DEFAULT_MAX_POINTS
                max_points = max(1, min(max_points, SERIES_MAX_POINTS_CAP))
                points = api.series(serial, range_seconds, max_points)
                self.send_json(
                    {
                        "ok": True,
                        "serial": serial,
                        "range": range_raw,
                        "range_seconds": range_seconds,
                        "max_points": max_points,
                        "points": points,
                    }
                )
                return
            if action == "events":
                range_raw = params.get("range", [os.environ.get("SERIES_DEFAULT_RANGE", "24h")])[0]
                range_seconds = parse_range_seconds(range_raw)
                self.send_json(
                    {
                        "ok": True,
                        "serial": serial,
                        "range": range_raw,
                        "range_seconds": range_seconds,
                        "events": api.events(serial, range_seconds),
                    }
                )
                return

        if len(parts) == 3 and parts[:2] == ["api", "waveforms"]:
            try:
                waveform_id = int(parts[2])
            except ValueError:
                self.send_json({"ok": False, "error": "invalid waveform id"}, HTTPStatus.BAD_REQUEST)
                return
            waveform = api.waveform(waveform_id)
            if waveform is None:
                self.send_json({"ok": False, "error": "waveform not found"}, HTTPStatus.NOT_FOUND)
                return
            self.send_json({"ok": True, "waveform": waveform})
            return

        self.send_json({"ok": False, "error": "api endpoint not found"}, HTTPStatus.NOT_FOUND)

    def meters(self) -> None:
        serials = read_serials_file(self.server.serials_path)  # type: ignore[attr-defined]
        body = json.dumps({"ok": True, "serials": serials}, separators=(",", ":")).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def update_meters(self) -> None:
        length = int(self.headers.get("Content-Length", "0") or "0")
        try:
            raw_body = self.rfile.read(length).decode("utf-8") if length else "{}"
            payload = json.loads(raw_body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            body = b'{"ok":false,"error":"invalid json"}\n'
            self.send_response(HTTPStatus.BAD_REQUEST)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        serials = normalize_serials(payload.get("serials", []) if isinstance(payload, dict) else [])
        write_serials_file(self.server.serials_path, serials)  # type: ignore[attr-defined]
        body = json.dumps({"ok": True, "serials": serials}, separators=(",", ":")).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def clear_data(self, query: str) -> None:
        params = parse_qs(query)
        serial = (params.get("serial", [""])[0] or "").strip()
        if not serial:
            body = b'{"ok":false,"error":"serial required"}\n'
            self.send_response(HTTPStatus.BAD_REQUEST)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        results = {
            "analysis": compact_jsonl_by_serial(self.server.log_path, serial),  # type: ignore[attr-defined]
            "events": compact_jsonl_by_serial(self.server.events_log_path, serial),  # type: ignore[attr-defined]
            "waveform_csv": compact_csv_by_serial(self.server.waveform_csv_path, serial),  # type: ignore[attr-defined]
        }
        removed = sum(int(result.get("removed", 0)) for result in results.values())
        body = json.dumps(
            {
                "ok": True,
                "serial": serial,
                "removed": removed,
                "results": results,
            },
            separators=(",", ":"),
        ).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def serve_waveform_csv(self, query: str) -> None:
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
        params = parse_qs(query)
        serial = normalize_serial(params.get("serial", [""])[0]) or None
        try:
            limit = int(params.get("limit", [str(DEFAULT_WAVEFORM_LIMIT)])[0] or DEFAULT_WAVEFORM_LIMIT)
        except ValueError:
            limit = DEFAULT_WAVEFORM_LIMIT
        body = waveform_csv_tail(path, serial=serial, limit=limit)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def stream(self, query: str) -> None:
        params = parse_qs(query)
        serial = params.get("serial", [""])[0] or None
        backlog = int(params.get("backlog", ["1200"])[0] or 1200)
        poll_s = float(params.get("poll_s", ["0.5"])[0] or 0.5)
        heartbeat_s = float(params.get("heartbeat_s", ["5"])[0] or 5)
        path: Path = self.server.log_path  # type: ignore[attr-defined]

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        def send_event(event: str, payload: dict, *, replay: bool | None = None) -> None:
            out = dict(payload)
            if replay is not None:
                out["_stream_replay"] = replay
                out["_stream_sent_at_ms"] = int(time.time() * 1000)
            data = json.dumps(out, separators=(",", ":"), ensure_ascii=False)
            self.wfile.write(f"event: {event}\n".encode("utf-8"))
            self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
            self.wfile.flush()

        try:
            send_event("status", {"status": "connected", "serial": serial, "log_path": str(path)})
            last_status_at = time.time()
            for record in iter_backlog(path, serial=serial, limit=backlog):
                send_event("frame", record, replay=True)

            offset = path.stat().st_size if path.exists() else 0
            while True:
                now = time.time()
                if now - last_status_at >= heartbeat_s:
                    send_event(
                        "status",
                        {"status": "heartbeat", "serial": serial, "log_path": str(path), "server_time_ms": int(now * 1000)},
                    )
                    last_status_at = now
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
                        send_event("frame", record, replay=False)
                time.sleep(poll_s)
        except (BrokenPipeError, ConnectionResetError):
            return


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--log", type=Path, default=REPO / "mqtt_analysis_log.jsonl")
    parser.add_argument(
        "--events-log",
        type=Path,
        default=REPO / "mqtt_events.jsonl",
        help="JSONL events file cleaned by /clear-data.",
    )
    parser.add_argument(
        "--waveform-csv",
        type=Path,
        default=ROOT / "live_BB8100017587_realtime.csv",
        help="CSV file exposed at /waveform.csv for waveform hover/inspection.",
    )
    parser.add_argument(
        "--serials-json",
        type=Path,
        default=REPO / "live_meter_serials.json",
        help="UI-managed JSON file containing currently subscribed serial numbers.",
    )
    parser.add_argument(
        "--app-token",
        default="",
        help="Optional token required for /stream and /waveform.csv. Use ?token=... in the UI URL.",
    )
    return parser


class LiveHTTPServer(ThreadingHTTPServer):
    def handle_error(self, request: object, client_address: tuple[str, int]) -> None:
        exc_type, _exc, _tb = sys.exc_info()
        if exc_type in {BrokenPipeError, ConnectionResetError}:
            return
        super().handle_error(request, client_address)


def main() -> None:
    args = build_parser().parse_args()
    server = LiveHTTPServer((args.host, args.port), LiveHandler)
    server.log_path = args.log.resolve()  # type: ignore[attr-defined]
    server.events_log_path = args.events_log.resolve()  # type: ignore[attr-defined]
    server.waveform_csv_path = args.waveform_csv.resolve()  # type: ignore[attr-defined]
    server.serials_path = args.serials_json.resolve()  # type: ignore[attr-defined]
    server.app_token = args.app_token  # type: ignore[attr-defined]
    server.database_url = os.environ.get("DATABASE_URL", "").strip()  # type: ignore[attr-defined]
    print(f"Serving prototype: http://{args.host}:{args.port}/temperature_zero_flow_prototype.html")
    print(f"Streaming JSONL:   {server.log_path}")
    print(f"Events JSONL:      {server.events_log_path}")
    print(f"Waveform CSV:      {server.waveform_csv_path}")
    print(f"Meter serials:     {server.serials_path}")
    print(f"Database:          {'enabled' if server.database_url else 'disabled'}")
    print(f"Data auth:         {'enabled' if args.app_token else 'disabled'}")
    server.serve_forever()


if __name__ == "__main__":
    main()
