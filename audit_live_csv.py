#!/usr/bin/env python3
"""Offline audit for live meter waveform CSV captures."""

from __future__ import annotations

import argparse
import csv
import json
import math
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from cnn_embedding_runtime import CnnEmbeddingScorer
from mqtt_stream_analyzer import AdaptiveAnalyzer


FEATURES_TO_SUMMARIZE = [
    "noise_rms_v",
    "gate_rms_v",
    "template_corr",
    "snr_db",
    "low_clip_ratio",
    "peak_offset_samples",
    "first_arrival_offset_samples",
]


def parse_ts(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def safe_float(raw: Any) -> float | None:
    if raw is None or raw == "":
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def quantile(values: list[float], pct: float) -> float | None:
    values = sorted(value for value in values if math.isfinite(value))
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    pos = (len(values) - 1) * pct / 100.0
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return values[lo]
    return values[lo] * (hi - pos) + values[hi] * (pos - lo)


def stats(values: list[float]) -> dict[str, float | int | None]:
    cleaned: list[float] = []
    for value in values:
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(numeric):
            cleaned.append(numeric)
    if not cleaned:
        return {
            "n": 0,
            "min": None,
            "p05": None,
            "p50": None,
            "mean": None,
            "p95": None,
            "max": None,
        }
    values = cleaned
    return {
        "n": len(values),
        "min": min(values),
        "p05": quantile(values, 5),
        "p50": quantile(values, 50),
        "mean": sum(values) / len(values),
        "p95": quantile(values, 95),
        "max": max(values),
    }


def sample_columns(header: list[str]) -> list[str]:
    indexed: list[tuple[int, str]] = []
    for name in header:
        if not name.startswith("s_"):
            continue
        try:
            indexed.append((int(name[2:]), name))
        except ValueError:
            continue
    if not indexed:
        raise ValueError("No sample columns named s_0, s_1, ... were found.")
    return [name for _, name in sorted(indexed)]


def make_metadata(row: dict[str, str]) -> dict[str, Any]:
    sq = safe_float(row.get("sq"))
    sq_age = safe_float(row.get("sq_age_ms"))
    ots = safe_float(row.get("ots"))
    return {
        "serial": row.get("serial") or "_unknown_",
        "timestamp": row.get("timestamp"),
        "sq": sq,
        "sq_age_ms": sq_age,
        "sq_label": row.get("label") or "unknown",
        "ots": ots,
    }


def counter_dict(counter: Counter[str]) -> dict[str, int]:
    return dict(sorted(counter.items(), key=lambda item: (-item[1], item[0])))


def short_row(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "row": record["row"],
        "timestamp": record["timestamp"],
        "mode": record["mode"],
        "label": record["label"],
        "raw_state": record["raw_state"],
        "stable_state": record["stable_state"],
        "health": record["health"],
        "health_label": record["health_label"],
        "score": record["score"],
        "top_reasons": record["top_reasons"],
    }


def build_analyzer(args: argparse.Namespace) -> AdaptiveAnalyzer:
    model = json.loads(args.model.read_text())
    cnn = None
    if args.cnn_model is not None:
        cnn = CnnEmbeddingScorer(args.cnn_model, device=args.cnn_device, top_k=args.cnn_top_k)
    return AdaptiveAnalyzer(
        model,
        self_train=False,
        stable_window=args.stable_window,
        template_alpha=0.001,
        center_alpha=0.001,
        empty_window_m=args.empty_window_m,
        empty_window_n=args.empty_window_n,
        empty_recovery_n=args.empty_recovery_n,
        empty_include_weak=(not args.empty_strict),
        air_window_n=args.air_window_n,
        air_corr_std_mult=args.air_corr_std_mult,
        air_recovery_factor=args.air_recovery_factor,
        state_enter_frames=args.state_enter_frames,
        state_recover_frames=args.state_recover_frames,
        cnn_health_weight=args.cnn_health_weight,
        cnn_scorer=cnn,
    )


def audit_csv(args: argparse.Namespace) -> dict[str, Any]:
    analyzer = build_analyzer(args)
    with args.csv.open(newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            raise ValueError(f"CSV is empty: {args.csv}")
        samples = sample_columns(reader.fieldnames)
        rows = list(reader)

    csv_labels: Counter[str] = Counter()
    labels: Counter[str] = Counter()
    raw_states: Counter[str] = Counter()
    stable_states: Counter[str] = Counter()
    peak_modes: Counter[str] = Counter()
    health_labels: Counter[str] = Counter()
    conditions: Counter[str] = Counter()
    events: Counter[str] = Counter()
    confirmations: Counter[str] = Counter()

    timestamps: list[datetime] = []
    intervals_s: list[float] = []
    sq_values: list[float] = []
    sq_age_ms: list[float] = []
    health_scores: list[float] = []
    pattern_scores: list[float] = []
    per_mode: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    cnn_by_mode: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    worst_rows: list[dict[str, Any]] = []

    raw_transitions = 0
    stable_transitions = 0
    mode_switches = 0
    last_raw: str | None = None
    last_stable: str | None = None
    last_mode: str | None = None
    last_ts: datetime | None = None

    for row_number, row in enumerate(rows, start=1):
        csv_labels[row.get("label") or ""] += 1
        sq = safe_float(row.get("sq"))
        sq_age = safe_float(row.get("sq_age_ms"))
        if sq is not None:
            sq_values.append(sq)
        if sq_age is not None:
            sq_age_ms.append(sq_age)
        timestamp = parse_ts(row.get("timestamp"))
        if timestamp is not None:
            timestamps.append(timestamp)
            if last_ts is not None:
                intervals_s.append((timestamp - last_ts).total_seconds())
            last_ts = timestamp

        waveform = [float(row[name]) for name in samples]
        result = analyzer.analyze(waveform, make_metadata(row))
        mode = result["peak_mode"]
        raw = result["raw_pipe_state"]
        stable = result["pipe_state"]

        labels[result["label"]] += 1
        raw_states[raw] += 1
        stable_states[stable] += 1
        peak_modes[mode] += 1
        health_labels[result["flow_meter_health"]["label"]] += 1
        conditions[result["condition"]] += 1
        confirmations[result["state_confirmation"]["status"]] += 1
        for event in result["detection_events"]:
            events[event["event"]] += 1

        raw_transitions += int(last_raw is not None and raw != last_raw)
        stable_transitions += int(last_stable is not None and stable != last_stable)
        mode_switches += int(last_mode is not None and mode != last_mode)
        last_raw = raw
        last_stable = stable
        last_mode = mode

        for name in FEATURES_TO_SUMMARIZE:
            per_mode[mode][name].append(float(result["features"][name]))
        per_mode[mode]["score"].append(float(result["score"]))
        per_mode[mode]["health"].append(float(result["flow_meter_health"]["score"]))
        health_scores.append(float(result["flow_meter_health"]["score"]))
        pattern_scores.append(float(result["flow_meter_health"]["subscores"]["acoustic_pattern_match"]))

        cnn = result.get("cnn_analysis")
        if isinstance(cnn, dict) and "error" not in cnn:
            for metric in ("reconstruction_mse", "nearest_embedding_distance", "nearest_similarity"):
                value = safe_float(cnn.get(metric))
                if value is not None:
                    cnn_by_mode[mode][metric].append(value)

        worst_rows.append(
            {
                "row": row_number,
                "timestamp": row.get("timestamp"),
                "mode": mode,
                "label": result["label"],
                "raw_state": raw,
                "stable_state": stable,
                "health": float(result["flow_meter_health"]["score"]),
                "health_label": result["flow_meter_health"]["label"],
                "score": float(result["score"]),
                "top_reasons": result["top_z_reasons"][:3],
            }
        )

    time_summary: dict[str, Any] = {
        "start": min(timestamps).isoformat() if timestamps else None,
        "end": max(timestamps).isoformat() if timestamps else None,
        "duration_s": (
            (max(timestamps) - min(timestamps)).total_seconds()
            if len(timestamps) >= 2 else None
        ),
        "interval_s": stats(intervals_s),
    }

    return {
        "file": str(args.csv),
        "model": str(args.model),
        "cnn_model": str(args.cnn_model) if args.cnn_model else None,
        "rows": len(rows),
        "sample_columns": len(samples),
        "time": time_summary,
        "csv_labels": counter_dict(csv_labels),
        "sq": stats(sq_values),
        "sq_age_ms": stats(sq_age_ms),
        "analyzer": {
            "labels": counter_dict(labels),
            "raw_states": counter_dict(raw_states),
            "stable_states": counter_dict(stable_states),
            "peak_modes": counter_dict(peak_modes),
            "health_labels": counter_dict(health_labels),
            "conditions": counter_dict(conditions),
            "detection_events": counter_dict(events),
            "state_confirmations": counter_dict(confirmations),
            "raw_transitions": raw_transitions,
            "stable_transitions": stable_transitions,
            "mode_switches": mode_switches,
            "health_score": stats(health_scores),
            "pattern_score": stats(pattern_scores),
        },
        "per_mode": {
            mode: {key: stats(values) for key, values in groups.items()}
            for mode, groups in sorted(per_mode.items())
        },
        "cnn_by_mode": {
            mode: {key: stats(values) for key, values in groups.items()}
            for mode, groups in sorted(cnn_by_mode.items())
        },
        "worst_by_score": [
            short_row(record)
            for record in sorted(worst_rows, key=lambda item: item["score"], reverse=True)[:args.top_n]
        ],
        "worst_by_health": [
            short_row(record)
            for record in sorted(worst_rows, key=lambda item: item["health"])[:args.top_n]
        ],
    }


def fmt_stat(summary: dict[str, Any], key: str = "mean") -> str:
    value = summary.get(key)
    if value is None:
        return "-"
    if isinstance(value, int):
        return str(value)
    return f"{float(value):.3f}"


def markdown_report(audit: dict[str, Any]) -> str:
    analyzer = audit["analyzer"]
    lines = [
        f"# Waveform CSV Audit: {Path(audit['file']).name}",
        "",
        "## Summary",
        "",
        f"- Rows: {audit['rows']}",
        f"- Sample columns: {audit['sample_columns']}",
        f"- Time range: {audit['time']['start']} to {audit['time']['end']}",
        f"- Duration seconds: {audit['time']['duration_s']}",
        f"- Median interval seconds: {fmt_stat(audit['time']['interval_s'], 'p50')}",
        f"- CSV labels: `{audit['csv_labels']}`",
        f"- Analyzer labels: `{analyzer['labels']}`",
        f"- Peak modes: `{analyzer['peak_modes']}`",
        f"- Raw states: `{analyzer['raw_states']}`",
        f"- Stable states: `{analyzer['stable_states']}`",
        f"- Health labels: `{analyzer['health_labels']}`",
        f"- Detection events: `{analyzer['detection_events']}`",
        "",
        "## Scores",
        "",
        "| Metric | n | min | p50 | mean | p95 | max |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for label, summary in [
        ("Health", analyzer["health_score"]),
        ("Pattern", analyzer["pattern_score"]),
        ("SQ", audit["sq"]),
        ("SQ age ms", audit["sq_age_ms"]),
    ]:
        lines.append(
            f"| {label} | {summary['n']} | {fmt_stat(summary, 'min')} | "
            f"{fmt_stat(summary, 'p50')} | {fmt_stat(summary, 'mean')} | "
            f"{fmt_stat(summary, 'p95')} | {fmt_stat(summary, 'max')} |"
        )

    lines += ["", "## Per Mode", ""]
    for mode, groups in audit["per_mode"].items():
        lines += [
            f"### {mode}",
            "",
            "| Metric | n | min | p50 | mean | p95 | max |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
        for metric, summary in groups.items():
            lines.append(
                f"| {metric} | {summary['n']} | {fmt_stat(summary, 'min')} | "
                f"{fmt_stat(summary, 'p50')} | {fmt_stat(summary, 'mean')} | "
                f"{fmt_stat(summary, 'p95')} | {fmt_stat(summary, 'max')} |"
            )
        lines.append("")

    lines += ["## Worst Rows By Score", ""]
    for record in audit["worst_by_score"]:
        reasons = ", ".join(
            f"{item['feature']} z={item['z']:.1f}" for item in record["top_reasons"]
        )
        lines.append(
            f"- row {record['row']} {record['timestamp']}: "
            f"score={record['score']:.3f}, health={record['health']:.1f}/"
            f"{record['health_label']}, mode={record['mode']}, "
            f"label={record['label']}, raw={record['raw_state']}; {reasons}"
        )
    lines.append("")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv", type=Path)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--cnn-model", type=Path, default=None)
    parser.add_argument("--cnn-device", default="auto")
    parser.add_argument("--cnn-top-k", type=int, default=3)
    parser.add_argument("--cnn-health-weight", type=float, default=0.0)
    parser.add_argument("--state-enter-frames", type=int, default=3)
    parser.add_argument("--state-recover-frames", type=int, default=5)
    parser.add_argument("--empty-window-m", type=int, default=5)
    parser.add_argument("--empty-window-n", type=int, default=3)
    parser.add_argument("--empty-recovery-n", type=int, default=1)
    parser.add_argument("--empty-strict", action="store_true")
    parser.add_argument("--air-window-n", type=int, default=20)
    parser.add_argument("--air-corr-std-mult", type=float, default=5.0)
    parser.add_argument("--air-recovery-factor", type=float, default=0.5)
    parser.add_argument("--stable-window", type=int, default=64)
    parser.add_argument("--top-n", type=int, default=12)
    parser.add_argument("--json-out", type=Path, default=None)
    parser.add_argument("--markdown-out", type=Path, default=None)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    audit = audit_csv(args)
    if args.json_out is not None:
        args.json_out.write_text(json.dumps(audit, indent=2))
    if args.markdown_out is not None:
        args.markdown_out.write_text(markdown_report(audit))

    analyzer = audit["analyzer"]
    print(f"Audit: {audit['file']}")
    print(f"  rows={audit['rows']} duration_s={audit['time']['duration_s']}")
    print(f"  labels={analyzer['labels']}")
    print(f"  peak_modes={analyzer['peak_modes']}")
    print(f"  raw_states={analyzer['raw_states']}")
    print(f"  stable_states={analyzer['stable_states']}")
    print(f"  health_labels={analyzer['health_labels']}")
    print(
        f"  transitions: raw={analyzer['raw_transitions']} "
        f"stable={analyzer['stable_transitions']} mode={analyzer['mode_switches']}"
    )
    print(
        "  health mean/p50/p95="
        f"{fmt_stat(analyzer['health_score'], 'mean')}/"
        f"{fmt_stat(analyzer['health_score'], 'p50')}/"
        f"{fmt_stat(analyzer['health_score'], 'p95')}"
    )
    if args.json_out is not None:
        print(f"  wrote json: {args.json_out}")
    if args.markdown_out is not None:
        print(f"  wrote markdown: {args.markdown_out}")


if __name__ == "__main__":
    main()
