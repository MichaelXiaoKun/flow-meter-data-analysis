#!/usr/bin/env python3
"""Daily Bluebot meter health report.

Produces an Oura-style daily summary for each device:

* a machine-readable JSON file (``reports/<serial>/YYYY-MM-DD.json``)
* a human-readable Markdown card (``...YYYY-MM-DD.md``)
* a self-contained HTML dashboard with a 30-day trend
  (``reports/<serial>/index.html``)

Inputs (in order of preference):

1. ``--log-jsonl`` and ``--events-jsonl`` produced by ``mqtt_stream_analyzer.py``
   — this is the recommended source because the records already contain the
   per-frame features, label, pipe_state, sq, etc.
2. ``--csv`` — a GUI-captured CSV (``timestamp,serial,sq,...,s_0,..s_N``). The
   script will compute features on the fly using the same logic as
   ``train_oneclass_meter_model``. Useful for back-filling reports from
   existing captures.

A ``--model`` JSON is required so subscores can be normalized against the
training distribution (``feature_stats.p05/p99`` for each feature).
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import html
import json
import math
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

from flow_meter_health import FLOW_HEALTH_VERSION, band_for as flow_health_band_for
from train_oneclass_meter_model import (
    anomaly_score,
    extract_features,
    label_from_score,
)


# ── Scoring helpers ──────────────────────────────────────────────────────────

# Composite weights — sum to 1.0. Uptime is intentionally NOT in the
# composite because it confuses "did the device run all day" with "were
# the readings any good". We show uptime as a separate, informational
# metric (same way Oura separates sleep score from sleep duration).
SCORE_WEIGHTS = {
    "coupling": 0.30,
    "signal_strength": 0.20,
    "sq_quality": 0.20,
    "stability": 0.15,
    "incident_burden": 0.15,
}

# Score → label bands (matches the four colors in the design doc)
BANDS = [
    (90, "Optimal", "#10b981"),
    (75, "Good", "#3b82f6"),
    (60, "Fair", "#f59e0b"),
    (0, "Pay attention", "#ef4444"),
]


def normalize_to_100(value: float, lo: float, hi: float) -> float:
    """Map ``value`` linearly to 0–100 between ``lo`` and ``hi``, clamped.

    If ``hi <= lo`` (degenerate training distribution), returns 50.
    """
    if hi <= lo:
        return 50.0
    return max(0.0, min(100.0, (value - lo) / (hi - lo) * 100.0))


def z_normalize(value: float, center: float, sigma: float, *, higher_is_better: bool = True) -> float:
    """Oura-style score: at-or-above the training center is 100, each robust
    sigma below loses 25 points, clamped at 0.

    This is what makes "a normal healthy day" actually show as 90+ rather
    than 50 (which is what raw p05–p99 normalization gives at the median).
    """
    if sigma <= 0:
        return 100.0 if value >= center else 0.0
    z = (value - center) / sigma
    if not higher_is_better:
        z = -z
    if z >= 0:
        return 100.0
    return max(0.0, 100.0 + z * 25.0)


def band_for(score: float) -> tuple[str, str]:
    for cutoff, label, color in BANDS:
        if score >= cutoff:
            return label, color
    return BANDS[-1][1], BANDS[-1][2]


def percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * pct / 100.0
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return ordered[lo]
    return ordered[lo] * (hi - pos) + ordered[hi] * (pos - lo)


# ── ISO timestamp helpers ────────────────────────────────────────────────────


def parse_iso(ts: str) -> dt.datetime:
    """Parse the various ISO shapes the analyzer / GUI emit.

    Handles ``Z`` suffix and missing milliseconds. Returns a naive UTC datetime.
    """
    if not isinstance(ts, str):
        raise ValueError("non-string timestamp")
    raw = ts.rstrip("Z")
    # datetime.fromisoformat only accepts microseconds, so trim to 6 digits.
    if "." in raw:
        whole, frac = raw.split(".", 1)
        frac = frac[:6].ljust(6, "0")
        raw = f"{whole}.{frac}"
    return dt.datetime.fromisoformat(raw)


def to_date(ts: str) -> dt.date:
    return parse_iso(ts).date()


def iso_day_bounds(date: dt.date) -> tuple[dt.datetime, dt.datetime]:
    start = dt.datetime(date.year, date.month, date.day)
    end = start + dt.timedelta(days=1)
    return start, end


# ── Record loading: JSONL and CSV ────────────────────────────────────────────


def load_jsonl(path: Path | None) -> list[dict[str, Any]]:
    if path is None or not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def records_from_csv(
    csv_path: Path,
    model: dict[str, Any],
    *,
    serial_filter: str | None,
    date_filter: dt.date | None,
) -> list[dict[str, Any]]:
    """Convert a GUI CSV into the same record shape ``analyze`` produces.

    Computes features + label + pipe_state per row so the aggregation code
    below can treat CSV-backed reports identically to JSONL-backed ones.
    """
    with csv_path.open(newline="") as f:
        reader = csv.reader(f)
        header = next(reader)
        ts_col = header.index("timestamp")
        sr_col = header.index("serial") if "serial" in header else None
        sq_col = header.index("sq") if "sq" in header else None
        lbl_col = header.index("label") if "label" in header else None
        dt_col = header.index("pub_dt_ns") if "pub_dt_ns" in header else None
        sample_cols: list[int] = sorted(
            (int(name[2:]), idx) for idx, name in enumerate(header) if name.startswith("s_")
        )
        sample_cols = [idx for _, idx in sample_cols]

        profile = model["profile"]
        feature_stats = model["feature_stats"]
        thresholds = model["score_thresholds"]

        out: list[dict[str, Any]] = []
        for row in reader:
            ts = row[ts_col]
            serial = row[sr_col] if sr_col is not None else None
            if serial_filter and serial != serial_filter:
                continue
            if date_filter is not None and to_date(ts) != date_filter:
                continue
            try:
                samples = [float(row[i]) for i in sample_cols]
            except (ValueError, IndexError):
                continue
            features = extract_features(samples, profile)
            score, _ = anomaly_score(features, feature_stats)
            label = label_from_score(score, thresholds)
            sq_val: float | None
            try:
                sq_val = float(row[sq_col]) if sq_col is not None and row[sq_col] else None
            except ValueError:
                sq_val = None
            dt_val: int | None = None
            if dt_col is not None and row[dt_col]:
                try:
                    dt_val = int(row[dt_col])
                except ValueError:
                    dt_val = None
            out.append({
                "timestamp": ts,
                "serial": serial,
                "sq": sq_val,
                "sq_label": (row[lbl_col] if lbl_col is not None else None),
                "pub_dt_ns": dt_val,
                "features": features,
                "score": score,
                "label": label,
                # CSV path has no pipe_state — we derive a coarse one from label
                # so the incident counting still works.
                "pipe_state": (
                    "waveform_anomaly" if label == "anomaly"
                    else "signal_quality_suspect" if label == "suspect"
                    else "normal_acoustic_state"
                ),
            })
    return out


# ── Event timeline aggregation ───────────────────────────────────────────────


def condition_minutes_for_day(
    events: list[dict[str, Any]],
    day: dt.date,
    detect_event: str,
    clear_event: str,
) -> float:
    """Total minutes the device was inside the (detect → clear) condition on
    the given day. Handles intervals that start before midnight or end after.
    """
    day_start, day_end = iso_day_bounds(day)
    paired: list[tuple[dt.datetime, dt.datetime]] = []
    open_at: dt.datetime | None = None
    for ev in events:
        ts = ev.get("timestamp")
        if not ts:
            continue
        try:
            when = parse_iso(ts)
        except ValueError:
            continue
        name = ev.get("event")
        if name == detect_event:
            if open_at is None:
                open_at = when
        elif name == clear_event and open_at is not None:
            paired.append((open_at, when))
            open_at = None
    if open_at is not None:
        # Still open at the tail of the event log — treat as continuing.
        paired.append((open_at, day_end))

    minutes = 0.0
    for a, b in paired:
        start = max(a, day_start)
        end = min(b, day_end)
        if end > start:
            minutes += (end - start).total_seconds() / 60.0
    return minutes


# ── Daily aggregation ────────────────────────────────────────────────────────


def aggregate_day(
    records: list[dict[str, Any]],
    events: list[dict[str, Any]],
    base_model: dict[str, Any],
    day: dt.date,
) -> dict[str, Any] | None:
    """Compute one daily report for one serial. Returns ``None`` if there are
    no records for the day."""
    if not records:
        return None

    feature_stats = base_model["feature_stats"]

    # Pull all the per-feature values for the day.
    corr_values = [r["features"]["template_corr"] for r in records]
    snr_values = [r["features"]["snr_db"] for r in records]
    gate_values = [r["features"]["gate_rms_v"] for r in records]
    sq_values = [r["sq"] for r in records if isinstance(r.get("sq"), (int, float))]
    realtime_health_values = [
        float(r["flow_meter_health"]["score"])
        for r in records
        if isinstance(r.get("flow_meter_health"), dict)
        and isinstance(r["flow_meter_health"].get("score"), (int, float))
    ]
    cnn_reconstruction_values = [
        float(r["cnn_analysis"]["reconstruction_mse"])
        for r in records
        if isinstance(r.get("cnn_analysis"), dict)
        and isinstance(r["cnn_analysis"].get("reconstruction_mse"), (int, float))
    ]
    cnn_distance_values = [
        float(r["cnn_analysis"]["nearest_embedding_distance"])
        for r in records
        if isinstance(r.get("cnn_analysis"), dict)
        and isinstance(r["cnn_analysis"].get("nearest_embedding_distance"), (int, float))
    ]

    # Frame cadence — used both for incident accounting and uptime.
    timestamps = sorted(
        parse_iso(r["timestamp"]) for r in records if r.get("timestamp")
    )
    deltas_s = (
        [(timestamps[i + 1] - timestamps[i]).total_seconds() for i in range(len(timestamps) - 1)]
        if len(timestamps) >= 2
        else []
    )
    median_period_s = (
        statistics.median([d for d in deltas_s if d > 0])
        if any(d > 0 for d in deltas_s)
        else 60.0
    )
    if median_period_s <= 0:
        median_period_s = 60.0

    # ── Subscore: Coupling
    # z-score against training center; ≥center → 100, each robust_sigma below → −25.
    corr_stats = feature_stats["template_corr"]
    coupling = z_normalize(
        statistics.mean(corr_values), corr_stats["center"], corr_stats["robust_sigma"]
    )

    # ── Subscore: Signal Strength (SNR + gate_rms, averaged)
    snr_stats = feature_stats["snr_db"]
    gate_stats = feature_stats["gate_rms_v"]
    snr_norm = z_normalize(
        statistics.mean(snr_values), snr_stats["center"], snr_stats["robust_sigma"]
    )
    gate_norm = z_normalize(
        statistics.mean(gate_values), gate_stats["center"], gate_stats["robust_sigma"]
    )
    signal_strength = 0.5 * snr_norm + 0.5 * gate_norm

    # ── Subscore: SQ Quality (device self-report, already 0–100)
    sq_quality = statistics.mean(sq_values) if sq_values else 50.0
    sq_quality = max(0.0, min(100.0, sq_quality))

    # ── Subscore: Stability
    # Compare observed daily std(template_corr) against training robust_sigma.
    # ratio = 1 → matches trained tightness → 100
    # ratio = 5 → 5x more jittery → 50
    # ratio ≥ 9 → 0
    std_corr = statistics.stdev(corr_values) if len(corr_values) >= 2 else 0.0
    robust_sigma = max(corr_stats["robust_sigma"], 1e-9)
    ratio = std_corr / robust_sigma
    stability = max(0.0, min(100.0, 100.0 - (max(ratio, 1.0) - 1.0) * 12.5))

    # ── Subscore: Incident burden
    anomaly_frames = sum(1 for r in records if r.get("label") == "anomaly")
    suspect_frames = sum(1 for r in records if r.get("label") == "suspect")
    empty_pipe_min = condition_minutes_for_day(events, day, "empty_pipe_detected", "pipe_refilled")
    air_bubble_min = condition_minutes_for_day(events, day, "air_bubble_detected", "air_bubble_cleared")
    # Frame-level anomalies that aren't already covered by an open condition:
    anomaly_min = anomaly_frames * median_period_s / 60.0
    suspect_min = suspect_frames * median_period_s / 60.0 * 0.5  # half weight
    observed_min = len(records) * median_period_s / 60.0
    incident_min = min(empty_pipe_min + air_bubble_min + anomaly_min + suspect_min, observed_min)
    incident_burden = max(0.0, 100.0 - (incident_min / max(observed_min, 1.0)) * 100.0)

    # ── Subscore: Uptime
    # Expected = how many frames a 24h day at the observed cadence would have.
    # We cap actual at expected so a device that bursts won't inflate uptime.
    expected_frames = 86400.0 / median_period_s
    uptime = min(100.0, len(records) / expected_frames * 100.0)

    # ── Composite (uptime is separate — see SCORE_WEIGHTS comment)
    subs = {
        "coupling": round(coupling, 1),
        "signal_strength": round(signal_strength, 1),
        "sq_quality": round(sq_quality, 1),
        "stability": round(stability, 1),
        "incident_burden": round(incident_burden, 1),
    }
    health = sum(SCORE_WEIGHTS[k] * v for k, v in subs.items())
    label, color = band_for(health)
    subs["uptime"] = round(uptime, 1)  # surfaced in UI, not weighted in composite

    # ── Highlights — short prose lines for the markdown card / HTML headline
    highlights: list[str] = []
    if empty_pipe_min > 0:
        highlights.append(
            f"{empty_pipe_min:.0f} min of confirmed empty/lost coupling"
        )
    if air_bubble_min > 0:
        highlights.append(
            f"{air_bubble_min:.0f} min of intermittent coupling (air bubble pattern)"
        )
    if anomaly_frames > 0:
        highlights.append(f"{anomaly_frames} anomaly frames")
    if suspect_frames > 0 and not highlights:
        highlights.append(f"{suspect_frames} suspect frames (mild)")
    if uptime < 80:
        highlights.append(
            f"Uptime only {uptime:.0f}% — pub stream thinner than expected"
        )
    if not highlights:
        highlights.append("Clean day — no notable events")

    if realtime_health_values and percentile(realtime_health_values, 5) is not None:
        p05_health = percentile(realtime_health_values, 5)
        if p05_health is not None and p05_health < 60:
            highlights.append(
                f"Realtime flow health dipped to p05={p05_health:.0f}"
            )

    flow_label, flow_color = flow_health_band_for(health)
    flow_meter_health = {
        "version": FLOW_HEALTH_VERSION,
        "score": round(health),
        "label": flow_label,
        "color": flow_color,
        "meaning": "daily_measurement_trust",
        "subscores": subs,
        "weights": SCORE_WEIGHTS,
    }

    return {
        "serial": records[0].get("serial"),
        "date": day.isoformat(),
        "health_score": round(health),
        "label": label,
        "color": color,
        "flow_meter_health": flow_meter_health,
        "subscores": subs,
        "summary": {
            "frames_observed": len(records),
            "expected_frames": round(expected_frames),
            "median_period_s": round(median_period_s, 2),
            "anomaly_frames": anomaly_frames,
            "suspect_frames": suspect_frames,
            "empty_pipe_minutes": round(empty_pipe_min, 1),
            "air_bubble_minutes": round(air_bubble_min, 1),
            "mean_template_corr": round(statistics.mean(corr_values), 5),
            "mean_snr_db": round(statistics.mean(snr_values), 2),
            "mean_gate_rms_v": round(statistics.mean(gate_values), 4),
            "mean_sq": round(statistics.mean(sq_values), 1) if sq_values else None,
            "std_template_corr": round(std_corr, 6),
            "mean_realtime_flow_health": (
                round(statistics.mean(realtime_health_values), 1)
                if realtime_health_values else None
            ),
            "p05_realtime_flow_health": (
                round(percentile(realtime_health_values, 5), 1)
                if realtime_health_values else None
            ),
            "mean_cnn_reconstruction_mse": (
                round(statistics.mean(cnn_reconstruction_values), 6)
                if cnn_reconstruction_values else None
            ),
            "p95_cnn_reconstruction_mse": (
                round(percentile(cnn_reconstruction_values, 95), 6)
                if cnn_reconstruction_values else None
            ),
            "mean_cnn_embedding_distance": (
                round(statistics.mean(cnn_distance_values), 4)
                if cnn_distance_values else None
            ),
            "p95_cnn_embedding_distance": (
                round(percentile(cnn_distance_values, 95), 4)
                if cnn_distance_values else None
            ),
        },
        "highlights": highlights,
    }


def add_trend_deltas(report: dict[str, Any], history: list[dict[str, Any]]) -> None:
    """Compare today's scores to the 7-day median from prior reports."""
    if not history:
        report["trend_vs_7d"] = {}
        return
    recent = history[-7:]
    deltas: dict[str, float] = {}
    deltas["health_score"] = report["health_score"] - statistics.median(
        h["health_score"] for h in recent
    )
    for key in report["subscores"]:
        deltas[key] = report["subscores"][key] - statistics.median(
            h["subscores"][key] for h in recent
        )
    report["trend_vs_7d"] = {k: round(v, 1) for k, v in deltas.items()}


