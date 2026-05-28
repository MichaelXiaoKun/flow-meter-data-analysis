#!/usr/bin/env python3
"""Flow meter health scoring helpers.

The health score is a customer-facing measurement-trust index. It does not
estimate flow rate; it estimates whether the meter is producing believable
acoustic measurements right now.
"""

from __future__ import annotations

import math
from typing import Any


FLOW_HEALTH_VERSION = "flow_meter_health_v1"

FLOW_HEALTH_WEIGHTS = {
    "signal_integrity": 0.30,
    "acoustic_pattern_match": 0.25,
    "coupling_condition": 0.20,
    "temporal_stability": 0.15,
    "telemetry_reliability": 0.10,
}

FLOW_HEALTH_BANDS = [
    (90, "Excellent", "#10b981"),
    (75, "Healthy", "#3b82f6"),
    (60, "Watch", "#f59e0b"),
    (40, "Degraded", "#ef4444"),
    (0, "Unreliable", "#991b1b"),
]


def clamp(value: float, lo: float = 0.0, hi: float = 100.0) -> float:
    if not math.isfinite(value):
        return lo
    return max(lo, min(hi, value))


def band_for(score: float) -> tuple[str, str]:
    for cutoff, label, color in FLOW_HEALTH_BANDS:
        if score >= cutoff:
            return label, color
    return FLOW_HEALTH_BANDS[-1][1], FLOW_HEALTH_BANDS[-1][2]


def z_score_to_100(
    value: float,
    stat: dict[str, float],
    *,
    higher_is_better: bool = True,
    points_per_sigma: float = 25.0,
) -> float:
    center = float(stat.get("center", 0.0))
    sigma = max(float(stat.get("robust_sigma", 1e-9)), 1e-9)
    z = (float(value) - center) / sigma
    if not higher_is_better:
        z = -z
    if z >= 0:
        return 100.0
    return clamp(100.0 + z * points_per_sigma)


def anomaly_score_to_100(score: float, thresholds: dict[str, float]) -> float:
    suspect = max(float(thresholds.get("suspect", 1.0)), 1e-9)
    anomaly = max(float(thresholds.get("anomaly", suspect + 1.0)), suspect + 1e-9)
    score = max(float(score), 0.0)
    if score <= suspect:
        return clamp(100.0 - 20.0 * score / suspect)
    if score <= anomaly:
        return clamp(80.0 - 40.0 * (score - suspect) / (anomaly - suspect))
    return clamp(40.0 * math.exp(-(score - anomaly)))


def sq_to_100(sq: Any) -> float:
    if isinstance(sq, (int, float)) and math.isfinite(float(sq)):
        return clamp(float(sq))
    return 50.0


def age_penalty(age_ms: Any, *, stale_ms: float, max_penalty: float) -> float:
    if age_ms is None:
        return max_penalty * 0.5
    try:
        age = max(float(age_ms), 0.0)
    except (TypeError, ValueError):
        return max_penalty * 0.5
    if age <= stale_ms:
        return 0.0
    # Ramp from no penalty at stale_ms to max_penalty at 3x stale_ms.
    return clamp((age - stale_ms) / max(stale_ms * 2.0, 1.0) * max_penalty, 0.0, max_penalty)


