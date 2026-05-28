#!/usr/bin/env python3
"""Unsupervised clustering for ultrasonic ADC waveform states.

This script does not use CSV labels. It learns acoustic sub-modes from waveform
features with deterministic k-means, then reports cluster profiles and outliers.

With the current all-good dataset, clusters should be interpreted as normal
sub-states or drift modes. When future empty-pipe / air / weak-signal captures
are mixed in, those states should appear as separate clusters or high-distance
outliers.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path

from train_oneclass_meter_model import (
    FEATURES,
    collect_features,
    count_rows,
    label_row_filter,
    learn_template,
    quantiles,
    read_header,
    robust_fit,
)


CLUSTER_FEATURES = [
    "baseline_v",
    "noise_rms_v",
    "gate_rms_v",
    "peak_abs_gate_v",
    "snr_db",
    "template_corr",
    "low_clip_ratio",
    "ptp_v",
]


def read_metadata(path: Path) -> dict[int, dict[str, str]]:
    metadata: dict[int, dict[str, str]] = {}
    with path.open(newline="") as f:
        reader = csv.DictReader(f)
        for row_idx, row in enumerate(reader, start=1):
            metadata[row_idx] = {
                "timestamp": row.get("timestamp", ""),
                "sq": row.get("sq", ""),
                "sq_age_ms": row.get("sq_age_ms", ""),
                "label": row.get("label", ""),
            }
    return metadata


def vectorize(features: dict[str, float], stats: dict) -> list[float]:
    values: list[float] = []
    for name in CLUSTER_FEATURES:
        stat = stats[name]
        z = (features[name] - stat["center"]) / stat["robust_sigma"]
        values.append(max(-8.0, min(8.0, z)))
    return values


def squared_distance(a: list[float], b: list[float]) -> float:
    return sum((x - y) ** 2 for x, y in zip(a, b))


def choose_initial_centers(points: list[list[float]], k: int) -> list[list[float]]:
    origin = [0.0] * len(points[0])
    first = min(range(len(points)), key=lambda i: squared_distance(points[i], origin))
    centers = [points[first][:]]
    while len(centers) < k:
        farthest = max(
            range(len(points)),
            key=lambda i: min(squared_distance(points[i], center) for center in centers),
        )
        centers.append(points[farthest][:])
    return centers


def kmeans(points: list[list[float]], k: int, max_iter: int) -> tuple[list[int], list[list[float]], float]:
    if k < 1:
        raise ValueError("k must be >= 1")
    if k > len(points):
        raise ValueError("k cannot be larger than number of points")

    centers = choose_initial_centers(points, k)
    assignments = [-1] * len(points)

    for _ in range(max_iter):
        changed = 0
        for i, point in enumerate(points):
            cluster = min(range(k), key=lambda c: squared_distance(point, centers[c]))
            if assignments[i] != cluster:
                assignments[i] = cluster
                changed += 1

        sums = [[0.0] * len(points[0]) for _ in range(k)]
        counts = [0] * k
        for point, cluster in zip(points, assignments):
            counts[cluster] += 1
            for j, value in enumerate(point):
                sums[cluster][j] += value

        for c in range(k):
            if counts[c] == 0:
                farthest = max(
                    range(len(points)),
                    key=lambda i: min(squared_distance(points[i], center) for center in centers),
                )
                centers[c] = points[farthest][:]
                assignments[farthest] = c
                counts[c] = 1
                sums[c] = points[farthest][:]
            else:
                centers[c] = [value / counts[c] for value in sums[c]]

        if changed == 0:
            break

    inertia = sum(
        squared_distance(point, centers[cluster])
        for point, cluster in zip(points, assignments)
    )
    return assignments, centers, inertia


def center_to_feature_units(center: list[float], stats: dict) -> dict[str, float]:
    out: dict[str, float] = {}
    for name, z in zip(CLUSTER_FEATURES, center):
        stat = stats[name]
        out[name] = stat["center"] + z * stat["robust_sigma"]
    return out


def cluster_report(
    rows: list[tuple[int, dict[str, float]]],
    points: list[list[float]],
    assignments: list[int],
    centers: list[list[float]],
    metadata: dict[int, dict[str, str]],
    stats: dict,
) -> dict:
    k = len(centers)
    clusters: list[dict] = []
    nearest_distances: list[float] = []
    outliers: list[tuple[float, int, int, dict[str, float]]] = []

    for c in range(k):
        indices = [i for i, cluster in enumerate(assignments) if cluster == c]
        distances = [math.sqrt(squared_distance(points[i], centers[c])) for i in indices]
        nearest_distances.extend(distances)
        sq_counts: dict[str, int] = {}
        for i in indices:
            row_idx = rows[i][0]
            sq = metadata.get(row_idx, {}).get("sq", "")
            sq_counts[sq] = sq_counts.get(sq, 0) + 1
        clusters.append(
            {
                "cluster": c,
                "count": len(indices),
                "distance_quantiles": quantiles(distances),
                "center": center_to_feature_units(centers[c], stats),
                "sq_counts": dict(sorted(sq_counts.items(), key=lambda item: item[0])),
            }
        )

    for i, (row_idx, features) in enumerate(rows):
        cluster = assignments[i]
        distance = math.sqrt(squared_distance(points[i], centers[cluster]))
        outliers.append((distance, row_idx, cluster, features))
    outliers.sort(reverse=True, key=lambda item: item[0])

    return {
        "distance_thresholds": {
            "suspect": quantiles(nearest_distances)["p995"],
            "anomaly": quantiles(nearest_distances)["p999"],
        },
        "global_distance_quantiles": quantiles(nearest_distances),
        "clusters": clusters,
        "outliers": [
            {
                "row": row_idx,
                "cluster": cluster,
                "distance": distance,
                "metadata": metadata.get(row_idx, {}),
                "features": {name: features[name] for name in CLUSTER_FEATURES},
            }
            for distance, row_idx, cluster, features in outliers[:20]
        ],
    }


def print_report(model: dict, report: dict) -> None:
    profile = model["profile"]
    print("Unsupervised meter clustering")
    print(f"  dataset: {model['dataset']}")
    print(f"  rows: {model['rows']}")
    print(f"  k: {model['k']}")
    print(
        "  echo profile: "
        f"peak=s_{profile['template_peak_idx']} ({profile['template_peak_time_us']:.3f} us), "
        f"gate=s_{profile['gate_start']}..s_{profile['gate_end'] - 1} "
        f"({profile['gate_start_us']:.3f}..{profile['gate_end_us']:.3f} us)"
    )
    q = report["global_distance_quantiles"]
    print(
        "  nearest-cluster distance q50/q95/q99/q999: "
        f"{q['p50']:.3f}/{q['p95']:.3f}/{q['p99']:.3f}/{q['p999']:.3f}"
    )
    print(
        "  distance thresholds: "
        f"suspect>={report['distance_thresholds']['suspect']:.3f}, "
        f"anomaly>={report['distance_thresholds']['anomaly']:.3f}"
    )

    print("\nClusters")
    for cluster in report["clusters"]:
        center = cluster["center"]
        print(
            f"  cluster {cluster['cluster']}: count={cluster['count']}, "
            f"dist_p95={cluster['distance_quantiles']['p95']:.3f}, "
            f"gate_rms={center['gate_rms_v']:.4f}, "
            f"snr={center['snr_db']:.2f}, "
            f"corr={center['template_corr']:.5f}, "
            f"low_clip={center['low_clip_ratio']:.4f}, "
            f"sq={cluster['sq_counts']}"
        )

    print("\nTop outliers")
    for outlier in report["outliers"][:5]:
        features = outlier["features"]
        meta = outlier["metadata"]
        print(
            f"  row {outlier['row']}: distance={outlier['distance']:.3f}, "
            f"cluster={outlier['cluster']}, sq={meta.get('sq', '')}, "
            f"gate_rms={features['gate_rms_v']:.4f}, "
            f"snr={features['snr_db']:.2f}, "
            f"corr={features['template_corr']:.5f}, "
            f"low_clip={features['low_clip_ratio']:.4f}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv", type=Path)
    parser.add_argument("--k", type=int, default=4)
    parser.add_argument("--max-iter", type=int, default=80)
    parser.add_argument(
        "--labels",
        default="",
        help="Comma-separated label filter (e.g. 'good' or 'good,fair'). "
             "Applies to CSVs produced by bluebot-rainbird-test-gui. "
             "Empty = use all rows (legacy behavior).",
    )
    parser.add_argument("--model-out", type=Path, default=Path("unsupervised_meter_model.json"))
    parser.add_argument("--report-out", type=Path, default=Path("unsupervised_meter_report.json"))
    args = parser.parse_args()

    _, indices = read_header(args.csv)
    total_rows = count_rows(args.csv)

    labels = {tok.strip().lower() for tok in args.labels.split(",") if tok.strip()}
    label_keep = label_row_filter(args.csv, labels) if labels else None
    if label_keep is None:
        row_filter = lambda row_idx: True
    else:
        row_filter = label_keep

    profile = learn_template(args.csv, indices, row_filter)
    rows = collect_features(args.csv, indices, profile, row_filter)
    feature_stats = robust_fit([features for _, features in rows])
    points = [vectorize(features, feature_stats) for _, features in rows]
    assignments, centers, inertia = kmeans(points, args.k, args.max_iter)
    metadata = read_metadata(args.csv)

    model = {
        "model_type": "unsupervised_kmeans_acoustic_profile",
        "dataset": str(args.csv),
        "rows": total_rows,
        "training_rows": len(rows),
        "label_filter": sorted(labels) if labels else [],
        "k": args.k,
        "features": CLUSTER_FEATURES,
        "profile": profile,
        "feature_stats": feature_stats,
        "centers_z": centers,
        "centers_feature_units": [
            center_to_feature_units(center, feature_stats) for center in centers
        ],
        "inertia": inertia,
    }
    report = cluster_report(rows, points, assignments, centers, metadata, feature_stats)
    model["distance_thresholds"] = report["distance_thresholds"]

    args.model_out.write_text(json.dumps(model, indent=2))
    args.report_out.write_text(json.dumps(report, indent=2))
    print_report(model, report)
    print(f"\nWrote model: {args.model_out}")
    print(f"Wrote report: {args.report_out}")


if __name__ == "__main__":
    main()