def load_history(out_dir: Path, serial: str, before: dt.date) -> list[dict[str, Any]]:
    """Load prior daily reports for this serial, sorted ascending by date."""
    serial_dir = out_dir / serial
    if not serial_dir.exists():
        return []
    reports: list[dict[str, Any]] = []
    for p in sorted(serial_dir.glob("*.json")):
        try:
            data = json.loads(p.read_text())
        except json.JSONDecodeError:
            continue
        if "date" not in data or "health_score" not in data:
            continue
        try:
            if dt.date.fromisoformat(data["date"]) >= before:
                continue
        except ValueError:
            continue
        reports.append(data)
    reports.sort(key=lambda r: r["date"])
    return reports


# ── Renderers ────────────────────────────────────────────────────────────────


def render_markdown(report: dict[str, Any]) -> str:
    score = report["health_score"]
    label = report["label"]
    bar_len = 30
    lines: list[str] = []
    lines.append(f"# {report['serial']}  ·  {report['date']}")
    lines.append("")
    lines.append(f"**{score}**  ·  **{label}**")
    lines.append("")
    lines.append("```")
    composite_keys = [k for k in report["subscores"] if k != "uptime"]
    for key in composite_keys:
        val = report["subscores"][key]
        bar = "█" * int(val / 100 * bar_len) + "░" * (bar_len - int(val / 100 * bar_len))
        display_name = key.replace("_", " ").title().ljust(18)
        delta_field = report.get("trend_vs_7d", {}).get(key)
        delta_str = ""
        if delta_field is not None and abs(delta_field) >= 1.0:
            arrow = "↑" if delta_field > 0 else "↓"
            delta_str = f"  {arrow}{abs(delta_field):.0f}"
        lines.append(f"{display_name} {bar}  {val:5.1f}{delta_str}")
    lines.append("")
    # Uptime as a separate informational line
    up_val = report["subscores"]["uptime"]
    up_bar = "█" * int(up_val / 100 * bar_len) + "░" * (bar_len - int(up_val / 100 * bar_len))
    up_delta = report.get("trend_vs_7d", {}).get("uptime")
    up_delta_str = ""
    if up_delta is not None and abs(up_delta) >= 1.0:
        arrow = "↑" if up_delta > 0 else "↓"
        up_delta_str = f"  {arrow}{abs(up_delta):.0f}"
    lines.append(f"{'Uptime (informational)'.ljust(18)} {up_bar}  {up_val:5.1f}{up_delta_str}")
    lines.append("```")
    lines.append("")
    lines.append("## Highlights")
    for h in report["highlights"]:
        lines.append(f"- {h}")
    lines.append("")
    lines.append("## Numbers")
    for key, val in report["summary"].items():
        if val is None:
            continue
        lines.append(f"- **{key}**: {val}")
    return "\n".join(lines) + "\n"