def compute_realtime_flow_health(
    *,
    features: dict[str, float],
    feature_stats: dict[str, dict[str, float]],
    anomaly_score: float,
    thresholds: dict[str, float],
    label: str,
    pipe_state: str,
    active_conditions: list[str] | set[str],
    metadata: dict[str, Any],
    air_corr_std: float | None = None,
    air_corr_threshold: float | None = None,
    cnn: dict[str, Any] | None = None,
    cnn_weight: float = 0.5,
) -> dict[str, Any]:
    """Compute a per-frame flow-meter health score.

    ``cnn`` is optional future input from the CNN embedding analyzer. If
    present, it may include ``reconstruction_mse``, ``reconstruction_thresholds``,
    ``nearest_embedding_distance`` and ``nearest_embedding_distance_thresholds``.
    """
    stats = feature_stats
    conditions = set(active_conditions)

    snr = z_score_to_100(features["snr_db"], stats["snr_db"])
    gate = z_score_to_100(features["gate_rms_v"], stats["gate_rms_v"])
    noise = z_score_to_100(features["noise_rms_v"], stats["noise_rms_v"], higher_is_better=False)
    signal_integrity = 0.45 * snr + 0.35 * gate + 0.20 * noise

    oneclass_match = anomaly_score_to_100(anomaly_score, thresholds)
    pattern_match = oneclass_match
    if cnn:
        cnn_scores: list[float] = []
        recon = cnn.get("reconstruction_mse")
        recon_thresholds = cnn.get("reconstruction_thresholds") or {}
        if isinstance(recon, (int, float)) and recon_thresholds:
            cnn_scores.append(anomaly_score_to_100(float(recon), {
                "suspect": float(recon_thresholds.get("suspect", recon)),
                "anomaly": float(recon_thresholds.get("anomaly", recon + 1e-9)),
            }))
        dist = cnn.get("nearest_embedding_distance")
        dist_thresholds = cnn.get("nearest_embedding_distance_thresholds") or {}
        if isinstance(dist, (int, float)) and dist_thresholds:
            cnn_scores.append(anomaly_score_to_100(float(dist), {
                "suspect": float(dist_thresholds.get("suspect", dist)),
                "anomaly": float(dist_thresholds.get("anomaly", dist + 1e-9)),
            }))
        if cnn_scores:
            cnn_weight = clamp(float(cnn_weight), 0.0, 1.0)
            cnn_match = sum(cnn_scores) / len(cnn_scores)
            pattern_match = (1.0 - cnn_weight) * oneclass_match + cnn_weight * cnn_match

    corr = z_score_to_100(features["template_corr"], stats["template_corr"])
    peak = z_score_to_100(features["peak_abs_gate_v"], stats["peak_abs_gate_v"])
    low_clip_stat = stats.get("low_clip_ratio", {"center": 0.0, "robust_sigma": 0.01})
    high_clip_stat = stats.get("high_clip_ratio", {"center": 0.0, "robust_sigma": 0.001})
    low_clip = z_score_to_100(
        features["low_clip_ratio"],
        low_clip_stat,
        higher_is_better=False,
        points_per_sigma=35.0,
    )
    high_clip = z_score_to_100(
        features["high_clip_ratio"],
        high_clip_stat,
        higher_is_better=False,
        points_per_sigma=50.0,
    )
    coupling_condition = 0.45 * corr + 0.25 * peak + 0.15 * low_clip + 0.15 * high_clip

    if pipe_state == "empty_or_lost_acoustic_path_candidate" or "empty_pipe" in conditions:
        coupling_condition = min(coupling_condition, 35.0)
    elif pipe_state == "weak_signal_or_air_candidate":
        coupling_condition = min(coupling_condition, 65.0)
    elif pipe_state == "adc_saturation_or_bias_issue":
        coupling_condition = min(coupling_condition, 55.0)

    temporal_stability = 100.0
    if air_corr_std is not None and air_corr_threshold is not None and air_corr_threshold > 0:
        ratio = max(float(air_corr_std) / float(air_corr_threshold), 0.0)
        temporal_stability = clamp(100.0 - max(ratio - 0.5, 0.0) * 80.0)
    if "air_bubble" in conditions:
        temporal_stability = min(temporal_stability, 55.0)

    telemetry_reliability = sq_to_100(metadata.get("sq"))
    telemetry_reliability -= age_penalty(metadata.get("sq_age_ms"), stale_ms=5 * 60 * 1000, max_penalty=30.0)
    telemetry_reliability -= age_penalty(metadata.get("pub_dt_age_ms"), stale_ms=5000.0, max_penalty=20.0)
    if metadata.get("sq_label") == "unknown":
        telemetry_reliability = min(telemetry_reliability, 60.0)
    telemetry_reliability = clamp(telemetry_reliability)

    subscores = {
        "signal_integrity": round(clamp(signal_integrity), 1),
        "acoustic_pattern_match": round(clamp(pattern_match), 1),
        "coupling_condition": round(clamp(coupling_condition), 1),
        "temporal_stability": round(clamp(temporal_stability), 1),
        "telemetry_reliability": round(clamp(telemetry_reliability), 1),
    }

    score = sum(FLOW_HEALTH_WEIGHTS[k] * subscores[k] for k in FLOW_HEALTH_WEIGHTS)
    if label == "anomaly":
        score = min(score, 55.0)
    elif label == "suspect":
        score = min(score, 74.0)
    if "empty_pipe" in conditions:
        score = min(score, 35.0)
    if "air_bubble" in conditions:
        score = min(score, 65.0)

    label_text, color = band_for(score)
    drivers = [
        {
            "subscore": key,
            "score": value,
            "reason": reason_for_subscore(key, value),
        }
        for key, value in sorted(subscores.items(), key=lambda item: item[1])[:3]
    ]

    return {
        "version": FLOW_HEALTH_VERSION,
        "score": round(score, 1),
        "label": label_text,
        "color": color,
        "meaning": "measurement_trust",
        "subscores": subscores,
        "weights": FLOW_HEALTH_WEIGHTS,
        "drivers": drivers,
    }


def reason_for_subscore(key: str, value: float) -> str:
    if value >= 75:
        return "within healthy range"
    reasons = {
        "signal_integrity": "signal strength, SNR, or noise is outside the healthy baseline",
        "acoustic_pattern_match": "waveform pattern is drifting from learned healthy behavior",
        "coupling_condition": "coupling, clipping, or pipe-fill indicators are weak",
        "temporal_stability": "recent waveform match is unstable",
        "telemetry_reliability": "SQ or device telemetry is stale, missing, or low",
    }
    return reasons.get(key, "below healthy range")
