#!/usr/bin/env python3
"""FastAPI web API for 24-hour meter history.

This app exposes the same API contract currently mirrored by
``prototype/live_server.py``. It is intentionally independent from the static
prototype/SSE server so it can be deployed or tested separately before Railway
traffic is switched over to it.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware

from meter_data_store import MeterDataQuery, parse_range_seconds


DEFAULT_SERIES_MAX_POINTS = 1600


def env_path(name: str, default: Path) -> Path:
    raw = os.environ.get(name, "").strip()
    return Path(raw) if raw else default


def app_token() -> str:
    return os.environ.get("APP_TOKEN", "").strip()


def require_token(
    request: Request,
    authorization: Optional[str] = Header(default=None),
    x_app_token: Optional[str] = Header(default=None),
) -> None:
    expected = app_token()
    if not expected:
        return
    supplied = request.query_params.get("token", "")
    if supplied == expected:
        return
    if authorization == f"Bearer {expected}":
        return
    if x_app_token == expected:
        return
    raise HTTPException(status_code=401, detail="unauthorized")


def query_store() -> MeterDataQuery:
    data_dir = Path(os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or os.environ.get("DATA_DIR") or "/tmp/flow-meter-data")
    return MeterDataQuery(
        database_url=os.environ.get("DATABASE_URL", "").strip(),
        log_path=env_path("ANALYSIS_LOG_PATH", data_dir / "live_mqtt_analysis.jsonl"),
        events_path=env_path("EVENTS_LOG_PATH", data_dir / "live_mqtt_events.jsonl"),
    )


app = FastAPI(
    title="Bluebot Meter History API",
    version="0.1.0",
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-App-Token"],
)


@app.get("/health")
def health() -> dict[str, bool]:
    return {"ok": True}


@app.get("/api/meters/{serial}/latest")
def latest_meter_frame(serial: str, _: None = Depends(require_token)) -> dict[str, Any]:
    normalized = normalize_serial(serial)
    if not normalized:
        raise HTTPException(status_code=400, detail="serial required")
    return {"ok": True, "serial": normalized, "frame": query_store().latest(normalized)}


@app.get("/api/meters/{serial}/series")
def meter_series(
    serial: str,
    range: str = Query(default_factory=lambda: os.environ.get("SERIES_DEFAULT_RANGE", "24h")),
    max_points: int = Query(default_factory=lambda: int(os.environ.get("SERIES_MAX_POINTS", DEFAULT_SERIES_MAX_POINTS))),
    _: None = Depends(require_token),
) -> dict[str, Any]:
    normalized = normalize_serial(serial)
    if not normalized:
        raise HTTPException(status_code=400, detail="serial required")
    max_points = max(1, min(int(max_points or DEFAULT_SERIES_MAX_POINTS), 5000))
    range_seconds = parse_range_seconds(range)
    points = query_store().series(normalized, range_seconds, max_points)
    return {
        "ok": True,
        "serial": normalized,
        "range": range,
        "range_seconds": range_seconds,
        "max_points": max_points,
        "points": points,
    }


@app.get("/api/meters/{serial}/events")
def meter_events(
    serial: str,
    range: str = Query(default_factory=lambda: os.environ.get("SERIES_DEFAULT_RANGE", "24h")),
    _: None = Depends(require_token),
) -> dict[str, Any]:
    normalized = normalize_serial(serial)
    if not normalized:
        raise HTTPException(status_code=400, detail="serial required")
    range_seconds = parse_range_seconds(range)
    return {
        "ok": True,
        "serial": normalized,
        "range": range,
        "range_seconds": range_seconds,
        "events": query_store().events(normalized, range_seconds),
    }


@app.get("/api/waveforms/{waveform_id}")
def waveform_detail(waveform_id: int, _: None = Depends(require_token)) -> dict[str, Any]:
    waveform = query_store().waveform(waveform_id)
    if waveform is None:
        raise HTTPException(status_code=404, detail="waveform not found")
    return {"ok": True, "waveform": waveform}


def normalize_serial(raw: object) -> str:
    return "".join(ch for ch in str(raw or "").strip().upper() if ch.isalnum() or ch in {"_", "-"})
