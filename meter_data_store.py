"""Storage and 24-hour query helpers for live meter data.

The current Railway prototype still works without a database. When
``DATABASE_URL`` is set, this module writes analyzer output to Postgres and
serves bounded history queries from it. Without ``DATABASE_URL``, API queries
fall back to the existing JSONL debug files where possible.
"""

from __future__ import annotations

import gzip
import json
import math
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


SERIES_DEFAULT_MAX_POINTS = 24 * 1800
SERIES_MAX_POINTS_CAP = 24 * 1800

SERIES_FIELDS = [
    "raw_fs_mps",
    "raw_fr_m3h",
    "temperature_c",
    "ots_temp_c",
    "zero_estimate_fs",
    "corrected_fs_mps",
    "displayed_gpm",
    "phantom_probability",
    "event_probability",
    "zero_probability",
]

SERIES_TEXT_FIELDS = [
    "state_name",
    "quality_status",
]


def parse_timestamp(raw: Any) -> datetime | None:
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    text = str(raw).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def iso_timestamp(raw: Any) -> str | None:
    parsed = parse_timestamp(raw)
    if parsed is None:
        return None
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def finite_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def first_finite(*values: Any) -> float | None:
    for value in values:
        number = finite_number(value)
        if number is not None:
            return number
    return None


def parse_range_seconds(raw_range: str | None, default_seconds: int = 24 * 60 * 60) -> int:
    if not raw_range:
        return default_seconds
    text = str(raw_range).strip().lower()
    if not text:
        return default_seconds
    suffix = text[-1]
    try:
        value = float(text[:-1] if suffix in {"m", "h", "d"} else text)
    except ValueError:
        return default_seconds
    if suffix == "m":
        return max(60, int(value * 60))
    if suffix == "h":
        return max(60, int(value * 60 * 60))
    if suffix == "d":
        return max(60, int(value * 24 * 60 * 60))
    return max(60, int(value))


def frame_from_record(record: dict[str, Any], waveform_id: int | None = None) -> dict[str, Any]:
    health = record.get("flow_meter_health") if isinstance(record.get("flow_meter_health"), dict) else {}
    return {
        "serial": record.get("serial"),
        "timestamp": record.get("timestamp") or record.get("server_timestamp"),
        "raw_fs_mps": first_finite(
            record.get("raw_fs_mps"),
            record.get("pub_fs_mps"),
            record.get("raw_flow_speed_mps"),
            record.get("zero_corr_fs_mps"),
            record.get("corr_fs_mps"),
        ),
        "raw_fr_m3h": first_finite(
            record.get("raw_fr_m3h"),
            record.get("pub_fr_m3h"),
            record.get("raw_flow_rate_m3h"),
            record.get("zero_corr_fr_m3h"),
            record.get("corr_fr_m3h"),
        ),
        "temperature_c": first_finite(record.get("temperature_c"), record.get("onboard_temperature_c"), record.get("ots")),
        "ots_temp_c": first_finite(record.get("ots_temp_c"), record.get("ots"), record.get("onboard_temperature_c")),
        "zero_estimate_fs": finite_number(record.get("zero_estimate_fs")),
        "corrected_fs_mps": first_finite(record.get("corrected_fs_mps"), record.get("corr_fs_mps"), record.get("zero_corr_fs_mps")),
        "displayed_gpm": finite_number(record.get("displayed_gpm")),
        "phantom_probability": finite_number(record.get("phantom_probability")),
        "event_probability": finite_number(record.get("event_probability")),
        "zero_probability": finite_number(record.get("zero_probability")),
        "state_name": record.get("state_name") or record.get("pipe_state") or record.get("condition"),
        "quality_status": record.get("quality_status") or record.get("sq_label") or record.get("label") or health.get("label"),
        "waveform_id": waveform_id,
        "cnn_analysis": record.get("cnn_analysis"),
        "record_json": record,
    }


