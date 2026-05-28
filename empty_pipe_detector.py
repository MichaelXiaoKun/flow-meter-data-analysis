#!/usr/bin/env python3
"""Baseline-driven empty-pipe detector for Bluebot ultrasonic ADC captures.

The detector learns a full-pipe acoustic template from known-good captures,
then scores new captures for "empty pipe or lost acoustic path" conditions.
It intentionally uses only the Python standard library so it can run in small
factory/debug environments without pandas or numpy.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
from pathlib import Path
from typing import Iterable


BASELINE_SAMPLES = 160
ADC_SAMPLE_RATE_HZ = 100_000_000


def sample_columns(header: list[str]) -> list[int]:
    pairs: list[tuple[int, int]] = []
    for idx, name in enumerate(header):
        if name.startswith("s_"):
            try:
                pairs.append((int(name[2:]), idx))
            except ValueError:
                pass
    if not pairs:
        raise ValueError("No sample columns named s_0, s_1, ... were found.")
    return [idx for _, idx in sorted(pairs)]


def read_header(path: Path) -> tuple[list[str], list[int]]:
    with path.open(newline="") as f:
        reader = csv.reader(f)
        header = next(reader)
    return header, sample_columns(header)


def iter_waveforms(path: Path, indices: list[int]) -> Iterable[list[float]]:
    with path.open(newline="") as f:
        reader = csv.reader(f)
        next(reader)
        for row in reader:
            yield [float(row[i]) for i in indices]


def median(values: list[float]) -> float:
    return float(statistics.median(values))


def rms(values: Iterable[float]) -> float:
    total = 0.0
    count = 0
    for value in values:
        total += value * value
        count += 1
    return math.sqrt(total / max(count, 1))


def percentile(sorted_values: list[float], pct: float) -> float:
    if not sorted_values:
        return float("nan")
    if len(sorted_values) == 1:
        return sorted_values[0]
    pos = (len(sorted_values) - 1) * pct / 100.0
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return sorted_values[lo]
    return sorted_values[lo] * (hi - pos) + sorted_values[hi] * (pos - lo)


def quantiles(values: list[float]) -> dict[str, float]:
    sorted_values = sorted(values)
    return {
        "p01": percentile(sorted_values, 1),
        "p05": percentile(sorted_values, 5),
        "p50": percentile(sorted_values, 50),
        "p95": percentile(sorted_values, 95),
        "p99": percentile(sorted_values, 99),
    }


def normalized_corr(a: list[float], b: list[float]) -> float:
    dot = 0.0
    aa = 0.0
    bb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        aa += x * x
        bb += y * y
    denom = math.sqrt(aa * bb)
    if denom <= 1e-12:
        return 0.0
    return dot / denom


def learn_template(train_csv: Path) -> dict:
    _, indices = read_header(train_csv)
    sample_count = len(indices)
    sums = [0.0] * sample_count
    rows = 0

    for values in iter_waveforms(train_csv, indices):
        rows += 1
        for i, value in enumerate(values):
            sums[i] += value

    if rows == 0:
        raise ValueError(f"{train_csv} has no data rows.")

    mean_wave = [value / rows for value in sums]
    mean_baseline = median(mean_wave[:BASELINE_SAMPLES])
    centered_mean = [value - mean_baseline for value in mean_wave]

    search_start = min(BASELINE_SAMPLES, sample_count - 1)
    peak_idx = max(
        range(search_start, sample_count),
        key=lambda i: abs(centered_mean[i]),
    )
    gate_start = max(BASELINE_SAMPLES, peak_idx - 160)
    gate_end = min(sample_count, peak_idx + 160)

    template_gate = centered_mean[gate_start:gate_end]
    return {
        "rows": rows,
        "sample_count": sample_count,
        "sample_rate_hz": ADC_SAMPLE_RATE_HZ,
        "baseline_samples": BASELINE_SAMPLES,
        "mean_baseline_v": mean_baseline,
        "template_peak_idx": peak_idx,
        "template_peak_time_us": peak_idx / ADC_SAMPLE_RATE_HZ * 1_000_000,
        "gate_start": gate_start,
        "gate_end": gate_end,
        "gate_start_us": gate_start / ADC_SAMPLE_RATE_HZ * 1_000_000,
        "gate_end_us": gate_end / ADC_SAMPLE_RATE_HZ * 1_000_000,
        "template_gate": template_gate,
    }


def extract_features(values: list[float], model: dict) -> dict[str, float]:
    baseline_samples = int(model["baseline_samples"])
    gate_start = int(model["gate_start"])
    gate_end = int(model["gate_end"])
    template_gate = model["template_gate"]

    row_baseline = median(values[:baseline_samples])
    centered = [value - row_baseline for value in values]
    noise = rms(centered[:baseline_samples])
    gate = centered[gate_start:gate_end]
    gate_rms = rms(gate)
    peak_abs_gate = max(abs(value) for value in gate)
    snr_db = 20 * math.log10(max(gate_rms, 1e-9) / max(noise, 1e-9))
    corr = normalized_corr(gate, template_gate)
    low_clip_ratio = sum(1 for value in values if value <= 0.05) / len(values)
    high_clip_ratio = sum(1 for value in values if value >= 3.25) / len(values)

    arrival_threshold = max(6.0 * noise, 0.15)
    first_arrival = -1
    for offset, value in enumerate(gate):
        if abs(value) > arrival_threshold:
            first_arrival = gate_start + offset
            break

    return {
        "baseline_v": row_baseline,
        "noise_rms_v": noise,
        "gate_rms_v": gate_rms,
        "peak_abs_gate_v": peak_abs_gate,
        "snr_db": snr_db,
        "template_corr": corr,
        "low_clip_ratio": low_clip_ratio,
        "high_clip_ratio": high_clip_ratio,
        "first_arrival_idx": float(first_arrival),
        "first_arrival_us": first_arrival / ADC_SAMPLE_RATE_HZ * 1_000_000
        if first_arrival >= 0
        else -1.0,
    }


def calibrate(train_csv: Path) -> dict:
    model = learn_template(train_csv)
    _, indices = read_header(train_csv)
    feature_rows = [extract_features(values, model) for values in iter_waveforms(train_csv, indices)]

    feature_names = [
        "gate_rms_v",
        "peak_abs_gate_v",
        "snr_db",
        "template_corr",
        "noise_rms_v",
        "baseline_v",
        "low_clip_ratio",
    ]
    stats = {name: quantiles([row[name] for row in feature_rows]) for name in feature_names}

    model["feature_stats"] = stats
    model["thresholds"] = {
        "gate_rms_v": stats["gate_rms_v"]["p01"],
        "peak_abs_gate_v": stats["peak_abs_gate_v"]["p01"],
        "snr_db": stats["snr_db"]["p01"],
        "template_corr": stats["template_corr"]["p01"],
    }
    return model


def low_feature_severity(value: float, p01: float, p05: float) -> float:
    if value < p01:
        return 1.0
    if value < p05:
        return 0.5
    return 0.0


def score_features(features: dict[str, float], model: dict) -> dict:
    stats = model["feature_stats"]
    checks = {
        "weak_gate_energy": low_feature_severity(
            features["gate_rms_v"],
            stats["gate_rms_v"]["p01"],
            stats["gate_rms_v"]["p05"],
        ),
        "weak_peak": low_feature_severity(
            features["peak_abs_gate_v"],
            stats["peak_abs_gate_v"]["p01"],
            stats["peak_abs_gate_v"]["p05"],
        ),
        "low_snr": low_feature_severity(
            features["snr_db"],
            stats["snr_db"]["p01"],
            stats["snr_db"]["p05"],
        ),
        "low_template_match": low_feature_severity(
            features["template_corr"],
            stats["template_corr"]["p01"],
            stats["template_corr"]["p05"],
        ),
        "missing_arrival": 1.0 if features["first_arrival_idx"] < 0 else 0.0,
    }
    score = sum(checks.values()) / len(checks)
    if score >= 0.6:
        label = "empty_pipe_or_lost_acoustic_path"
    elif score >= 0.3:
        label = "suspect"
    else:
        label = "normal"
    return {"score": score, "label": label, "checks": checks}


def stddev(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    return math.sqrt(sum((value - mean) ** 2 for value in values) / len(values))


def rolling_event_summary(rows: list[dict], window_size: int) -> dict:
    if window_size <= 1 or len(rows) < window_size:
        return {"window_size": window_size, "labels": {}, "worst_windows": []}

    labels: dict[str, int] = {}
    worst: list[tuple[float, int, int, str, dict[str, float]]] = []

    for start in range(0, len(rows) - window_size + 1, window_size):
        end = start + window_size
        window = rows[start:end]
        row_labels = [row["label"] for row in window]
        suspect_fraction = sum(label != "normal" for label in row_labels) / window_size
        loss_fraction = sum(
            label == "empty_pipe_or_lost_acoustic_path" for label in row_labels
        ) / window_size
        corr_values = [row["features"]["template_corr"] for row in window]
        rms_values = [row["features"]["gate_rms_v"] for row in window]
        arrivals = [
            row["features"]["first_arrival_idx"]
            for row in window
            if row["features"]["first_arrival_idx"] >= 0
        ]
        corr_std = stddev(corr_values)
        rms_mean = sum(rms_values) / len(rms_values)
        rms_cv = stddev(rms_values) / max(rms_mean, 1e-9)
        arrival_std = stddev(arrivals)

        if loss_fraction >= 0.60:
            label = "sustained_acoustic_loss"
            severity = max(loss_fraction, suspect_fraction)
        elif suspect_fraction >= 0.15 and (
            corr_std >= 0.010 or rms_cv >= 0.080 or arrival_std >= 4.0
        ):
            label = "intermittent_air_or_bubbles"
            severity = suspect_fraction + min(corr_std * 10.0, 0.3) + min(rms_cv, 0.3)
        elif corr_std >= 0.020 or rms_cv >= 0.120 or (
            arrival_std >= 25.0 and suspect_fraction >= 0.05
        ):
            label = "unstable_acoustic_path"
            severity = min(corr_std * 10.0 + rms_cv + arrival_std / 50.0, 1.0)
        else:
            label = "normal"
            severity = suspect_fraction

        labels[label] = labels.get(label, 0) + 1
        metrics = {
            "suspect_fraction": suspect_fraction,
            "loss_fraction": loss_fraction,
            "corr_std": corr_std,
            "gate_rms_cv": rms_cv,
            "arrival_std_samples": arrival_std,
        }
        worst.append((severity, start + 1, end, label, metrics))

    worst.sort(reverse=True, key=lambda item: item[0])
    return {"window_size": window_size, "labels": labels, "worst_windows": worst[:5]}


def score_csv(score_csv: Path, model: dict, window_size: int) -> dict:
    _, indices = read_header(score_csv)
    labels: dict[str, int] = {}
    scores: list[float] = []
    worst: list[tuple[float, int, str, dict[str, float]]] = []
    rows: list[dict] = []

    for row_idx, values in enumerate(iter_waveforms(score_csv, indices), start=1):
        features = extract_features(values, model)
        scored = score_features(features, model)
        label = scored["label"]
        labels[label] = labels.get(label, 0) + 1
        scores.append(scored["score"])
        worst.append((scored["score"], row_idx, label, features))
        rows.append({"label": label, "score": scored["score"], "features": features})

    worst.sort(reverse=True, key=lambda item: item[0])
    return {
        "rows": len(scores),
        "labels": labels,
        "mean_score": sum(scores) / max(len(scores), 1),
        "max_score": max(scores) if scores else 0.0,
        "worst_rows": worst[:5],
        "rolling_events": rolling_event_summary(rows, window_size),
    }


def print_report(model: dict, scoring: dict) -> None:
    print("Baseline model")
    print(f"  train rows: {model['rows']}")
    print(
        "  template peak: "
        f"s_{model['template_peak_idx']} ({model['template_peak_time_us']:.3f} us)"
    )
    print(
        "  detection gate: "
        f"s_{model['gate_start']}..s_{model['gate_end'] - 1} "
        f"({model['gate_start_us']:.3f}..{model['gate_end_us']:.3f} us)"
    )
    print("  feature thresholds from full-pipe baseline:")
    for name, value in model["thresholds"].items():
        print(f"    {name}: {value:.6g}")

    print("\nScoring result")
    print(f"  rows: {scoring['rows']}")
    print(f"  labels: {scoring['labels']}")
    print(f"  mean score: {scoring['mean_score']:.3f}")
    print(f"  max score: {scoring['max_score']:.3f}")
    print("  worst rows:")
    for score, row_idx, label, features in scoring["worst_rows"]:
        print(
            f"    row {row_idx}: score={score:.3f}, label={label}, "
            f"gate_rms={features['gate_rms_v']:.4f}, "
            f"snr={features['snr_db']:.2f} dB, "
            f"corr={features['template_corr']:.3f}, "
            f"arrival={features['first_arrival_idx']:.0f}"
        )

    events = scoring["rolling_events"]
    if events["labels"]:
        print(f"\nRolling gas/air analysis ({events['window_size']} rows per window)")
        print(f"  window labels: {events['labels']}")
        print("  worst windows:")
        for severity, start, end, label, metrics in events["worst_windows"]:
            print(
                f"    rows {start}-{end}: severity={severity:.3f}, label={label}, "
                f"suspect={metrics['suspect_fraction']:.2f}, "
                f"loss={metrics['loss_fraction']:.2f}, "
                f"corr_std={metrics['corr_std']:.4f}, "
                f"rms_cv={metrics['gate_rms_cv']:.3f}, "
                f"arrival_std={metrics['arrival_std_samples']:.2f} samples"
            )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv", nargs="?", type=Path, help="CSV to calibrate and score.")
    parser.add_argument("--train", type=Path, help="Known-full-pipe CSV used to learn baseline.")
    parser.add_argument("--score", type=Path, help="CSV to score. Defaults to the train CSV.")
    parser.add_argument("--model", type=Path, help="Load an existing baseline model JSON.")
    parser.add_argument("--save-model", type=Path, help="Write the learned baseline model JSON.")
    parser.add_argument(
        "--window-size",
        type=int,
        default=64,
        help="Rows per window for intermittent air/bubble detection. Use 0 to disable.",
    )
    args = parser.parse_args()

    if args.model:
        model = json.loads(args.model.read_text())
        score_path = args.score or args.csv
        if not score_path:
            raise SystemExit("--score or csv is required when --model is used.")
    else:
        train_path = args.train or args.csv
        if not train_path:
            raise SystemExit("Provide a CSV path or --train.")
        model = calibrate(train_path)
        score_path = args.score or args.csv or train_path
        if args.save_model:
            args.save_model.write_text(json.dumps(model, indent=2))

    scoring = score_csv(score_path, model, args.window_size)
    print_report(model, scoring)


if __name__ == "__main__":
    main()
