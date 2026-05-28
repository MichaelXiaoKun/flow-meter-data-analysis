#!/usr/bin/env python3
"""Train an experimental one-class model from good ultrasonic ADC waveforms.

This is intentionally a first-stage model:

* It only uses known-good captures.
* It learns a normal acoustic profile and robust feature distribution.
* It reports holdout false-positive behavior.
* It probes sensitivity with deterministic synthetic anomalies.

The model is not a supervised empty-pipe/air classifier yet because the current
dataset does not contain those labels.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
from pathlib import Path
from typing import Callable, Iterable


ADC_SAMPLE_RATE_HZ = 100_000_000
BASELINE_SAMPLES = 160
EPS = 1e-9


FEATURES = [
    "baseline_v",
    "noise_rms_v",
    "gate_rms_v",
    "peak_abs_gate_v",
    "snr_db",
    "template_corr",
    "low_clip_ratio",
    "high_clip_ratio",
    "ptp_v",
]

DIAGNOSTIC_FEATURES = [
    "first_arrival_offset_samples",
    "peak_offset_samples",
]

MODE_MIN_TRAIN_ROWS = 25

MIN_ROBUST_SIGMA = {
    "baseline_v": 0.05,
    "noise_rms_v": 0.005,
    "gate_rms_v": 0.02,
    "peak_abs_gate_v": 0.05,
    "snr_db": 0.20,
    "template_corr": 0.002,
    "low_clip_ratio": 0.005,
    "high_clip_ratio": 0.001,
    "ptp_v": 0.05,
}


def sample_columns(header: list[str]) -> list[int]:
    indexed: list[tuple[int, int]] = []
    for idx, name in enumerate(header):
        if not name.startswith("s_"):
            continue
        try:
            indexed.append((int(name[2:]), idx))
        except ValueError:
            continue
    if not indexed:
        raise ValueError("No sample columns named s_0, s_1, ... were found.")
    return [idx for _, idx in sorted(indexed)]


def read_header(path: Path) -> tuple[list[str], list[int]]:
    with path.open(newline="") as f:
        reader = csv.reader(f)
        header = next(reader)
    return header, sample_columns(header)


def iter_waveforms(path: Path, indices: list[int]) -> Iterable[tuple[int, list[float]]]:
    with path.open(newline="") as f:
        reader = csv.reader(f)
        next(reader)
        for row_idx, row in enumerate(reader, start=1):
            yield row_idx, [float(row[i]) for i in indices]


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
        "p995": percentile(sorted_values, 99.5),
        "p999": percentile(sorted_values, 99.9),
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
    if denom <= EPS:
        return 0.0
    return dot / denom


def count_rows(path: Path) -> int:
    with path.open(newline="") as f:
        return max(sum(1 for _ in f) - 1, 0)


def label_row_filter(path: Path, labels: set[str]) -> Callable[[int], bool] | None:
    """Build a row predicate that keeps only rows whose ``label`` column is in
    ``labels``. Returns ``None`` if the CSV has no ``label`` column (which is
    the case for older datasets captured before the GUI added auto-labeling).

    The labels expected here are the ones the rainbird-test-gui writes:
    ``good``, ``fair``, ``poor``, or ``unknown`` (derived from ``diagnose.sq``).
    """
    with path.open(newline="") as f:
        reader = csv.reader(f)
        header = next(reader)
        if "label" not in header:
            return None
        label_idx = header.index("label")
        keep: set[int] = set()
        for row_idx, row in enumerate(reader, start=1):
            if label_idx >= len(row):
                continue
            if row[label_idx].strip().lower() in labels:
                keep.add(row_idx)
    return lambda i: i in keep


def split_selector(
    total_rows: int,
    train_fraction: float,
    split_mode: str,
) -> Callable[[int], bool]:
    if split_mode == "temporal":
        train_rows = max(1, min(total_rows - 1, int(total_rows * train_fraction)))
        return lambda row_idx: row_idx <= train_rows
    if split_mode == "interleaved":
        threshold = int(max(0.01, min(train_fraction, 0.99)) * 10_000)

        def is_train(row_idx: int) -> bool:
            hashed = (row_idx * 1_103_515_245 + 12_345) & 0x7FFFFFFF
            return hashed % 10_000 < threshold

        return is_train
    raise ValueError(f"Unknown split mode: {split_mode}")


def learn_template(
    path: Path,
    indices: list[int],
    is_train: Callable[[int], bool],
) -> dict:
    sample_count = len(indices)
    sums = [0.0] * sample_count
    rows = 0

    for row_idx, values in iter_waveforms(path, indices):
        if not is_train(row_idx):
            continue
        rows += 1
        for i, value in enumerate(values):
            sums[i] += value

    if rows == 0:
        raise ValueError("Training split is empty.")

    mean_wave = [value / rows for value in sums]
    mean_baseline = median(mean_wave[:BASELINE_SAMPLES])
    centered_mean = [value - mean_baseline for value in mean_wave]
    search_start = min(BASELINE_SAMPLES, sample_count - 1)
    peak_idx = max(range(search_start, sample_count), key=lambda i: abs(centered_mean[i]))
    gate_start = max(BASELINE_SAMPLES, peak_idx - 160)
    gate_end = min(sample_count, peak_idx + 160)

    return {
        "sample_count": sample_count,
        "sample_rate_hz": ADC_SAMPLE_RATE_HZ,
        "baseline_samples": BASELINE_SAMPLES,
        "template_rows": rows,
        "template_peak_idx": peak_idx,
        "template_peak_time_us": peak_idx / ADC_SAMPLE_RATE_HZ * 1_000_000,
        "gate_start": gate_start,
        "gate_end": gate_end,
        "gate_start_us": gate_start / ADC_SAMPLE_RATE_HZ * 1_000_000,
        "gate_end_us": gate_end / ADC_SAMPLE_RATE_HZ * 1_000_000,
        "mean_baseline_v": mean_baseline,
        "template_gate": centered_mean[gate_start:gate_end],
    }


def extract_features(values: list[float], profile: dict) -> dict[str, float]:
    baseline_samples = int(profile["baseline_samples"])
    gate_start = int(profile["gate_start"])
    gate_end = int(profile["gate_end"])
    peak_idx = int(profile["template_peak_idx"])
    template_gate = profile["template_gate"]

    row_baseline = median(values[:baseline_samples])
    centered = [value - row_baseline for value in values]
    gate = centered[gate_start:gate_end]
    noise = rms(centered[:baseline_samples])
    gate_rms = rms(gate)
    peak_abs_gate = max(abs(value) for value in gate)
    peak_local = max(range(len(gate)), key=lambda i: abs(gate[i]))
    peak_abs_idx = gate_start + peak_local
    snr_db = 20.0 * math.log10(max(gate_rms, EPS) / max(noise, EPS))
    corr = normalized_corr(gate, template_gate)
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
        "first_arrival_offset_samples": float(first_arrival - peak_idx)
        if first_arrival >= 0
        else 9999.0,
        "peak_offset_samples": float(peak_abs_idx - peak_idx),
        "low_clip_ratio": sum(1 for value in values if value <= 0.05) / len(values),
        "high_clip_ratio": sum(1 for value in values if value >= 3.25) / len(values),
        "ptp_v": max(values) - min(values),
    }


def robust_fit(feature_rows: list[dict[str, float]]) -> dict:
    stats: dict[str, dict[str, float]] = {}
    for name in FEATURES:
        values = [row[name] for row in feature_rows]
        center = median(values)
        deviations = [abs(value - center) for value in values]
        mad = median(deviations)
        robust_sigma = max(1.4826 * mad, MIN_ROBUST_SIGMA.get(name, 1e-6))
        stats[name] = {
            "center": center,
            "mad": mad,
            "robust_sigma": robust_sigma,
            **quantiles(values),
        }
    return stats


def anomaly_score(features: dict[str, float], feature_stats: dict) -> tuple[float, dict[str, float]]:
    zscores: dict[str, float] = {}
    total = 0.0
    for name in FEATURES:
        stat = feature_stats[name]
        z = abs(features[name] - stat["center"]) / stat["robust_sigma"]
        zscores[name] = z
        total += min(z, 20.0) ** 2
    return math.sqrt(total / len(FEATURES)), zscores


def label_from_score(score: float, thresholds: dict[str, float]) -> str:
    if score >= thresholds["anomaly"]:
        return "anomaly"
    if score >= thresholds["suspect"]:
        return "suspect"
    return "normal"


def top_reasons(zscores: dict[str, float], limit: int = 4) -> list[dict[str, float]]:
    return [
        {"feature": name, "z": z}
        for name, z in sorted(zscores.items(), key=lambda item: item[1], reverse=True)[:limit]
    ]


def peak_mode_from_features(features: dict[str, float]) -> str:
    """Bucket a waveform by which repeated echo peak the detector locked onto."""
    offset = features.get("peak_offset_samples", 0.0)
    if offset <= -50.0:
        return "peak_minus_100"
    if offset >= 25.0:
        return "peak_plus_50"
    if -25.0 <= offset <= 25.0:
        return "peak_center"
    return "peak_other"


def group_rows_by_peak_mode(
    rows: list[tuple[int, dict[str, float]]],
) -> dict[str, list[tuple[int, dict[str, float]]]]:
    grouped: dict[str, list[tuple[int, dict[str, float]]]] = {}
    for row_idx, features in rows:
        mode = peak_mode_from_features(features)
        grouped.setdefault(mode, []).append((row_idx, features))
    return dict(sorted(grouped.items()))


def build_mode_models(
    rows: list[tuple[int, dict[str, float]]],
    min_rows: int,
) -> tuple[dict[str, dict], dict[str, dict[str, float]], dict[str, dict]]:
    mode_feature_stats: dict[str, dict] = {}
    mode_thresholds: dict[str, dict[str, float]] = {}
    mode_summaries: dict[str, dict] = {}

    for mode, mode_rows in group_rows_by_peak_mode(rows).items():
        if len(mode_rows) < min_rows:
            mode_summaries[mode] = {
                "rows": len(mode_rows),
                "enabled": False,
                "reason": f"fewer_than_{min_rows}_training_rows",
            }
            continue
        stats = robust_fit([features for _, features in mode_rows])
        scores = [anomaly_score(features, stats)[0] for _, features in mode_rows]
        score_quantiles = quantiles(scores)
        thresholds = {
            "suspect": score_quantiles["p995"],
            "anomaly": score_quantiles["p999"],
        }
        mode_feature_stats[mode] = stats
        mode_thresholds[mode] = thresholds
        mode_summaries[mode] = {
            "rows": len(mode_rows),
            "enabled": True,
            "score_quantiles": score_quantiles,
            "score_thresholds": thresholds,
        }

    return mode_feature_stats, mode_thresholds, mode_summaries


def collect_features(
    path: Path,
    indices: list[int],
    profile: dict,
    include_row: Callable[[int], bool],
    transform: Callable[[list[float]], list[float]] | None = None,
    limit: int | None = None,
) -> list[tuple[int, dict[str, float]]]:
    rows: list[tuple[int, dict[str, float]]] = []
    for row_idx, values in iter_waveforms(path, indices):
        if not include_row(row_idx):
            continue
        if transform:
            values = transform(values)
        rows.append((row_idx, extract_features(values, profile)))
        if limit is not None and len(rows) >= limit:
            break
    return rows


def summarize_scores(
    rows: list[tuple[int, dict[str, float]]],
    feature_stats: dict,
    thresholds: dict[str, float],
    mode_feature_stats: dict[str, dict] | None = None,
    mode_thresholds: dict[str, dict[str, float]] | None = None,
) -> dict:
    labels: dict[str, int] = {}
    scores: list[float] = []
    modes: dict[str, int] = {}
    worst: list[tuple[float, int, str, dict[str, float], dict[str, float]]] = []

    for row_idx, features in rows:
        mode = peak_mode_from_features(features)
        modes[mode] = modes.get(mode, 0) + 1
        stats_for_row = (mode_feature_stats or {}).get(mode, feature_stats)
        thresholds_for_row = (mode_thresholds or {}).get(mode, thresholds)
        score, zscores = anomaly_score(features, stats_for_row)
        label = label_from_score(score, thresholds_for_row)
        labels[label] = labels.get(label, 0) + 1
        scores.append(score)
        worst.append((score, row_idx, label, features, zscores))

    worst.sort(reverse=True, key=lambda item: item[0])
    return {
        "rows": len(rows),
        "labels": labels,
        "peak_modes": modes,
        "score_quantiles": quantiles(scores),
        "worst": worst[:5],
    }


def make_attenuated(scale: float) -> Callable[[list[float]], list[float]]:
    def transform(values: list[float]) -> list[float]:
        base = median(values[:BASELINE_SAMPLES])
        return [base + scale * (value - base) for value in values]

    return transform


def make_empty_noise() -> Callable[[list[float]], list[float]]:
    def transform(values: list[float]) -> list[float]:
        base = median(values[:BASELINE_SAMPLES])
        noise = [value - base for value in values[:BASELINE_SAMPLES]]
        if not noise:
            return values
        return [base + 0.8 * noise[i % len(noise)] for i in range(len(values))]

    return transform


def make_shift(samples: int) -> Callable[[list[float]], list[float]]:
    def transform(values: list[float]) -> list[float]:
        base = median(values[:BASELINE_SAMPLES])
        centered = [value - base for value in values]
        shifted = [0.0] * len(centered)
        for i, value in enumerate(centered):
            target = i + samples
            if 0 <= target < len(shifted):
                shifted[target] = value
        return [base + value for value in shifted]

    return transform


def make_bias_shift(delta_v: float) -> Callable[[list[float]], list[float]]:
    def transform(values: list[float]) -> list[float]:
        return [min(3.3, max(0.0, value + delta_v)) for value in values]

    return transform


def make_gain_clip(gain: float) -> Callable[[list[float]], list[float]]:
    def transform(values: list[float]) -> list[float]:
        base = median(values[:BASELINE_SAMPLES])
        return [min(3.3, max(0.0, base + gain * (value - base))) for value in values]

    return transform


def train_model(
    path: Path,
    train_fraction: float,
    split_mode: str,
    synthetic_rows: int,
    labels: set[str] | None = None,
    mode_aware: bool = True,
    mode_min_rows: int = MODE_MIN_TRAIN_ROWS,
) -> tuple[dict, dict]:
    header, indices = read_header(path)
    total_rows = count_rows(path)
    base_is_train = split_selector(total_rows, train_fraction, split_mode)

    # If a label filter is requested and the CSV has a label column, restrict
    # both train and validation splits to the requested labels. We split first,
    # then intersect with the label set so the train/validation ratio stays
    # the same as if all rows were used.
    label_keep = label_row_filter(path, labels) if labels else None
    if label_keep is None:
        is_train = base_is_train
        is_validation = lambda row_idx: not base_is_train(row_idx)
    else:
        is_train = lambda row_idx: base_is_train(row_idx) and label_keep(row_idx)
        is_validation = lambda row_idx: (not base_is_train(row_idx)) and label_keep(row_idx)

    profile = learn_template(path, indices, is_train)
    train_feature_rows = collect_features(path, indices, profile, is_train)
    feature_stats = robust_fit([features for _, features in train_feature_rows])
    train_scores = [
        anomaly_score(features, feature_stats)[0] for _, features in train_feature_rows
    ]
    train_score_quantiles = quantiles(train_scores)
    thresholds = {
        "suspect": train_score_quantiles["p995"],
        "anomaly": train_score_quantiles["p999"],
    }
    mode_feature_stats: dict[str, dict] = {}
    mode_score_thresholds: dict[str, dict[str, float]] = {}
    mode_summary: dict[str, dict] = {}
    if mode_aware:
        mode_feature_stats, mode_score_thresholds, mode_summary = build_mode_models(
            train_feature_rows,
            mode_min_rows,
        )

    validation_rows = collect_features(path, indices, profile, is_validation)
    train_summary = summarize_scores(
        train_feature_rows,
        feature_stats,
        thresholds,
        mode_feature_stats,
        mode_score_thresholds,
    )
    validation_summary = summarize_scores(
        validation_rows,
        feature_stats,
        thresholds,
        mode_feature_stats,
        mode_score_thresholds,
    )

    probes = {
        "weak_signal_20pct": make_attenuated(0.20),
        "empty_like_noise_only": make_empty_noise(),
        "echo_delayed_35_samples": make_shift(35),
        "bias_low_minus_0p30v": make_bias_shift(-0.30),
        "gain_high_clip_1p60x": make_gain_clip(1.60),
    }
    synthetic: dict[str, dict] = {}
    for name, transform in probes.items():
        probe_rows = collect_features(
            path,
            indices,
            profile,
            is_validation,
            transform,
            limit=synthetic_rows,
        )
        synthetic[name] = summarize_scores(
            probe_rows,
            feature_stats,
            thresholds,
            mode_feature_stats,
            mode_score_thresholds,
        )

    model = {
        "model_type": "one_class_robust_feature_model",
        "dataset": str(path),
        "csv_columns": {
            "sample_count": len(indices),
            "non_sample_columns": [name for name in header if not name.startswith("s_")],
        },
        "train_rows": len(train_feature_rows),
        "validation_rows": len(validation_rows),
        "total_rows": total_rows,
        "train_fraction": train_fraction,
        "split_mode": split_mode,
        "label_filter": sorted(labels) if labels else [],
        "profile": profile,
        "features": FEATURES,
        "diagnostic_features_not_scored": DIAGNOSTIC_FEATURES,
        "feature_stats": feature_stats,
        "score_thresholds": thresholds,
        "score_threshold_note": "suspect=p99.5 and anomaly=p99.9 of training good scores",
        "mode_aware": mode_aware,
        "mode_min_train_rows": mode_min_rows,
        "peak_mode_note": (
            "peak mode is bucketed from peak_offset_samples; runtime falls back "
            "to global stats for modes without enough training rows"
        ),
        "mode_feature_stats": mode_feature_stats,
        "mode_score_thresholds": mode_score_thresholds,
        "mode_training_summary": mode_summary,
    }
    report = {
        "train": train_summary,
        "validation": validation_summary,
        "synthetic_probes": synthetic,
    }
    return model, report


def compact_summary(summary: dict) -> dict:
    return {
        "rows": summary["rows"],
        "labels": summary["labels"],
        "peak_modes": summary.get("peak_modes", {}),
        "score_quantiles": summary["score_quantiles"],
        "worst": [
            {
                "score": score,
                "row": row_idx,
                "label": label,
                "reasons": top_reasons(zscores),
            }
            for score, row_idx, label, _, zscores in summary["worst"]
        ],
    }


def print_report(model: dict, report: dict) -> None:
    profile = model["profile"]
    print("Experimental one-class training")
    print(f"  dataset: {model['dataset']}")
    print(
        f"  rows: train={model['train_rows']}, "
        f"validation={model['validation_rows']}, total={model['total_rows']}"
    )
    print(f"  split mode: {model['split_mode']}")
    print(
        "  echo profile: "
        f"peak=s_{profile['template_peak_idx']} ({profile['template_peak_time_us']:.3f} us), "
        f"gate=s_{profile['gate_start']}..s_{profile['gate_end'] - 1} "
        f"({profile['gate_start_us']:.3f}..{profile['gate_end_us']:.3f} us)"
    )
    print(
        "  thresholds: "
        f"suspect>={model['score_thresholds']['suspect']:.3f}, "
        f"anomaly>={model['score_thresholds']['anomaly']:.3f}"
    )
    if model.get("mode_aware"):
        enabled = {
            mode: summary
            for mode, summary in model.get("mode_training_summary", {}).items()
            if summary.get("enabled")
        }
        print(f"  mode-aware thresholds: {len(enabled)} enabled mode(s)")
        for mode, summary in enabled.items():
            thr = summary["score_thresholds"]
            print(
                f"    {mode}: rows={summary['rows']}, "
                f"suspect>={thr['suspect']:.3f}, anomaly>={thr['anomaly']:.3f}"
            )

    for name in ["train", "validation"]:
        summary = report[name]
        print(f"\n{name.capitalize()} summary")
        print(f"  rows: {summary['rows']}")
        print(f"  labels: {summary['labels']}")
        print(f"  peak modes: {summary.get('peak_modes', {})}")
        q = summary["score_quantiles"]
        print(
            "  score q50/q95/q99/q999: "
            f"{q['p50']:.3f}/{q['p95']:.3f}/{q['p99']:.3f}/{q['p999']:.3f}"
        )
        print("  worst examples:")
        for score, row_idx, label, _, zscores in summary["worst"][:3]:
            reasons = ", ".join(
                f"{item['feature']} z={item['z']:.1f}" for item in top_reasons(zscores, 3)
            )
            print(f"    row {row_idx}: score={score:.3f}, label={label}, {reasons}")

    print("\nSynthetic anomaly probes")
    for name, summary in report["synthetic_probes"].items():
        labels = summary["labels"]
        detected = labels.get("suspect", 0) + labels.get("anomaly", 0)
        rate = detected / max(summary["rows"], 1)
        q = summary["score_quantiles"]
        print(
            f"  {name}: detected={detected}/{summary['rows']} ({rate:.1%}), "
            f"labels={labels}, q50={q['p50']:.3f}, q99={q['p99']:.3f}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv", type=Path)
    parser.add_argument("--train-fraction", type=float, default=0.70)
    parser.add_argument(
        "--split-mode",
        choices=["interleaved", "temporal"],
        default="interleaved",
        help="interleaved checks same-distribution behavior; temporal checks time drift.",
    )
    parser.add_argument("--synthetic-rows", type=int, default=2000)
    parser.add_argument(
        "--no-mode-aware",
        action="store_true",
        help="Disable per-peak-mode feature stats / thresholds in the saved model.",
    )
    parser.add_argument(
        "--mode-min-rows",
        type=int,
        default=MODE_MIN_TRAIN_ROWS,
        help=f"Minimum training rows required before enabling a peak-mode model (default {MODE_MIN_TRAIN_ROWS}).",
    )
    parser.add_argument(
        "--labels",
        default="",
        help="Comma-separated label filter (e.g. 'good' or 'good,fair'). "
             "Use with CSVs produced by bluebot-rainbird-test-gui, which auto-labels "
             "each waveform from diagnose.sq. Empty = use all rows (legacy behavior).",
    )
    parser.add_argument("--model-out", type=Path, default=Path("oneclass_meter_model.json"))
    parser.add_argument("--report-out", type=Path, default=Path("oneclass_meter_report.json"))
    args = parser.parse_args()

    labels = {tok.strip().lower() for tok in args.labels.split(",") if tok.strip()}

    model, report = train_model(
        args.csv,
        args.train_fraction,
        args.split_mode,
        args.synthetic_rows,
        labels=labels or None,
        mode_aware=(not args.no_mode_aware),
        mode_min_rows=args.mode_min_rows,
    )
    args.model_out.write_text(json.dumps(model, indent=2))
    report_json = {
        "mode_aware": model.get("mode_aware", False),
        "mode_training_summary": model.get("mode_training_summary", {}),
        "train": compact_summary(report["train"]),
        "validation": compact_summary(report["validation"]),
        "synthetic_probes": {
            name: compact_summary(summary)
            for name, summary in report["synthetic_probes"].items()
        },
    }
    args.report_out.write_text(json.dumps(report_json, indent=2))
    print_report(model, report)
    print(f"\nWrote model: {args.model_out}")
    print(f"Wrote report: {args.report_out}")


if __name__ == "__main__":
    main()