def waveform_summary(serial: str | None, timestamp: Any, samples: list[float]) -> dict[str, Any]:
    baseline_count = min(80, len(samples))
    baseline_values = samples[:baseline_count] or samples[:]
    baseline = sorted(baseline_values)[len(baseline_values) // 2] if baseline_values else None
    peak_index = None
    peak_abs = None
    if baseline is not None and samples:
        values = [abs(value - baseline) for value in samples]
        peak_abs = max(values)
        peak_index = values.index(peak_abs)
    return {
        "serial": serial,
        "timestamp": timestamp,
        "samples_compressed": gzip.compress(json.dumps(samples, separators=(",", ":")).encode("utf-8")),
        "sample_count": len(samples),
        "baseline": baseline,
        "gate_start": 120 if len(samples) > 120 else None,
        "gate_end": min(520, len(samples)) if len(samples) > 120 else None,
        "peak_index": peak_index,
        "peak_abs": peak_abs,
    }


def aggregate_series(rows: list[dict[str, Any]], max_points: int) -> list[dict[str, Any]]:
    if not rows:
        return []
    max_points = max(1, min(int(max_points or SERIES_DEFAULT_MAX_POINTS), SERIES_MAX_POINTS_CAP))
    if len(rows) <= max_points:
        return [series_point_from_rows([row]) for row in rows]
    buckets: list[dict[str, Any]] = []
    for bucket in range(max_points):
        start = math.floor(bucket * len(rows) / max_points)
        end = max(start + 1, math.floor((bucket + 1) * len(rows) / max_points))
        buckets.append(series_point_from_rows(rows[start:end]))
    return buckets


def series_point_from_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    latest = rows[-1]
    point: dict[str, Any] = {
        "timestamp": iso_timestamp(latest.get("timestamp")),
        "count": len(rows),
    }
    for field in SERIES_FIELDS:
        values = [finite_number(row.get(field)) for row in rows]
        values = [value for value in values if value is not None]
        latest_value = finite_number(latest.get(field))
        point[field] = {
            "avg": sum(values) / len(values) if values else None,
            "min": min(values) if values else None,
            "max": max(values) if values else None,
            "latest": latest_value,
        }
    for field in SERIES_TEXT_FIELDS:
        point[field] = latest.get(field)
    return point


def iter_jsonl_records(path: Path, serial: str | None = None) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8", errors="ignore") as handle:
        for line in handle:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue
            if serial and record.get("serial") != serial:
                continue
            records.append(record)
    return records


def iter_jsonl_records_reverse(path: Path, serial: str | None = None):
    if not path.exists():
        return
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        position = handle.tell()
        buffer = b""
        while position > 0:
            read_size = min(1024 * 1024, position)
            position -= read_size
            handle.seek(position)
            chunk = handle.read(read_size)
            parts = (chunk + buffer).split(b"\n")
            buffer = parts[0]
            for raw_line in reversed(parts[1:]):
                if not raw_line:
                    continue
                try:
                    record = json.loads(raw_line.decode("utf-8", errors="ignore"))
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict):
                    continue
                if serial and record.get("serial") != serial:
                    continue
                yield record
        if buffer:
            try:
                record = json.loads(buffer.decode("utf-8", errors="ignore"))
            except json.JSONDecodeError:
                return
            if isinstance(record, dict) and (not serial or record.get("serial") == serial):
                yield record


def jsonl_frame_rows(path: Path, serial: str, range_seconds: int) -> list[dict[str, Any]]:
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=range_seconds)
    rows = []
    for record in iter_jsonl_records_reverse(path, serial):
        ts = parse_timestamp(record.get("timestamp"))
        if ts is None:
            continue
        if ts < cutoff:
            if rows:
                break
            continue
        frame = frame_from_record(record)
        if frame["serial"] and frame["timestamp"]:
            rows.append(frame)
    rows.sort(key=lambda row: parse_timestamp(row["timestamp"]) or datetime.min.replace(tzinfo=timezone.utc))
    return rows