def render_html(report: dict[str, Any], history: list[dict[str, Any]]) -> str:
    """Single-file dashboard. Loads Chart.js from CDN for the trend chart."""
    score = report["health_score"]
    color = report["color"]
    label = report["label"]
    serial = html.escape(str(report["serial"]))
    date_str = report["date"]
    subscores = report["subscores"]
    trend_history = history + [report]
    trend_dates = [h["date"] for h in trend_history]
    trend_scores = [h["health_score"] for h in trend_history]
    trend_uptime = [h["subscores"]["uptime"] for h in trend_history]
    trend_coupling = [h["subscores"]["coupling"] for h in trend_history]
    trend_incident = [h["subscores"]["incident_burden"] for h in trend_history]

    def sub_ring(name: str, value: float) -> str:
        pct = max(0.0, min(100.0, float(value)))
        sub_label, sub_color = band_for(pct)
        circumf = 2 * math.pi * 38  # radius 38
        offset = circumf * (1 - pct / 100)
        return f"""
        <div class="sub-ring">
          <svg viewBox="0 0 100 100" width="100" height="100">
            <circle cx="50" cy="50" r="38" stroke="#1f2937" stroke-width="8" fill="none"/>
            <circle cx="50" cy="50" r="38" stroke="{sub_color}" stroke-width="8" fill="none"
              stroke-dasharray="{circumf:.2f}" stroke-dashoffset="{offset:.2f}"
              transform="rotate(-90 50 50)" stroke-linecap="round"/>
            <text x="50" y="55" text-anchor="middle" font-size="22" font-weight="700"
              fill="#f3f4f6">{int(round(pct))}</text>
          </svg>
          <div class="sub-name">{html.escape(name.replace('_', ' ').title())}</div>
        </div>
        """

    def delta_chip(key: str) -> str:
        d = report.get("trend_vs_7d", {}).get(key)
        if d is None or abs(d) < 1.0:
            return ""
        arrow = "▲" if d > 0 else "▼"
        cls = "delta up" if d > 0 else "delta down"
        return f'<span class="{cls}">{arrow}{abs(d):.0f}</span>'

    big_ring_circumf = 2 * math.pi * 90
    big_offset = big_ring_circumf * (1 - score / 100)
    big_delta = delta_chip("health_score")

    highlights_html = "".join(
        f"<li>{html.escape(h)}</li>" for h in report["highlights"]
    )

    summary_rows = "".join(
        f"<tr><td>{html.escape(k.replace('_',' '))}</td><td>{v}</td></tr>"
        for k, v in report["summary"].items() if v is not None
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<title>{serial} · {date_str}</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  :root {{ color-scheme: dark; }}
  * {{ box-sizing: border-box; }}
  body {{
    background: #0b0f17; color: #f3f4f6;
    font-family: -apple-system, BlinkMacSystemFont, "SF Pro Display", "Segoe UI", system-ui, sans-serif;
    margin: 0; padding: 32px 24px; max-width: 920px; margin-inline: auto;
  }}
  header {{ display:flex; align-items:baseline; justify-content:space-between; margin-bottom: 8px; }}
  header h1 {{ font-size: 18px; font-weight: 600; margin: 0; color: #9ca3af; letter-spacing: 0.5px; }}
  header .date {{ font-size: 14px; color: #6b7280; }}
  .hero {{ display:flex; align-items:center; gap: 24px; padding: 32px 0 16px; }}
  .ring-wrap {{ position: relative; flex: 0 0 220px; }}
  .ring-wrap svg {{ transform: rotate(-90deg); }}
  .ring-center {{
    position:absolute; inset:0; display:flex; flex-direction:column;
    align-items:center; justify-content:center; pointer-events:none;
  }}
  .ring-center .score {{ font-size: 64px; font-weight: 700; letter-spacing: -2px; line-height:1; }}
  .ring-center .label {{ font-size: 13px; letter-spacing: 1px; text-transform: uppercase; margin-top: 6px; color: {color}; font-weight: 600; }}
  .delta {{ font-size: 13px; font-weight: 600; margin-left: 8px; }}
  .delta.up   {{ color: #10b981; }}
  .delta.down {{ color: #ef4444; }}
  .hero-body h2 {{ margin: 0 0 4px; font-size: 24px; font-weight: 700; }}
  .hero-body p  {{ margin: 4px 0 0; color: #9ca3af; font-size: 14px; line-height: 1.5; }}
  .subs {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr)); gap: 12px; margin: 28px 0; }}
  .sub-ring {{ background: #111827; border-radius: 16px; padding: 16px 8px 12px; text-align: center; }}
  .sub-name {{ margin-top: 4px; font-size: 12px; color: #9ca3af; letter-spacing: 0.3px; }}
  .section {{ background: #111827; border-radius: 16px; padding: 20px 24px; margin: 16px 0; }}
  .section h3 {{ margin: 0 0 12px; font-size: 14px; color: #9ca3af; font-weight: 600; letter-spacing: 0.5px; text-transform: uppercase; }}
  .section ul {{ margin: 0; padding-left: 20px; }}
  .section li {{ padding: 4px 0; font-size: 14px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  table td {{ padding: 6px 4px; border-bottom: 1px solid #1f2937; }}
  table td:first-child {{ color: #9ca3af; }}
  table td:last-child {{ text-align: right; font-variant-numeric: tabular-nums; }}
  canvas {{ max-height: 220px; }}
  .uptime-row .uptime-body {{ display:flex; align-items:center; gap: 20px; }}
  .uptime-row .uptime-body .sub-ring {{ flex: 0 0 100px; padding: 0; background: transparent; }}
  .uptime-row .uptime-body p {{ margin: 0; color: #9ca3af; font-size: 13px; line-height: 1.5; }}
  .tag {{ display:inline-block; padding: 2px 8px; margin-left: 8px; border-radius: 999px;
          background: #1f2937; color: #9ca3af; font-size: 10px; letter-spacing: 0.5px;
          font-weight: 500; text-transform: uppercase; }}
</style>
</head>
<body>
<header>
  <h1>BLUEBOT · {serial}</h1>
  <span class="date">{date_str}</span>
</header>

<section class="hero">
  <div class="ring-wrap">
    <svg viewBox="0 0 220 220" width="220" height="220">
      <circle cx="110" cy="110" r="90" stroke="#1f2937" stroke-width="14" fill="none"/>
      <circle cx="110" cy="110" r="90" stroke="{color}" stroke-width="14" fill="none"
        stroke-dasharray="{big_ring_circumf:.2f}" stroke-dashoffset="{big_offset:.2f}" stroke-linecap="round"/>
    </svg>
    <div class="ring-center">
      <div class="score">{score}{big_delta}</div>
      <div class="label">{label}</div>
    </div>
  </div>
  <div class="hero-body">
    <h2>Meter Health</h2>
    <p>Daily composite across coupling, signal, stability, incident burden, and uptime. Scoring scales against the trained acoustic baseline; same number means the same thing tomorrow.</p>
  </div>
</section>

<div class="subs">
  {''.join(sub_ring(k, v) for k, v in subscores.items() if k != "uptime")}
</div>

<section class="section uptime-row">
  <h3>Uptime <span class="tag">informational</span></h3>
  <div class="uptime-body">
    {sub_ring("uptime", subscores.get("uptime", 0))}
    <p>Fraction of expected frames received over a 24-hour day at the device's typical cadence ({report["summary"]["median_period_s"]} s). This is shown separately so a slow / partial day doesn't drag your health score down.</p>
  </div>
</section>

<section class="section">
  <h3>Highlights</h3>
  <ul>{highlights_html}</ul>
</section>

<section class="section">
  <h3>30-day trend</h3>
  <canvas id="trend"></canvas>
</section>

<section class="section">
  <h3>Numbers</h3>
  <table>{summary_rows}</table>
</section>

<script>
const ctx = document.getElementById('trend');
new Chart(ctx, {{
  type: 'line',
  data: {{
    labels: {json.dumps(trend_dates)},
    datasets: [
      {{ label: 'Health', data: {json.dumps(trend_scores)}, borderColor: '{color}', backgroundColor: 'transparent', tension: 0.3, pointRadius: 3 }},
      {{ label: 'Coupling', data: {json.dumps(trend_coupling)}, borderColor: '#60a5fa', backgroundColor: 'transparent', tension: 0.3, pointRadius: 2 }},
      {{ label: 'Incident Burden', data: {json.dumps(trend_incident)}, borderColor: '#f59e0b', backgroundColor: 'transparent', tension: 0.3, pointRadius: 2 }},
      {{ label: 'Uptime', data: {json.dumps(trend_uptime)}, borderColor: '#a78bfa', backgroundColor: 'transparent', tension: 0.3, pointRadius: 2 }}
    ]
  }},
  options: {{
    plugins: {{ legend: {{ labels: {{ color: '#9ca3af' }} }} }},
    scales: {{
      x: {{ ticks: {{ color: '#6b7280' }}, grid: {{ color: '#1f2937' }} }},
      y: {{ min: 0, max: 100, ticks: {{ color: '#6b7280' }}, grid: {{ color: '#1f2937' }} }}
    }}
  }}
}});
</script>
</body>
</html>
"""


# ── Driver ───────────────────────────────────────────────────────────────────


def parse_date(arg: str) -> dt.date | None:
    if arg in ("today",):
        return dt.date.today()
    if arg in ("yesterday",):
        return dt.date.today() - dt.timedelta(days=1)
    if arg in ("auto", "all", ""):
        return None
    return dt.date.fromisoformat(arg)


def group_by_serial_and_day(
    records: list[dict[str, Any]],
) -> dict[tuple[str | None, dt.date], list[dict[str, Any]]]:
    out: dict[tuple[str | None, dt.date], list[dict[str, Any]]] = defaultdict(list)
    for r in records:
        ts = r.get("timestamp")
        if not ts:
            continue
        try:
            day = to_date(ts)
        except ValueError:
            continue
        serial = r.get("serial")
        out[(serial, day)].append(r)
    return out


def filter_events(
    events: list[dict[str, Any]], serial: str | None, day: dt.date
) -> list[dict[str, Any]]:
    day_start, day_end = iso_day_bounds(day)
    out: list[dict[str, Any]] = []
    for ev in events:
        if serial is not None and ev.get("serial") != serial:
            continue
        ts = ev.get("timestamp")
        if not ts:
            continue
        try:
            when = parse_iso(ts)
        except ValueError:
            continue
        # Include events that could span into this day (within ±1 day).
        if when < day_start - dt.timedelta(days=1) or when > day_end + dt.timedelta(days=1):
            continue
        out.append(ev)
    return out


def write_report_files(
    out_dir: Path, report: dict[str, Any], history: list[dict[str, Any]]
) -> dict[str, Path]:
    serial = report["serial"] or "_unknown_"
    date_str = report["date"]
    serial_dir = out_dir / serial
    serial_dir.mkdir(parents=True, exist_ok=True)
    json_path = serial_dir / f"{date_str}.json"
    md_path = serial_dir / f"{date_str}.md"
    html_path = serial_dir / "index.html"
    json_path.write_text(json.dumps(report, indent=2))
    md_path.write_text(render_markdown(report))
    html_path.write_text(render_html(report, history))
    return {"json": json_path, "md": md_path, "html": html_path}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, default=Path("oneclass_meter_model.json"))
    parser.add_argument("--log-jsonl", type=Path, default=None,
                        help="Per-frame JSONL log from mqtt_stream_analyzer.")
    parser.add_argument("--events-jsonl", type=Path, default=None,
                        help="Events JSONL from mqtt_stream_analyzer.")
    parser.add_argument("--csv", type=Path, default=None,
                        help="GUI-captured CSV to use as source instead of JSONL.")
    parser.add_argument("--date", default="auto",
                        help="Date to report on: YYYY-MM-DD, 'today', 'yesterday', "
                             "or 'auto' (generate one report per (serial, day) in source).")
    parser.add_argument("--serial", default=None,
                        help="Only process this serial (otherwise: all serials present).")
    parser.add_argument("--out-dir", type=Path, default=Path("reports"),
                        help="Where to write per-serial subdirectories (default ./reports).")
    args = parser.parse_args()

    if not args.csv and not args.log_jsonl:
        raise SystemExit("Provide --csv or --log-jsonl as data source.")
    if not args.model.exists():
        raise SystemExit(f"Model not found: {args.model}")

    model = json.loads(args.model.read_text())
    target_date = parse_date(args.date)

    # Load records.
    if args.csv:
        records = records_from_csv(
            args.csv, model, serial_filter=args.serial, date_filter=target_date
        )
        events = load_jsonl(args.events_jsonl)
    else:
        all_records = load_jsonl(args.log_jsonl)
        # Filter by serial / date.
        records = []
        for r in all_records:
            if args.serial and r.get("serial") != args.serial:
                continue
            try:
                if target_date is not None and to_date(r["timestamp"]) != target_date:
                    continue
            except (KeyError, ValueError):
                continue
            records.append(r)
        events = load_jsonl(args.events_jsonl)

    if not records:
        print("No records matched the filters — nothing to report.")
        return

    grouped = group_by_serial_and_day(records)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    print(f"Generating {len(grouped)} report(s) → {args.out_dir.resolve()}")

    for (serial, day), day_records in sorted(grouped.items(), key=lambda kv: kv[0][1]):
        day_events = filter_events(events, serial, day)
        report = aggregate_day(day_records, day_events, model, day)
        if not report:
            continue
        history = load_history(args.out_dir, serial or "_unknown_", day)
        add_trend_deltas(report, history)
        paths = write_report_files(args.out_dir, report, history)
        print(
            f"  {serial} {day} score={report['health_score']:>3} ({report['label']:<14}) "
            f"frames={report['summary']['frames_observed']:>5}  "
            f"→ {paths['md'].name}, {paths['html'].name}"
        )


if __name__ == "__main__":
    main()
