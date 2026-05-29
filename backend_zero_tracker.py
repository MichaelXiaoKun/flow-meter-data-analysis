"""Incremental zero-flow display computation for live meter frames.

This module ports the browser prototype's zero tracker into a backend-friendly
rolling state. It enriches each analyzer record once as it arrives so storage,
API queries, and SSE consumers can reuse the same computed fields without
reprocessing historical samples in the browser.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


M3H_TO_GPM = 4.4028675393
TEMP_FILTER_TAU_MIN = 0.75
TEMP_RATE_LIMIT_C_PER_MIN = 0.35
ZERO_ESTIMATE_SLEW_FS_PER_MIN = 0.006
CORRECTION_OVERRUN_MIN_GPM = 0.08
CORRECTION_OVERRUN_RATIO = 3.0
DISPLAY_GPM_NOISE_FLOOR = 0.02
DISPLAY_GPM_ZERO_HOLD = 0.05
SAMPLE_GAP_AFTER_MS = 20_000


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


def clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def clamp01(value: float) -> float:
    return clamp(value, 0.0, 1.0)


def limit_step(previous: float | None, target: float, max_step: float) -> float:
    if previous is None or not math.isfinite(previous) or not math.isfinite(target):
        return target
    return previous + clamp(target - previous, -abs(max_step), abs(max_step))


def median(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return 0.5 * (ordered[mid - 1] + ordered[mid])


def pipe_area_m2(outer_diameter_mm: float, wall_thickness_mm: float) -> float:
    outer = max(float(outer_diameter_mm), 1.0)
    wall = max(float(wall_thickness_mm), 0.0)
    inner_m = max(outer - 2.0 * wall, 0.1) / 1000.0
    return math.pi * inner_m * inner_m / 4.0


def fs_to_fr(fs_mps: float, area_m2: float) -> float:
    return float(fs_mps) * float(area_m2) * 3600.0


def fr_to_fs(fr_m3h: float, area_m2: float) -> float:
    return float(fr_m3h) / max(float(area_m2) * 3600.0, 1e-9)


def m3h_to_gpm(fr_m3h: float) -> float:
    return float(fr_m3h) * M3H_TO_GPM


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


def env_float(name: str, default: float) -> float:
    return finite_number(os.environ.get(name)) or default


def env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class ZeroTrackerConfig:
    pipe_outer_diameter_mm: float = 26.67
    pipe_wall_thickness_mm: float = 2.87
    deadband_fr_m3h: float = 0.030
    temp_coeff_fs: float = 0.0020
    tracker_alpha: float = 0.045
    alert_threshold: float = 0.70
    suppress: bool = True
    feedback: bool = True
    split_cooling: bool = True

    @classmethod
    def from_env(cls) -> "ZeroTrackerConfig":
        return cls(
            pipe_outer_diameter_mm=env_float("ZERO_TRACKER_PIPE_OD_MM", cls.pipe_outer_diameter_mm),
            pipe_wall_thickness_mm=env_float("ZERO_TRACKER_PIPE_WALL_MM", cls.pipe_wall_thickness_mm),
            deadband_fr_m3h=env_float("ZERO_TRACKER_DEADBAND_M3H", cls.deadband_fr_m3h),
            temp_coeff_fs=env_float("ZERO_TRACKER_TEMP_COEFF_FS", cls.temp_coeff_fs),
            tracker_alpha=env_float("ZERO_TRACKER_ALPHA", cls.tracker_alpha),
            alert_threshold=env_float("ZERO_TRACKER_ALERT_THRESHOLD", cls.alert_threshold),
            suppress=env_bool("ZERO_TRACKER_SUPPRESS", cls.suppress),
            feedback=env_bool("ZERO_TRACKER_FEEDBACK", cls.feedback),
            split_cooling=env_bool("ZERO_TRACKER_SPLIT_COOLING", cls.split_cooling),
        )

    @property
    def area_m2(self) -> float:
        return pipe_area_m2(self.pipe_outer_diameter_mm, self.pipe_wall_thickness_mm)

    @property
    def deadband_fs_mps(self) -> float:
        return fr_to_fs(self.deadband_fr_m3h, self.area_m2)


@dataclass
class SerialZeroState:
    sample_count: int = 0
    thermal_reference_c: float | None = None
    warm_thermal_temps: list[float] = field(default_factory=list)
    filtered_temp_c: float | None = None
    last_filtered_temp_c: float | None = None
    last_timestamp: datetime | None = None
    previous_zero_estimate_fs: float | None = None
    adaptive_fs: float = 0.0
    zero_stability: float = 0.0
    no_flow_run: int = 0


class BackendZeroTracker:
    """Per-serial incremental tracker for backend-computed display fields."""

    def __init__(self, config: ZeroTrackerConfig | None = None) -> None:
        self.config = config or ZeroTrackerConfig()
        self.states: dict[str, SerialZeroState] = {}

    @classmethod
    def from_env(cls) -> "BackendZeroTracker":
        return cls(ZeroTrackerConfig.from_env())

    def get_state(self, serial: str) -> SerialZeroState:
        if serial not in self.states:
            self.states[serial] = SerialZeroState()
        return self.states[serial]

    def enrich(self, record: dict[str, Any]) -> dict[str, Any]:
        serial = str(record.get("serial") or "_unknown_")
        state = self.get_state(serial)
        computed = self.compute(record, state)
        record.update(computed)
        return record

    def compute(self, record: dict[str, Any], state: SerialZeroState) -> dict[str, Any]:
        config = self.config
        features = record.get("features") if isinstance(record.get("features"), dict) else {}
        area_m2 = first_finite(
            record.get("pipe_area_from_geometry_m2"),
            record.get("pipe_area_m2"),
            record.get("pipe_area"),
        ) or config.area_m2
        deadband_fs = fr_to_fs(config.deadband_fr_m3h, area_m2)

        raw_fs_mps = first_finite(
            record.get("raw_fs_mps"),
            record.get("pub_fs_mps"),
            record.get("raw_flow_speed_mps"),
            record.get("zero_corr_fs_mps"),
            record.get("corr_fs_mps"),
        )
        raw_fr_m3h = first_finite(
            record.get("raw_fr_m3h"),
            record.get("pub_fr_m3h"),
            record.get("raw_flow_rate_m3h"),
            record.get("zero_corr_fr_m3h"),
            record.get("corr_fr_m3h"),
        )
        if raw_fs_mps is None and raw_fr_m3h is not None:
            raw_fs_mps = fr_to_fs(raw_fr_m3h, area_m2)
        if raw_fs_mps is None:
            raw_fs_mps = 0.0
        if raw_fr_m3h is None:
            raw_fr_m3h = fs_to_fr(raw_fs_mps, area_m2)

        ots_temp_c = first_finite(record.get("onboard_temperature_c"), record.get("ots"), record.get("ots_temp_c"))
        fallback_temp_c = first_finite(record.get("temperature_c"), ots_temp_c, 25.0) or 25.0
        raw_thermal_temp_c = ots_temp_c if ots_temp_c is not None else fallback_temp_c

        timestamp = parse_timestamp(record.get("timestamp") or record.get("server_timestamp"))
        minutes = 0.001
        source_gap_ms = 0.0
        if timestamp is not None and state.last_timestamp is not None:
            source_gap_ms = max((timestamp - state.last_timestamp).total_seconds() * 1000.0, 0.0)
            minutes = max(source_gap_ms / 60000.0, 0.001)
        data_gap = source_gap_ms > SAMPLE_GAP_AFTER_MS if state.sample_count else False

        if state.filtered_temp_c is None or not math.isfinite(state.filtered_temp_c):
            state.filtered_temp_c = raw_thermal_temp_c
        if state.last_filtered_temp_c is None or not math.isfinite(state.last_filtered_temp_c):
            state.last_filtered_temp_c = state.filtered_temp_c

        if len(state.warm_thermal_temps) < 12:
            state.warm_thermal_temps.append(raw_thermal_temp_c)
            state.thermal_reference_c = median(state.warm_thermal_temps)
        if state.thermal_reference_c is None:
            state.thermal_reference_c = raw_thermal_temp_c

        index = state.sample_count
        temp_alpha = 1.0 if index == 0 else clamp(1.0 - math.exp(-minutes / TEMP_FILTER_TAU_MIN), 0.015, 0.35)
        state.filtered_temp_c += (raw_thermal_temp_c - state.filtered_temp_c) * temp_alpha
        thermal_temp_c = state.filtered_temp_c
        d_temp = thermal_temp_c - state.last_filtered_temp_c
        temp_rate_raw = 0.0 if index == 0 else d_temp / minutes
        temp_rate = clamp(temp_rate_raw, -TEMP_RATE_LIMIT_C_PER_MIN, TEMP_RATE_LIMIT_C_PER_MIN)
        thermal_delta_c = thermal_temp_c - state.thermal_reference_c
        thermal_gradient_c = None if ots_temp_c is None else raw_thermal_temp_c - thermal_temp_c
        thermal_gradient_score = 0.0 if thermal_gradient_c is None else clamp01(abs(thermal_gradient_c) / 12.0)

        signed_rate_coeff = config.temp_coeff_fs * 1.45 if config.split_cooling and temp_rate < 0 else config.temp_coeff_fs
        temp_model_fs = config.temp_coeff_fs * thermal_delta_c + signed_rate_coeff * 1.8 * temp_rate
        max_zero_step_fs = max(0.00005, ZERO_ESTIMATE_SLEW_FS_PER_MIN * minutes)
        estimate_before_target_fs = temp_model_fs + state.adaptive_fs
        estimate_before_fs = estimate_before_target_fs if index == 0 else limit_step(
            state.previous_zero_estimate_fs,
            estimate_before_target_fs,
            max_zero_step_fs,
        )
        corrected_before_fs = raw_fs_mps - estimate_before_fs

        source_type = record.get("source_type") or record.get("sourceType") or ""
        is_pub_only = source_type == "pub_only" or record.get("waveform_supported") is False
        template_corr = first_finite(features.get("template_corr"), 0.997 if is_pub_only else 0.9965) or 0.9965
        noise_rms = first_finite(features.get("noise_rms_v"), 0.25) or 0.25
        snr_db = first_finite(features.get("snr_db"), 12.2) or 12.2

        waveform_drift = clamp01(
            abs(temp_rate) / 0.18 * 0.34
            + max(0.0, 0.997 - template_corr) / 0.004 * 0.30
            + max(0.0, noise_rms - 0.25) / 0.06 * 0.20
            + thermal_gradient_score * 0.16
        )
        low_flow_evidence = clamp01(1.0 - abs(corrected_before_fs) / max(deadband_fs * 5.0, 0.0001))
        raw_small_enough = clamp01(1.0 - abs(raw_fs_mps) / max(deadband_fs * 8.0, 0.0001))
        event_evidence = clamp01(
            abs(corrected_before_fs) / max(deadband_fs * 7.0, 0.0001) * 0.60
            + max(0.0, template_corr - 0.995) / 0.004 * 0.25
            + max(0.0, snr_db - 12.1) / 1.0 * 0.15
        )
        zero_probability = clamp01(0.55 * low_flow_evidence + 0.25 * raw_small_enough + 0.20 * (1.0 - event_evidence))
        phantom_probability = clamp01(
            0.48 * waveform_drift
            + 0.31 * low_flow_evidence
            + 0.11 * raw_small_enough
            + 0.16 * thermal_gradient_score
            - 0.24 * event_evidence
        )
        event_probability = clamp01(event_evidence - 0.35 * phantom_probability)

        if data_gap:
            zero_probability = 0.0
            phantom_probability = 0.0
            event_probability = 0.0

        raw_near_zero_before = abs(raw_fs_mps) <= deadband_fs * 0.65 or abs(m3h_to_gpm(raw_fr_m3h)) <= 0.03
        correction_before_amplification = abs(corrected_before_fs) / max(abs(raw_fs_mps), deadband_fs * 0.12, 0.00025)
        preliminary_correction_overrun = (
            raw_near_zero_before
            and abs(m3h_to_gpm(fs_to_fr(corrected_before_fs, area_m2))) >= CORRECTION_OVERRUN_MIN_GPM
            and correction_before_amplification >= CORRECTION_OVERRUN_RATIO
            and event_probability < 0.62
        )
        temp_stable_enough = abs(temp_rate_raw) <= TEMP_RATE_LIMIT_C_PER_MIN * 2.5
        can_learn = (
            not data_gap
            and not preliminary_correction_overrun
            and temp_stable_enough
            and config.feedback
            and zero_probability > 0.82
            and event_probability < 0.22
            and abs(corrected_before_fs) < deadband_fs * 2.2
        )
        if can_learn:
            residual_fs = raw_fs_mps - temp_model_fs
            state.adaptive_fs = state.adaptive_fs * (1.0 - config.tracker_alpha) + residual_fs * config.tracker_alpha
            state.zero_stability = min(1.0, state.zero_stability + 0.08)
            state.no_flow_run += 1
        else:
            state.adaptive_fs *= 0.995
            state.zero_stability = max(0.0, state.zero_stability - 0.04)
            state.no_flow_run = 0

        zero_estimate_target_fs = temp_model_fs + state.adaptive_fs
        zero_estimate_fs = zero_estimate_target_fs if index == 0 else limit_step(
            state.previous_zero_estimate_fs,
            zero_estimate_target_fs,
            max_zero_step_fs,
        )
        state.adaptive_fs = zero_estimate_fs - temp_model_fs
        corrected_fs_mps = raw_fs_mps - zero_estimate_fs
        correction_amplification = abs(corrected_fs_mps) / max(abs(raw_fs_mps), deadband_fs * 0.12, 0.00025)
        raw_near_zero = abs(raw_fs_mps) <= deadband_fs * 0.65 or abs(m3h_to_gpm(raw_fr_m3h)) <= 0.03
        corrected_guard_gpm = abs(m3h_to_gpm(fs_to_fr(corrected_fs_mps, area_m2)))
        correction_overrun = (
            raw_near_zero
            and corrected_guard_gpm >= CORRECTION_OVERRUN_MIN_GPM
            and correction_amplification >= CORRECTION_OVERRUN_RATIO
            and event_probability < 0.62
        )
        if correction_overrun:
            zero_probability = max(zero_probability, 0.82)
            phantom_probability = max(phantom_probability, 0.86)
            event_probability = min(event_probability, 0.18)

        published_fs_mps = corrected_fs_mps
        if config.suppress and (
            correction_overrun
            or (phantom_probability > config.alert_threshold and abs(corrected_fs_mps) < deadband_fs * 3.0)
        ):
            published_fs_mps = 0.0
        auto_corrected = published_fs_mps == 0.0 and corrected_fs_mps != 0.0 and (
            correction_overrun or phantom_probability > config.alert_threshold
        )

        zero_estimate_fr_m3h = fs_to_fr(zero_estimate_fs, area_m2)
        corrected_fr_m3h = fs_to_fr(corrected_fs_mps, area_m2)
        published_fr_m3h = fs_to_fr(published_fs_mps, area_m2)
        raw_gpm = m3h_to_gpm(raw_fr_m3h)
        zero_estimate_gpm = m3h_to_gpm(zero_estimate_fr_m3h)
        corrected_gpm = m3h_to_gpm(corrected_fr_m3h)
        published_gpm = m3h_to_gpm(published_fr_m3h)
        displayed_fr_m3h = max(0.0, published_fr_m3h)
        displayed_gpm = max(0.0, published_gpm)
        display_suppression_reason = ""
        zero_like_display = zero_probability > 0.72 and event_probability < 0.35
        if displayed_gpm > 0.0 and displayed_gpm < DISPLAY_GPM_NOISE_FLOOR:
            displayed_gpm = 0.0
            displayed_fr_m3h = 0.0
            display_suppression_reason = "below_display_noise_floor"
        elif displayed_gpm > 0.0 and zero_like_display and displayed_gpm < DISPLAY_GPM_ZERO_HOLD:
            displayed_gpm = 0.0
            displayed_fr_m3h = 0.0
            display_suppression_reason = "zero_hold_low_positive_residual"

        measurement_confidence = clamp01(0.96 - 0.45 * phantom_probability + 0.25 * event_probability - 0.18 * waveform_drift)
        state_name = classify_state(
            data_gap=data_gap,
            phantom_probability=phantom_probability,
            event_probability=event_probability,
            zero_probability=zero_probability,
            corrected_fs_mps=corrected_fs_mps,
            correction_overrun=correction_overrun,
            deadband_fs_mps=deadband_fs,
        )

        state.last_filtered_temp_c = thermal_temp_c
        state.last_timestamp = timestamp or state.last_timestamp
        state.previous_zero_estimate_fs = zero_estimate_fs
        state.sample_count += 1

        health = record.get("flow_meter_health") if isinstance(record.get("flow_meter_health"), dict) else {}
        quality_status = (
            record.get("quality_status")
            or record.get("sq_label")
            or record.get("label")
            or health.get("label")
        )
        return {
            "backend_computed": True,
            "zero_tracker_version": "python_incremental_v1",
            "raw_fs_mps": raw_fs_mps,
            "raw_fr_m3h": raw_fr_m3h,
            "raw_gpm": raw_gpm,
            "temperature_c": fallback_temp_c,
            "ots_temp_c": ots_temp_c,
            "raw_thermal_temp_c": raw_thermal_temp_c,
            "thermal_temp_c": thermal_temp_c,
            "thermal_delta_c": thermal_delta_c,
            "thermal_gradient_c": thermal_gradient_c,
            "thermal_gradient_score": thermal_gradient_score,
            "temp_rate_c_per_min": temp_rate,
            "temp_rate_raw_c_per_min": temp_rate_raw,
            "temp_model_fs": temp_model_fs,
            "adaptive_zero_fs": state.adaptive_fs,
            "zero_estimate_fs": zero_estimate_fs,
            "zero_estimate_fr_m3h": zero_estimate_fr_m3h,
            "zero_estimate_gpm": zero_estimate_gpm,
            "corrected_fs_mps": corrected_fs_mps,
            "corrected_fr_m3h": corrected_fr_m3h,
            "corrected_gpm": corrected_gpm,
            "published_fs_mps": published_fs_mps,
            "published_fr_m3h": published_fr_m3h,
            "published_gpm": published_gpm,
            "displayed_fr_m3h": displayed_fr_m3h,
            "displayed_gpm": displayed_gpm,
            "auto_corrected": auto_corrected,
            "display_suppression_reason": display_suppression_reason,
            "correction_amplification": correction_amplification,
            "correction_overrun": correction_overrun,
            "waveform_drift": waveform_drift,
            "zero_probability": zero_probability,
            "phantom_probability": phantom_probability,
            "event_probability": event_probability,
            "measurement_confidence": measurement_confidence,
            "zero_stability": state.zero_stability,
            "no_flow_run": state.no_flow_run,
            "state_name": state_name,
            "quality_status": quality_status,
            "data_gap": data_gap,
            "source_gap_ms": source_gap_ms,
        }


def classify_state(
    *,
    data_gap: bool,
    phantom_probability: float,
    event_probability: float,
    zero_probability: float,
    corrected_fs_mps: float,
    correction_overrun: bool,
    deadband_fs_mps: float,
) -> str:
    if data_gap:
        return "data_gap"
    if correction_overrun:
        return "compensation_overrun"
    if event_probability > 0.72 and abs(corrected_fs_mps) > deadband_fs_mps * 2.5:
        return "event_flow"
    if phantom_probability > 0.72:
        return "phantom_risk"
    if zero_probability > 0.78 and abs(corrected_fs_mps) <= deadband_fs_mps * 2.0:
        return "zero_flow"
    return "review"