class MeterDataStore:
    def enqueue_frame(
        self,
        record: dict[str, Any],
        *,
        samples: list[float] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        return

    def enqueue_event(self, event: dict[str, Any], context: dict[str, Any] | None = None) -> None:
        return

    def enqueue_cnn_analysis(self, serial: str | None, timestamp: Any, cnn_analysis: dict[str, Any]) -> None:
        return

    def flush(self, *, force: bool = False) -> None:
        return

    def close(self) -> None:
        self.flush(force=True)


class NullMeterDataStore(MeterDataStore):
    pass


class PostgresMeterDataStore(MeterDataStore):
    def __init__(self, database_url: str, *, batch_size: int = 100, flush_ms: int = 1000) -> None:
        try:
            import psycopg
            from psycopg.types.json import Json
        except ImportError as exc:  # pragma: no cover - depends on deployed optional dependency
            raise RuntimeError("DATABASE_URL requires psycopg; install psycopg[binary].") from exc
        self.psycopg = psycopg
        self.Json = Json
        self.conn = psycopg.connect(database_url)
        self.conn.autocommit = True
        self.batch_size = max(1, batch_size)
        self.flush_s = max(0.1, flush_ms / 1000.0)
        self.frame_buffer: list[tuple[dict[str, Any], list[float] | None, dict[str, Any] | None]] = []
        self.event_buffer: list[tuple[dict[str, Any], dict[str, Any] | None]] = []
        self.cnn_update_buffer: list[tuple[str | None, Any, dict[str, Any]]] = []
        self.last_flush_at = time.time()
        self.ensure_schema()

    def ensure_schema(self) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS waveforms (
                    id BIGSERIAL PRIMARY KEY,
                    serial TEXT NOT NULL,
                    timestamp TIMESTAMPTZ NOT NULL,
                    samples_compressed BYTEA NOT NULL,
                    sample_count INTEGER,
                    baseline DOUBLE PRECISION,
                    gate_start INTEGER,
                    gate_end INTEGER,
                    peak_index INTEGER,
                    peak_abs DOUBLE PRECISION,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS meter_frames (
                    id BIGSERIAL PRIMARY KEY,
                    serial TEXT NOT NULL,
                    timestamp TIMESTAMPTZ NOT NULL,
                    raw_fs_mps DOUBLE PRECISION,
                    raw_fr_m3h DOUBLE PRECISION,
                    temperature_c DOUBLE PRECISION,
                    ots_temp_c DOUBLE PRECISION,
                    zero_estimate_fs DOUBLE PRECISION,
                    corrected_fs_mps DOUBLE PRECISION,
                    displayed_gpm DOUBLE PRECISION,
                    phantom_probability DOUBLE PRECISION,
                    event_probability DOUBLE PRECISION,
                    zero_probability DOUBLE PRECISION,
                    state_name TEXT,
                    quality_status TEXT,
                    waveform_id BIGINT REFERENCES waveforms(id),
                    cnn_analysis JSONB,
                    record_json JSONB,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS meter_events (
                    id BIGSERIAL PRIMARY KEY,
                    serial TEXT NOT NULL,
                    start_time TIMESTAMPTZ NOT NULL,
                    end_time TIMESTAMPTZ,
                    kind TEXT,
                    severity TEXT,
                    message TEXT,
                    event_json JSONB,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            cur.execute("CREATE INDEX IF NOT EXISTS idx_meter_frames_serial_timestamp ON meter_frames(serial, timestamp)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_meter_events_serial_start_time ON meter_events(serial, start_time)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_waveforms_serial_timestamp ON waveforms(serial, timestamp)")

    def enqueue_frame(
        self,
        record: dict[str, Any],
        *,
        samples: list[float] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.frame_buffer.append((record, samples, metadata))
        self.flush()

    def enqueue_event(self, event: dict[str, Any], context: dict[str, Any] | None = None) -> None:
        self.event_buffer.append((event, context))
        self.flush()

    def enqueue_cnn_analysis(self, serial: str | None, timestamp: Any, cnn_analysis: dict[str, Any]) -> None:
        self.cnn_update_buffer.append((serial, timestamp, cnn_analysis))
        self.flush()

    def flush(self, *, force: bool = False) -> None:
        due = time.time() - self.last_flush_at >= self.flush_s
        full = len(self.frame_buffer) + len(self.event_buffer) + len(self.cnn_update_buffer) >= self.batch_size
        if not force and not due and not full:
            return
        if not self.frame_buffer and not self.event_buffer and not self.cnn_update_buffer:
            return
        frame_batch = self.frame_buffer
        event_batch = self.event_buffer
        cnn_update_batch = self.cnn_update_buffer
        self.frame_buffer = []
        self.event_buffer = []
        self.cnn_update_buffer = []
        self.last_flush_at = time.time()
        with self.conn.cursor() as cur:
            for record, samples, metadata in frame_batch:
                serial = record.get("serial") or (metadata or {}).get("serial")
                timestamp = record.get("timestamp") or (metadata or {}).get("timestamp")
                waveform_id = None
                if samples:
                    summary = waveform_summary(serial, timestamp, samples)
                    cur.execute(
                        """
                        INSERT INTO waveforms (
                            serial, timestamp, samples_compressed, sample_count,
                            baseline, gate_start, gate_end, peak_index, peak_abs
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                        RETURNING id
                        """,
                        (
                            summary["serial"],
                            summary["timestamp"],
                            summary["samples_compressed"],
                            summary["sample_count"],
                            summary["baseline"],
                            summary["gate_start"],
                            summary["gate_end"],
                            summary["peak_index"],
                            summary["peak_abs"],
                        ),
                    )
                    waveform_id = cur.fetchone()[0]
                frame = frame_from_record(record, waveform_id)
                if not frame["serial"] or not frame["timestamp"]:
                    continue
                cur.execute(
                    """
                    INSERT INTO meter_frames (
                        serial, timestamp, raw_fs_mps, raw_fr_m3h, temperature_c,
                        ots_temp_c, zero_estimate_fs, corrected_fs_mps,
                        displayed_gpm, phantom_probability, event_probability,
                        zero_probability, state_name, quality_status, waveform_id,
                        cnn_analysis, record_json
                    )
                    VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s
                    )
                    """,
                    (
                        frame["serial"],
                        frame["timestamp"],
                        frame["raw_fs_mps"],
                        frame["raw_fr_m3h"],
                        frame["temperature_c"],
                        frame["ots_temp_c"],
                        frame["zero_estimate_fs"],
                        frame["corrected_fs_mps"],
                        frame["displayed_gpm"],
                        frame["phantom_probability"],
                        frame["event_probability"],
                        frame["zero_probability"],
                        frame["state_name"],
                        frame["quality_status"],
                        frame["waveform_id"],
                        self.Json(frame["cnn_analysis"]) if frame["cnn_analysis"] is not None else None,
                        self.Json(frame["record_json"]),
                    ),
                )
            for event, context in event_batch:
                serial = event.get("serial") or (context or {}).get("serial")
                timestamp = event.get("timestamp") or event.get("start_time") or (context or {}).get("timestamp")
                if not serial or not timestamp:
                    continue
                cur.execute(
                    """
                    INSERT INTO meter_events (
                        serial, start_time, end_time, kind, severity, message, event_json
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        serial,
                        timestamp,
                        event.get("end_time"),
                        event.get("kind") or event.get("event"),
                        event.get("severity"),
                        event.get("message") or event.get("reason"),
                        self.Json(event),
                    ),
                )
            for serial, timestamp, cnn_analysis in cnn_update_batch:
                if not serial or not timestamp:
                    continue
                cur.execute(
                    """
                    UPDATE meter_frames
                    SET
                        cnn_analysis = %s,
                        record_json = jsonb_set(
                            COALESCE(record_json, '{}'::jsonb),
                            '{cnn_analysis}',
                            %s::jsonb,
                            true
                        )
                    WHERE id = (
                        SELECT id
                        FROM meter_frames
                        WHERE serial = %s AND timestamp = %s
                        ORDER BY id DESC
                        LIMIT 1
                    )
                    """,
                    (
                        self.Json(cnn_analysis),
                        self.Json(cnn_analysis),
                        serial,
                        timestamp,
                    ),
                )

    def close(self) -> None:
        try:
            self.flush(force=True)
        finally:
            self.conn.close()


@dataclass
class MeterDataQuery:
    database_url: str | None = None
    log_path: Path | None = None
    events_path: Path | None = None

    def _connect(self):
        if not self.database_url:
            return None
        try:
            import psycopg
        except ImportError:
            return None
        return psycopg.connect(self.database_url)

    def latest(self, serial: str) -> dict[str, Any] | None:
        conn = self._connect()
        if conn is not None:
            try:
                with conn, conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT record_json FROM meter_frames
                        WHERE serial = %s
                        ORDER BY timestamp DESC
                        LIMIT 1
                        """,
                        (serial,),
                    )
                    row = cur.fetchone()
                    return row[0] if row else None
            finally:
                conn.close()
        if self.log_path is None:
            return None
        for record in iter_jsonl_records_reverse(self.log_path, serial):
            return record
        return None

    def series(self, serial: str, range_seconds: int, max_points: int) -> list[dict[str, Any]]:
        conn = self._connect()
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=range_seconds)
        rows: list[dict[str, Any]] = []
        if conn is not None:
            try:
                with conn, conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT
                            serial, timestamp, raw_fs_mps, raw_fr_m3h,
                            temperature_c, ots_temp_c, zero_estimate_fs,
                            corrected_fs_mps, displayed_gpm,
                            phantom_probability, event_probability,
                            zero_probability, state_name, quality_status
                        FROM meter_frames
                        WHERE serial = %s AND timestamp >= %s
                        ORDER BY timestamp
                        """,
                        (serial, cutoff),
                    )
                    for row in cur.fetchall():
                        rows.append({
                            "serial": row[0],
                            "timestamp": row[1],
                            "raw_fs_mps": row[2],
                            "raw_fr_m3h": row[3],
                            "temperature_c": row[4],
                            "ots_temp_c": row[5],
                            "zero_estimate_fs": row[6],
                            "corrected_fs_mps": row[7],
                            "displayed_gpm": row[8],
                            "phantom_probability": row[9],
                            "event_probability": row[10],
                            "zero_probability": row[11],
                            "state_name": row[12],
                            "quality_status": row[13],
                        })
            finally:
                conn.close()
        elif self.log_path is not None:
            rows = jsonl_frame_rows(self.log_path, serial, range_seconds)
        return aggregate_series(rows, max_points)

    def events(self, serial: str, range_seconds: int) -> list[dict[str, Any]]:
        conn = self._connect()
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=range_seconds)
        if conn is not None:
            try:
                with conn, conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT event_json FROM meter_events
                        WHERE serial = %s AND start_time >= %s
                        ORDER BY start_time DESC
                        LIMIT 500
                        """,
                        (serial, cutoff),
                    )
                    return [row[0] for row in cur.fetchall()]
            finally:
                conn.close()
        if self.events_path is None:
            return []
        out = []
        for event in iter_jsonl_records(self.events_path, serial):
            ts = parse_timestamp(event.get("timestamp") or event.get("start_time"))
            if ts is not None and ts >= cutoff:
                out.append(event)
        return out[-500:]

    def waveform(self, waveform_id: int) -> dict[str, Any] | None:
        conn = self._connect()
        if conn is None:
            return None
        try:
            with conn, conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, serial, timestamp, samples_compressed, sample_count,
                           baseline, gate_start, gate_end, peak_index, peak_abs
                    FROM waveforms
                    WHERE id = %s
                    """,
                    (waveform_id,),
                )
                row = cur.fetchone()
                if not row:
                    return None
                samples = json.loads(gzip.decompress(row[3]).decode("utf-8"))
                return {
                    "id": row[0],
                    "serial": row[1],
                    "timestamp": iso_timestamp(row[2]),
                    "samples": samples,
                    "sample_count": row[4],
                    "baseline": row[5],
                    "gate_start": row[6],
                    "gate_end": row[7],
                    "peak_index": row[8],
                    "peak_abs": row[9],
                }
        finally:
            conn.close()


def store_from_env() -> MeterDataStore:
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        return NullMeterDataStore()
    return PostgresMeterDataStore(
        database_url,
        batch_size=int(os.environ.get("DB_WRITE_BATCH_SIZE", "100") or 100),
        flush_ms=int(os.environ.get("DB_WRITE_FLUSH_MS", "1000") or 1000),
    )
