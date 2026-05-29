#!/usr/bin/env python3
"""Analyze meter ADC waveforms from MQTT and optionally self-adapt safely.

This analyzer mirrors the live ingest pipeline used by ``bluebot-rainbird-test-gui``
so that the self-training model sees the same waveforms and the same
signal-quality labels as the dashboard's CSV capture path.

Topic structure (matches the GUI's ``server.js``):

* ``meter/sig/<NUI>`` — waveform frames. Payload is *either* JSON like
  ``{"numbers": [1.4, 1.3, ...], "format": "legacy_hex"}`` *or* raw bytes,
  in which case each byte is decoded as ``int(byte, 16) / 10`` (the device's
  legacy 8‑bit ADC encoding).
* ``meter/pub/<NUI>`` and ``processed/meter/<NUI>`` — diagnostic publish
  carrying ``diagnose.sq`` (0–100 signal quality) plus flow/temperature fields
  when present. The analyzer tracks the most-recent SQ per serial with a
  5‑minute freshness window — the same rule the GUI uses — and uses it to
  auto-label captures and gate self-training. Devices without waveform support
  still emit a lightweight ``pub_only`` live record from this stream.

A single waveform frame produces:

* an analysis record (printed / written to ``--log-jsonl``)
* optional CSV row in the GUI's exact capture format (``--save-csv``)
* a self-training update *only* when the SQ-derived label is ``good`` *and*
  every acoustic gate from the original conservative policy passes.

Backwards compatibility: payloads that use ``samples``, ``waveform`` or
``s_0…s_N`` keys are still accepted, and ``--stdin`` JSONL mode is unchanged.
"""

from __future__ import annotations

import argparse
import copy
import csv
import json
import math
import os
import smtplib
import ssl
import sys
import time
import uuid
from collections import deque
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from cnn_embedding_runtime import CnnEmbeddingScorer
from flow_meter_health import compute_realtime_flow_health
from train_oneclass_meter_model import (
    FEATURES,
    anomaly_score,
    extract_features,
    label_from_score,
    peak_mode_from_features,
    top_reasons,
)


ADC_SAMPLE_RATE_HZ = 100_000_000
MAX_DYNAMIC_SERIALS = 10
PUB_ONLY_SIG_GRACE_S = 15.0
PUB_ONLY_INITIAL_WAIT_S = 4.0
M3H_TO_GPM = 4.4028675393

# Matches server.js: SQ readings older than this are treated as "no sq"
# rather than carrying stale labels onto fresh waveforms.
SQ_FRESHNESS_MS = 5 * 60 * 1000

# Matches server.js defaults for labelFromSq().
DEFAULT_THR_POOR = 50.0
DEFAULT_THR_FAIR = 80.0


# ── Signal-quality tracking (mirrors server.js latestSqBySerial) ──────────────


class SqTracker:
    """Most-recent pub-side diagnostic / flow metadata per device.

    Tracks ``diagnose.sq`` and selected ``flow`` / correction fields such as
    the onboard temperature sensor ``ots``.
    SQ uses a freshness window because it is shown as live quality. Pub flow
    metadata is also timestamped so each waveform can report how old the
    attached pub snapshot is.

    Some deployments may also provide a separate device-side pub timestamp in
    nanoseconds. That timestamp is tracked as ``dt_ns`` when present, but it is
    not the same as firmware ``diagnose.dt``. In the observed payload,
    ``diagnose.dt`` is the transducer time difference in ns and
    ``diagnose.tt`` is the transducer total time in ns.
    """

    def __init__(self, freshness_ms: int = SQ_FRESHNESS_MS) -> None:
        # key -> {"sq": float, "sq_ts_ms": float, "dt_ns": int|None,
        #         "dt_ts_ms": float|None, "prev_dt_ns": int|None,
        #         "pub_meta": dict|None, "pub_meta_ts_ms": float|None}
        self._latest: dict[str, dict[str, Any]] = {}
        self._freshness_ms = freshness_ms

    def update(
        self,
        key: str,
        sq: float | None,
        dt_ns: int | None = None,
        pub_meta: dict[str, Any] | None = None,
    ) -> None:
        now_ms = time.time() * 1000.0
        entry = self._latest.setdefault(key, {})
        if sq is not None:
            entry["sq"] = float(sq)
            entry["sq_ts_ms"] = now_ms
        if dt_ns is not None:
            entry["prev_dt_ns"] = entry.get("dt_ns")
            entry["dt_ns"] = int(dt_ns)
            entry["dt_ts_ms"] = now_ms
        if pub_meta:
            entry["pub_meta"] = dict(pub_meta)
            entry["pub_meta_ts_ms"] = now_ms

    def get(self, key: str) -> tuple[float | None, float | None]:
        entry = self._latest.get(key)
        if entry is None or "sq" not in entry:
            return None, None
        age = time.time() * 1000.0 - entry["sq_ts_ms"]
        if age > self._freshness_ms:
            return None, None
        return entry["sq"], age

    def get_dt(self, key: str) -> tuple[int | None, float | None, int | None]:
        """Return (dt_ns, dt_age_ms, dt_delta_ns_since_prev_pub).

        dt_age_ms is the wall-clock age of the most recent dt reading.
        dt_delta_ns is the nanosecond difference between the latest dt and
        the previous pub's dt — useful for spotting a stuck measurement
        loop (delta == 0 across many pubs) without depending on wall-clock.
        """
        entry = self._latest.get(key)
        if entry is None or entry.get("dt_ns") is None:
            return None, None, None
        dt_ns = entry["dt_ns"]
        dt_ts_ms = entry.get("dt_ts_ms")
        dt_age_ms = (time.time() * 1000.0 - dt_ts_ms) if dt_ts_ms is not None else None
        prev = entry.get("prev_dt_ns")
        dt_delta = (dt_ns - prev) if prev is not None else None
        return dt_ns, dt_age_ms, dt_delta

    def get_pub_metadata(self, key: str) -> tuple[dict[str, Any], float | None]:
        entry = self._latest.get(key)
        if entry is None or not isinstance(entry.get("pub_meta"), dict):
            return {}, None
        ts_ms = entry.get("pub_meta_ts_ms")
        age_ms = (time.time() * 1000.0 - ts_ms) if ts_ms is not None else None
        return dict(entry["pub_meta"]), age_ms

    def label(self, key: str, thr_poor: float, thr_fair: float) -> str:
        sq, _ = self.get(key)
        if sq is None or not math.isfinite(sq):
            return "unknown"
        if sq < thr_poor:
            return "poor"
        if sq < thr_fair:
            return "fair"
        return "good"


def extract_sq_from_pub(parsed: Any) -> float | None:
    """Pull ``diagnose.sq`` out of an arbitrary pub payload shape.

    Mirrors ``extractSqFromPubPayload`` in server.js.
    """
    if not isinstance(parsed, dict):
        return None
    candidates = []
    for key in ("diagnose", "diagnostic", "diag"):
        node = parsed.get(key)
        if isinstance(node, dict):
            candidates.append(node.get("sq"))
    payload = parsed.get("payload")
    if isinstance(payload, dict):
        diag = payload.get("diagnose")
        if isinstance(diag, dict):
            candidates.append(diag.get("sq"))
    for value in candidates:
        if value is None:
            continue
        try:
            n = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(n):
            return n
    return None


def as_finite_float(raw: Any) -> float | None:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def pipe_area_from_outer_wall(pd_mm: float, pt_mm: float) -> tuple[float, float] | None:
    """Return (inner_diameter_mm, area_m2) from pipe outer diameter and wall."""
    inner_mm = pd_mm - 2.0 * pt_mm
    if inner_mm <= 0:
        return None
    inner_m = inner_mm / 1000.0
    return inner_mm, math.pi * inner_m * inner_m / 4.0


def extract_pub_flow_metadata(parsed: Any) -> dict[str, Any]:
    """Extract selected flow / correction fields from a ``meter/pub`` payload.

    The returned names are canonical and unit-bearing where possible:

    * ``pub_fs_mps`` and ``pub_fr_m3h`` are the firmware's raw flow speed/rate.
    * ``pub_tfs_mps`` and ``pub_tfr_m3h`` are firmware total/processed variants
      when present.
    * ``ots`` / ``onboard_temperature_c`` is the firmware onboard temperature
      sensor in degrees C.
    """
    if not isinstance(parsed, dict):
        return {}

    root = parsed.get("payload") if isinstance(parsed.get("payload"), dict) else parsed
    if not isinstance(root, dict):
        return {}

    out: dict[str, Any] = {}
    pipe = root.get("pipe")
    if isinstance(pipe, dict):
        pd = as_finite_float(pipe.get("pd"))
        pt = as_finite_float(pipe.get("pt"))
        if pd is not None:
            out["pipe_outer_diameter_mm"] = pd
        if pt is not None:
            out["pipe_wall_thickness_mm"] = pt
        if pd is not None and pt is not None:
            geometry = pipe_area_from_outer_wall(pd, pt)
            if geometry is not None:
                inner_mm, area_m2 = geometry
                out["pipe_inner_diameter_mm"] = inner_mm
                out["pipe_area_from_geometry_m2"] = area_m2

    flow = root.get("flow")
    if isinstance(flow, dict):
        mapping = {
            "fs": "pub_fs_mps",
            "fr": "pub_fr_m3h",
            "tfs": "pub_tfs_mps",
            "tfr": "pub_tfr_m3h",
            "ft": "pub_flow_total_m3",
            "ots": "onboard_temperature_c",
        }
        for source, target in mapping.items():
            value = as_finite_float(flow.get(source))
            if value is not None:
                out[target] = value
                if source == "ots":
                    out["ots"] = value

    raw = root.get("raw")
    if isinstance(raw, dict):
        for source, target in (
            ("flow_speed", "raw_flow_speed_mps"),
            ("flow_rate", "raw_flow_rate_m3h"),
            ("flow_total", "raw_flow_total_m3"),
        ):
            value = as_finite_float(raw.get(source))
            if value is not None:
                out[target] = value

    corrected = root.get("corrected")
    if isinstance(corrected, dict):
        for source, target in (
            ("zero_corr_fs", "zero_corr_fs_mps"),
            ("zero_corr_fr", "zero_corr_fr_m3h"),
            ("corr_fs", "corr_fs_mps"),
            ("corr_fr", "corr_fr_m3h"),
            ("flow_speed_zero_offset", "flow_speed_zero_offset_mps"),
            ("low_flow_cutoff", "low_flow_cutoff_mps"),
            ("pipe_area", "pipe_area_m2"),
        ):
            value = as_finite_float(corrected.get(source))
            if value is not None:
                out[target] = value

    measure_config = root.get("measure_config")
    if isinstance(measure_config, dict):
        for source, target in (
            ("zero", "measure_zero"),
            ("lfc", "measure_lfc"),
            ("ledlfc", "measure_ledlfc"),
            ("kf", "measure_kf"),
        ):
            value = as_finite_float(measure_config.get(source))
            if value is not None:
                out[target] = value

    diagnose = root.get("diagnose")
    if isinstance(diagnose, dict):
        for source, target in (("tt", "diagnose_tt_ns"), ("dt", "diagnose_dt_ns")):
            value = as_finite_float(diagnose.get(source))
            if value is not None:
                out[target] = value
                out[f"diagnose_{source}"] = value

    return out


def extract_dt_from_pub(parsed: Any) -> int | None:
    """Pull an optional device pub timestamp in nanoseconds from a pub payload.

    Do not read ``diagnose.dt`` here: firmware uses that field for transducer
    time difference. This function only accepts explicit timestamp-like fields.
    """
    if not isinstance(parsed, dict):
        return None
    root = parsed.get("payload") if isinstance(parsed.get("payload"), dict) else parsed
    if not isinstance(root, dict):
        return None
    raw = root.get("dt_ns") or root.get("pub_dt_ns") or root.get("device_dt_ns")
    if raw is None:
        return None
    try:
        v = int(raw.strip()) if isinstance(raw, str) else int(raw)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


# ── Payload parsing ────────────────────────────────────────────────────────────


def parse_sig_payload(payload: bytes | str) -> tuple[list[float], dict[str, Any]]:
    """Parse a ``meter/sig/`` payload the same way ``server.js`` does.

    1. Try JSON. If it's a list, treat as raw sample list. If it's an object,
       look for ``numbers`` (the GUI's canonical field), then ``samples``,
       then ``waveform``, then ``s_0…s_N`` keys.
    2. If JSON parsing fails, fall back to legacy hex: each byte is decoded
       as ``int(byte_hex, 16) / 10`` — i.e. each raw byte / 10.
    """
    metadata: dict[str, Any] = {}
    raw_bytes: bytes | None = None

    if isinstance(payload, bytes):
        raw_bytes = payload
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError:
            text = ""
    else:
        text = payload

    if text:
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = None
    else:
        data = None

    if data is None:
        # Legacy hex: each byte is one 8-bit ADC reading scaled by 1/10.
        if raw_bytes is None:
            raise ValueError("Non-JSON sig payload but no raw bytes available.")
        return [b / 10.0 for b in raw_bytes], {"format": "legacy_hex"}

    if isinstance(data, list):
        return [float(value) for value in data], metadata

    if not isinstance(data, dict):
        raise ValueError("Sig payload must be a JSON object, JSON list, or hex bytes.")

    for key in (
        "timestamp",
        "serial",
        "seq",
        "sq",
        "temperature_c",
        "tdc",
        "direction",
        "format",
    ):
        if key in data:
            metadata[key] = data[key]

    for key in ("ots", "OTS", "ots_temp_c", "onboard_temperature_c", "onboard_temp_c"):
        if key in data:
            metadata["ots"] = data[key]
            metadata["onboard_temperature_c"] = data[key]
            break

    if isinstance(data.get("numbers"), list):
        return [float(v) for v in data["numbers"]], metadata
    if isinstance(data.get("samples"), list):
        return [float(v) for v in data["samples"]], metadata
    if isinstance(data.get("waveform"), list):
        return [float(v) for v in data["waveform"]], metadata

    sample_pairs: list[tuple[int, float]] = []
    for key, value in data.items():
        if not isinstance(key, str) or not key.startswith("s_"):
            continue
        try:
            sample_pairs.append((int(key[2:]), float(value)))
        except (TypeError, ValueError):
            continue
    if sample_pairs:
        sample_pairs.sort()
        return [value for _, value in sample_pairs], metadata

    raise ValueError("No samples found. Expected numbers / samples / waveform / s_0… keys.")


# Old name kept so existing callers / stdin mode still work.
parse_payload = parse_sig_payload


def topic_id(topic: str) -> str:
    """Strip the known prefixes to recover the device's NUI / serial."""
    for prefix in ("processed/meter/", "meter/sig/", "meter/pub/"):
        if topic.startswith(prefix):
            return topic[len(prefix):]
    return topic.rsplit("/", 1)[-1]


def normalize_meter_serial(raw: Any) -> str:
    return "".join(ch for ch in str(raw or "").strip().upper() if ch.isalnum() or ch in {"_", "-"})


def load_subscription_serials(path: Path | None, limit: int = MAX_DYNAMIC_SERIALS) -> list[str]:
    if path is None or not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    raw_serials = payload.get("serials", []) if isinstance(payload, dict) else payload
    if not isinstance(raw_serials, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in raw_serials:
        serial = normalize_meter_serial(item)
        if not serial or serial in seen:
            continue
        seen.add(serial)
        out.append(serial)
        if len(out) >= limit:
            break
    return out


def expand_subscription_topics(templates: list[str], serials: list[str]) -> set[str]:
    topics: set[str] = set()
    for template in templates:
        if not template:
            continue
        if "{serial}" in template:
            topics.update(template.format(serial=serial) for serial in serials)
            continue
        parts = template.split("/")
        if "+" in parts:
            for serial in serials:
                topics.add("/".join(serial if part == "+" else part for part in parts))
            continue
        topics.add(template)
    return topics


# ── Optional CSV capture in the GUI's exact format ─────────────────────────────


class GuiCsvWriter:
    """Writes rows in the same shape as ``server.js`` ``appendFrameToCapture``,
    with optional trailing ``pub_dt_ns`` / ``pub_dt_age_ms`` / ``ots`` columns
    when the device payloads carry those fields.

    The first six columns and the ``s_*`` block are identical to what the
    GUI writes, so the offline trainers in this repo keep working unchanged.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.locked_n: int | None = None
        self.has_dt_columns: bool = False
        self.has_ots_column: bool = False
        # If the file already exists, lock N from its header so we can append
        # compatible rows — same behavior as server.js on resume.
        if path.exists():
            try:
                head = path.read_text().split("\n", 1)[0]
                cols = head.split(",")
                self.locked_n = sum(1 for c in cols if c.startswith("s_")) or None
                self.has_dt_columns = "pub_dt_ns" in cols
                self.has_ots_column = "ots" in cols
            except OSError:
                self.locked_n = None
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            self.has_ots_column = False

    def write(self, metadata: dict[str, Any], samples: list[float]) -> None:
        if not samples:
            return
        if self.locked_n is None:
            self.locked_n = len(samples)
            # Promote dt columns at file creation only if this row has them.
            self.has_dt_columns = metadata.get("pub_dt_ns") is not None
            self.has_ots_column = metadata.get("ots") is not None
            header = (
                ["timestamp", "serial", "sq", "sq_age_ms", "label", "n_samples"]
                + [f"s_{i}" for i in range(self.locked_n)]
            )
            if self.has_dt_columns:
                header += ["pub_dt_ns", "pub_dt_age_ms"]
            if self.has_ots_column:
                header += ["ots"]
            with self.path.open("w", newline="") as f:
                f.write(",".join(header) + "\n")

        n = self.locked_n
        s = samples[:n] if len(samples) > n else samples

        def cell(value: Any) -> str:
            if value is None:
                return ""
            if isinstance(value, float):
                if not math.isfinite(value):
                    return ""
                # Match the JS toString(): up to 17 digits but trim trailing zeros.
                return repr(value)
            return str(value)

        row = [
            cell(metadata.get("timestamp") or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())),
            cell(metadata.get("serial") or ""),
            cell(metadata.get("sq", "")),
            cell(metadata.get("sq_age_ms", "")),
            cell(metadata.get("sq_label") or "unknown"),
            cell(len(s)),
        ] + [cell(v) for v in s]

        while len(row) < 6 + n:
            row.append("")

        if self.has_dt_columns:
            row.append(cell(metadata.get("pub_dt_ns", "")))
            row.append(cell(metadata.get("pub_dt_age_ms", "")))
        if self.has_ots_column:
            row.append(cell(metadata.get("ots", "")))

        with self.path.open("a", newline="") as f:
            f.write(",".join(row) + "\n")


# ── Per-serial adaptive analyzer ──────────────────────────────────────────────


class PerSerialState:
    """Mutable training state for a single device.

    Each device gets its own copy of the profile (template, gate window,
    baseline) and the feature_stats (per-feature center / robust_sigma /
    percentiles). The rolling stability window is also per-device so an
    anomalous device cannot drag down another device's adaptation.
    """

    def __init__(
        self,
        base_model: dict[str, Any],
        stable_window: int,
        *,
        warm_state: dict[str, Any] | None = None,
    ) -> None:
        if warm_state is not None:
            self.profile = warm_state["profile"]
            self.feature_stats = warm_state["feature_stats"]
            self.mode_feature_stats = copy.deepcopy(
                warm_state.get("mode_feature_stats")
                or base_model.get("mode_feature_stats")
                or {}
            )
            self.updates = int(warm_state.get("updates", 0))
            self.last_seen = warm_state.get("last_seen")
        else:
            # Deep-copy so per-serial mutation never touches the base model.
            self.profile = copy.deepcopy(base_model["profile"])
            self.feature_stats = copy.deepcopy(base_model["feature_stats"])
            self.mode_feature_stats = copy.deepcopy(base_model.get("mode_feature_stats") or {})
            self.updates = 0
            self.last_seen = None
        self.stability: deque[bool] = deque(maxlen=stable_window)
        self.last_freeze_reason: str = "warming_up"
        self.samples_seen: int = 0
        # State-transition / heartbeat tracking.
        self.last_pipe_state: str | None = None
        self.last_raw_pipe_state: str | None = None
        self.pending_pipe_state: str | None = None
        self.pending_state_frames: int = 0
        self.state_entered_at: str | None = None
        self.state_frames: int = 0
        self.last_console_wall_ts: float = 0.0
        # Detection: pipe-state sliding window and confirmed conditions.
        # ``pipe_state_window`` / ``corr_history`` are resized lazily on first
        # analyze() since per-serial state doesn't know detection params yet.
        self.pipe_state_window: deque[str] = deque()
        self.corr_history: deque[float] = deque()
        # A device can be in multiple confirmed conditions at the same time
        # (e.g. weak coupling on top of intermittent air bubbles), so we use
        # a set rather than a single label.
        self.active_conditions: set[str] = set()
        self.condition_entered_at: dict[str, str] = {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile": self.profile,
            "feature_stats": self.feature_stats,
            "mode_feature_stats": self.mode_feature_stats,
            "updates": self.updates,
            "last_seen": self.last_seen,
        }


class AdaptiveAnalyzer:
    """Analyzer with a per-serial profile / feature_stats / stability window.

    The score thresholds and hyperparameters are shared (they're learned at
    train time on the pooled CSV), but each device's template and feature
    centers adapt independently. This matters because:

    * one bad device can no longer block another device's self-training
    * each meter's acoustic signature can drift independently (different
      pipe / temperature / install angle)
    """

    def __init__(
        self,
        model: dict[str, Any],
        *,
        self_train: bool,
        stable_window: int,
        template_alpha: float,
        center_alpha: float,
        require_sq_label_good: bool = True,
        corr_min_percentile: str = "p05",
        snr_min_percentile: str = "p05",
        gate_energy_min_percentile: str = "p05",
        stable_ratio_threshold: float = 0.6,
        score_headroom: float = 0.95,
        pub_dt_stale_ms: float | None = 5000.0,
        empty_window_m: int = 5,
        empty_window_n: int = 3,
        empty_recovery_n: int = 1,
        empty_include_weak: bool = True,
        air_window_n: int = 20,
        air_corr_std_mult: float = 5.0,
        air_recovery_factor: float = 0.5,
        state_enter_frames: int = 3,
        state_recover_frames: int = 5,
        cnn_health_weight: float = 0.0,
        cnn_scorer: CnnEmbeddingScorer | None = None,
    ) -> None:
        self.base_model = model
        self.self_train = self_train
        self.stable_window = stable_window
        self.template_alpha = template_alpha
        self.center_alpha = center_alpha
        self.require_sq_label_good = require_sq_label_good
        self.corr_min_percentile = corr_min_percentile
        self.snr_min_percentile = snr_min_percentile
        self.gate_energy_min_percentile = gate_energy_min_percentile
        self.stable_ratio_threshold = stable_ratio_threshold
        self.score_headroom = score_headroom
        self.pub_dt_stale_ms = pub_dt_stale_ms
        # N-of-M empty pipe confirmation:
        self.empty_window_m = max(1, int(empty_window_m))
        self.empty_window_n = max(1, int(empty_window_n))
        self.empty_recovery_n = max(0, int(empty_recovery_n))
        # If True, ``weak_signal_or_air_candidate`` also counts toward the
        # empty-pipe confirmation (more sensitive). If False, only the strong
        # ``empty_or_lost_acoustic_path_candidate`` does.
        self.empty_include_weak = bool(empty_include_weak)
        # Air-bubble / intermittent coupling detection:
        # Rolling window of recent template_corr values per serial. We trigger
        # when its sample stddev exceeds the training-time robust_sigma by a
        # multiplier — i.e. the device is more jittery than it was at train
        # time, even though individual frames may still look normal.
        self.air_window_n = max(4, int(air_window_n))
        self.air_corr_std_mult = float(air_corr_std_mult)
        self.air_recovery_factor = float(air_recovery_factor)
        self.state_enter_frames = max(1, int(state_enter_frames))
        self.state_recover_frames = max(1, int(state_recover_frames))
        self.cnn_health_weight = max(0.0, min(float(cnn_health_weight), 1.0))
        self.cnn_scorer = cnn_scorer
        # serial -> PerSerialState. Warm-load from model file if present.
        self.states: dict[str, PerSerialState] = {}
        for serial, warm in (model.get("serials") or {}).items():
            self.states[serial] = PerSerialState(model, stable_window, warm_state=warm)

    @property
    def thresholds(self) -> dict[str, float]:
        return self.base_model["score_thresholds"]

    def thresholds_for_mode(self, peak_mode: str) -> dict[str, float]:
        return (self.base_model.get("mode_score_thresholds") or {}).get(
            peak_mode,
            self.thresholds,
        )

    @property
    def total_updates(self) -> int:
        return sum(state.updates for state in self.states.values())

    def get_state(self, serial: str) -> PerSerialState:
        state = self.states.get(serial)
        if state is None:
            state = PerSerialState(self.base_model, self.stable_window)
            self.states[serial] = state
        return state

    def snapshot_for_save(self) -> dict[str, Any]:
        """Build a JSON-serializable model dict with per-serial adapted state."""
        out = dict(self.base_model)
        out["serials"] = {serial: state.to_dict() for serial, state in self.states.items()}
        return out

    def feature_stats_for_mode(self, state: PerSerialState, peak_mode: str) -> dict:
        return state.mode_feature_stats.get(peak_mode, state.feature_stats)

    def confirmed_pipe_state(
        self,
        state: PerSerialState,
        raw_pipe_state: str,
    ) -> tuple[str, dict[str, Any]]:
        """Debounce raw per-frame pipe_state into a customer-facing state."""
        state.last_raw_pipe_state = raw_pipe_state
        current = state.last_pipe_state
        normal_state = "normal_acoustic_state"
        if current is None:
            if raw_pipe_state == normal_state or self.state_enter_frames <= 1:
                state.pending_pipe_state = None
                state.pending_state_frames = 0
                return raw_pipe_state, {
                    "status": "initialized",
                    "required_frames": 1,
                    "pending_state": None,
                    "pending_frames": 0,
                }
            state.pending_pipe_state = raw_pipe_state
            state.pending_state_frames = 1
            return normal_state, {
                "status": "startup_waiting_for_confirmation",
                "required_frames": self.state_enter_frames,
                "pending_state": raw_pipe_state,
                "pending_frames": 1,
            }

        if raw_pipe_state == current:
            state.pending_pipe_state = None
            state.pending_state_frames = 0
            return current, {
                "status": "stable",
                "required_frames": 1,
                "pending_state": None,
                "pending_frames": 0,
            }

        required = (
            self.state_recover_frames
            if raw_pipe_state == normal_state
            else self.state_enter_frames
        )
        if state.pending_pipe_state == raw_pipe_state:
            state.pending_state_frames += 1
        else:
            state.pending_pipe_state = raw_pipe_state
            state.pending_state_frames = 1

        if state.pending_state_frames >= required:
            confirmed = raw_pipe_state
            state.pending_pipe_state = None
            state.pending_state_frames = 0
            return confirmed, {
                "status": "confirmed",
                "required_frames": required,
                "pending_state": None,
                "pending_frames": 0,
            }

        return current, {
            "status": "pending",
            "required_frames": required,
            "pending_state": state.pending_pipe_state,
            "pending_frames": state.pending_state_frames,
        }

    def analyze(self, samples: list[float], metadata: dict[str, Any]) -> dict[str, Any]:
        serial = metadata.get("serial") or "_unknown_"
        state = self.get_state(serial)
        state.samples_seen += 1
        state.last_seen = metadata.get("timestamp") or time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
        )

        features = extract_features(samples, state.profile)
        peak_mode = peak_mode_from_features(features)
        score_feature_stats = self.feature_stats_for_mode(state, peak_mode)
        score_thresholds = self.thresholds_for_mode(peak_mode)
        score, zscores = anomaly_score(features, score_feature_stats)
        label = label_from_score(score, score_thresholds)
        raw_pipe_state, diagnostic_reasons = self.pipe_state(
            state,
            features,
            zscores,
            label,
            score_feature_stats,
        )
        pipe_state, state_confirmation = self.confirmed_pipe_state(state, raw_pipe_state)
        quality = confidence_from_score(score, score_thresholds)

        # Track pipe_state transitions per serial. A "transition" is the first
        # frame whose confirmed pipe_state differs from the previously observed one.
        previous_state = state.last_pipe_state
        transitioned = previous_state is not None and previous_state != pipe_state
        if transitioned or state.last_pipe_state is None:
            state.state_entered_at = metadata.get("timestamp") or time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
            )
            state.state_frames = 1
        else:
            state.state_frames += 1
        state.last_pipe_state = pipe_state

        # Detection: N-of-M sliding window of confirmed pipe_state to confirm an
        # ``empty_pipe`` condition. Raw single-frame ``empty_or_lost_*`` labels
        # are noisy (transient bubbles, brief mounting wobble); the customer
        # condition should follow the same debounce path as console state.
        if state.pipe_state_window.maxlen != self.empty_window_m:
            # Reinitialize with the configured length (first frame, or if the
            # operator changed window size between sessions).
            state.pipe_state_window = deque(state.pipe_state_window, maxlen=self.empty_window_m)
        state.pipe_state_window.append(pipe_state)
        bad_states = {"empty_or_lost_acoustic_path_candidate"}
        if self.empty_include_weak:
            bad_states.add("weak_signal_or_air_candidate")
        bad_count = sum(1 for s in state.pipe_state_window if s in bad_states)

        detection_events: list[dict[str, Any]] = []
        now_iso = metadata.get("timestamp") or time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
        )

        # ── Detection A: sustained empty pipe ────────────────────────────
        if (
            "empty_pipe" not in state.active_conditions
            and len(state.pipe_state_window) >= self.empty_window_n
            and bad_count >= self.empty_window_n
        ):
            state.active_conditions.add("empty_pipe")
            state.condition_entered_at["empty_pipe"] = now_iso
            detection_events.append({
                "event": "empty_pipe_detected",
                "timestamp": now_iso,
                "serial": metadata.get("serial"),
                "window": f"{bad_count}/{len(state.pipe_state_window)}",
                "recent_states": list(state.pipe_state_window),
                "sq": metadata.get("sq"),
                "sq_label": metadata.get("sq_label"),
            })
        elif (
            "empty_pipe" in state.active_conditions
            and bad_count <= self.empty_recovery_n
        ):
            recovered_after = state.condition_entered_at.pop("empty_pipe", None)
            state.active_conditions.discard("empty_pipe")
            detection_events.append({
                "event": "pipe_refilled",
                "timestamp": now_iso,
                "serial": metadata.get("serial"),
                "window": f"{bad_count}/{len(state.pipe_state_window)}",
                "empty_since": recovered_after,
                "recent_states": list(state.pipe_state_window),
            })

        # ── Detection C: intermittent coupling / air bubbles ─────────────
        # Rolling stddev of template_corr; we compare against the training-time
        # robust_sigma of template_corr so the threshold scales with how
        # tight the trained device was.
        if state.corr_history.maxlen != self.air_window_n:
            state.corr_history = deque(state.corr_history, maxlen=self.air_window_n)
        state.corr_history.append(features["template_corr"])
        air_std = None
        air_threshold = None
        if len(state.corr_history) == self.air_window_n:
            mean_c = sum(state.corr_history) / self.air_window_n
            var = sum((c - mean_c) ** 2 for c in state.corr_history) / (self.air_window_n - 1)
            air_std = math.sqrt(max(var, 0.0))
            base_sigma = state.feature_stats["template_corr"]["robust_sigma"]
            air_threshold = self.air_corr_std_mult * base_sigma
            recovery_threshold = air_threshold * self.air_recovery_factor
            if "air_bubble" not in state.active_conditions and air_std >= air_threshold:
                state.active_conditions.add("air_bubble")
                state.condition_entered_at["air_bubble"] = now_iso
                detection_events.append({
                    "event": "air_bubble_detected",
                    "timestamp": now_iso,
                    "serial": metadata.get("serial"),
                    "corr_std": air_std,
                    "corr_std_threshold": air_threshold,
                    "corr_mean": mean_c,
                    "window_n": self.air_window_n,
                    "sq": metadata.get("sq"),
                    "sq_label": metadata.get("sq_label"),
                })
            elif "air_bubble" in state.active_conditions and air_std < recovery_threshold:
                bubbled_since = state.condition_entered_at.pop("air_bubble", None)
                state.active_conditions.discard("air_bubble")
                detection_events.append({
                    "event": "air_bubble_cleared",
                    "timestamp": now_iso,
                    "serial": metadata.get("serial"),
                    "corr_std": air_std,
                    "recovery_threshold": recovery_threshold,
                    "bubbling_since": bubbled_since,
                })

        stable_candidate, freeze_reason = self.is_safe_training_sample(
            state,
            features,
            score,
            label,
            metadata,
            feature_stats=score_feature_stats,
            thresholds=score_thresholds,
        )
        state.stability.append(stable_candidate)
        stable_ratio = sum(state.stability) / max(len(state.stability), 1)
        trained = False
        if (
            self.self_train
            and len(state.stability) == state.stability.maxlen
            and stable_ratio >= self.stable_ratio_threshold
            and stable_candidate
        ):
            self.update_state(state, samples, features)
            trained = True
            state.last_freeze_reason = ""
        else:
            state.last_freeze_reason = (
                freeze_reason if self.self_train else "self_training_disabled"
            )

        cnn_analysis = None
        if self.cnn_scorer is not None:
            try:
                cnn_analysis = self.cnn_scorer.score(samples)
            except Exception as exc:  # noqa: BLE001
                cnn_analysis = {"error": str(exc)}

        flow_meter_health = compute_realtime_flow_health(
            features=features,
            feature_stats=score_feature_stats,
            anomaly_score=score,
            thresholds=score_thresholds,
            label=label,
            pipe_state=pipe_state,
            active_conditions=state.active_conditions,
            metadata=metadata,
            air_corr_std=air_std,
            air_corr_threshold=air_threshold,
            cnn=cnn_analysis if cnn_analysis and "error" not in cnn_analysis else None,
            cnn_weight=self.cnn_health_weight,
        )

        return {
            "timestamp": state.last_seen,
            "serial": metadata.get("serial"),
            "seq": metadata.get("seq", metadata.get("sq")),
            "sq": metadata.get("sq"),
            "sq_age_ms": metadata.get("sq_age_ms"),
            "sq_label": metadata.get("sq_label"),
            "pub_dt_ns": metadata.get("pub_dt_ns"),
            "pub_dt_age_ms": metadata.get("pub_dt_age_ms"),
            "pub_dt_delta_ns": metadata.get("pub_dt_delta_ns"),
            "pub_meta_age_ms": metadata.get("pub_meta_age_ms"),
            "ots": metadata.get("ots"),
            "onboard_temperature_c": metadata.get("onboard_temperature_c"),
            "pub_fs_mps": metadata.get("pub_fs_mps"),
            "pub_fr_m3h": metadata.get("pub_fr_m3h"),
            "pub_tfs_mps": metadata.get("pub_tfs_mps"),
            "pub_tfr_m3h": metadata.get("pub_tfr_m3h"),
            "zero_corr_fs_mps": metadata.get("zero_corr_fs_mps"),
            "zero_corr_fr_m3h": metadata.get("zero_corr_fr_m3h"),
            "corr_fs_mps": metadata.get("corr_fs_mps"),
            "corr_fr_m3h": metadata.get("corr_fr_m3h"),
            "diagnose_dt_ns": metadata.get("diagnose_dt_ns"),
            "diagnose_tt_ns": metadata.get("diagnose_tt_ns"),
            "pipe_outer_diameter_mm": metadata.get("pipe_outer_diameter_mm"),
            "pipe_wall_thickness_mm": metadata.get("pipe_wall_thickness_mm"),
            "pipe_inner_diameter_mm": metadata.get("pipe_inner_diameter_mm"),
            "pipe_area_from_geometry_m2": metadata.get("pipe_area_from_geometry_m2"),
            "pipe_area_m2": metadata.get("pipe_area_m2"),
            "score": score,
            "label": label,
            "peak_mode": peak_mode,
            "mode_aware": peak_mode in state.mode_feature_stats,
            "score_thresholds": score_thresholds,
            "raw_pipe_state": raw_pipe_state,
            "pipe_state": pipe_state,
            "measurement_confidence": quality,
            "flow_meter_health": flow_meter_health,
            "cnn_analysis": cnn_analysis,
            "cnn_health_weight": self.cnn_health_weight,
            "diagnostic_reasons": diagnostic_reasons,
            "features": features,
            "top_z_reasons": top_reasons(zscores),
            "self_training": {
                "enabled": self.self_train,
                "trained_this_sample": trained,
                "updates": state.updates,
                "total_updates": self.total_updates,
                "stable_ratio": stable_ratio,
                "freeze_reason": state.last_freeze_reason,
                "serial_known": serial in self.states,
            },
            "transitioned": transitioned,
            "previous_pipe_state": previous_state,
            "state_frames": state.state_frames,
            "state_entered_at": state.state_entered_at,
            "state_confirmation": state_confirmation,
            "pending_pipe_state": state.pending_pipe_state,
            "pending_state_frames": state.pending_state_frames,
            # Composite condition label so the existing log format keeps
            # working ("normal", "empty_pipe", "air_bubble",
            # "air_bubble+empty_pipe", ...).
            "condition": (
                "+".join(sorted(state.active_conditions))
                if state.active_conditions else "normal"
            ),
            "active_conditions": sorted(state.active_conditions),
            "condition_entered_at": dict(state.condition_entered_at),
            "detection_events": detection_events,
            "empty_window_count": bad_count,
            "empty_window_size": len(state.pipe_state_window),
            "air_corr_std": air_std,
            "air_corr_threshold": air_threshold,
        }

    def pipe_state(
        self,
        state: PerSerialState,
        features: dict[str, float],
        zscores: dict[str, float],
        label: str,
        feature_stats: dict | None = None,
    ) -> tuple[str, list[str]]:
        stats = feature_stats or state.feature_stats
        reasons: list[str] = []

        low_clip_limit = max(stats["low_clip_ratio"]["p99"] + 0.02, 0.08)
        high_clip_limit = max(stats["high_clip_ratio"]["p99"] + 0.005, 0.005)
        if features["low_clip_ratio"] > low_clip_limit:
            reasons.append("low_side_clipping_high")
        if features["high_clip_ratio"] > high_clip_limit:
            reasons.append("high_side_clipping_high")
        if reasons:
            return "adc_saturation_or_bias_issue", reasons

        weak_checks = []
        if features["gate_rms_v"] < stats["gate_rms_v"]["p01"]:
            weak_checks.append("gate_energy_low")
        if features["peak_abs_gate_v"] < stats["peak_abs_gate_v"]["p01"]:
            weak_checks.append("peak_low")
        if features["snr_db"] < stats["snr_db"]["p01"]:
            weak_checks.append("snr_low")
        if features["template_corr"] < stats["template_corr"]["p01"]:
            weak_checks.append("template_match_low")
        if len(weak_checks) >= 3:
            return "empty_or_lost_acoustic_path_candidate", weak_checks
        if len(weak_checks) >= 2:
            return "weak_signal_or_air_candidate", weak_checks

        if label == "anomaly":
            strongest = [item["feature"] for item in top_reasons(zscores, 3)]
            return "waveform_anomaly", strongest
        if label == "suspect":
            strongest = [item["feature"] for item in top_reasons(zscores, 3)]
            return "signal_quality_suspect", strongest
        return "normal_acoustic_state", []

    def is_safe_training_sample(
        self,
        state: PerSerialState,
        features: dict[str, float],
        score: float,
        label: str,
        metadata: dict[str, Any] | None = None,
        feature_stats: dict | None = None,
        thresholds: dict[str, float] | None = None,
    ) -> tuple[bool, str]:
        stats = feature_stats or state.feature_stats
        score_thresholds = thresholds or self.thresholds
        # Hard gate: if the GUI-style SQ label is anything but "good", freeze.
        # "unknown" still freezes — we never adapt without a fresh diagnose.sq
        # confirming the device thinks the read is good.
        if self.require_sq_label_good and metadata is not None:
            sq_label = metadata.get("sq_label")
            if sq_label is not None and sq_label != "good":
                return False, f"sq_label_{sq_label}"
        # Device-side dt (ns) freshness gate.
        if (
            self.pub_dt_stale_ms is not None
            and metadata is not None
            and metadata.get("pub_dt_age_ms") is not None
        ):
            if metadata["pub_dt_age_ms"] > self.pub_dt_stale_ms:
                return False, "pub_dt_stale"
        if label != "normal":
            return False, f"label_{label}"
        if score > score_thresholds["suspect"] * self.score_headroom:
            return False, "score_not_low_enough"
        corr_pct = self.corr_min_percentile
        if features["template_corr"] < stats["template_corr"][corr_pct]:
            return False, f"template_corr_below_{corr_pct}"
        snr_pct = self.snr_min_percentile
        if features["snr_db"] < stats["snr_db"][snr_pct]:
            return False, f"snr_below_{snr_pct}"
        gate_pct = self.gate_energy_min_percentile
        if features["gate_rms_v"] < stats["gate_rms_v"][gate_pct]:
            return False, f"gate_energy_below_{gate_pct}"
        if features["low_clip_ratio"] > max(stats["low_clip_ratio"]["p95"] + 0.01, 0.07):
            return False, "low_clipping_too_high"
        if features["high_clip_ratio"] > 0.001:
            return False, "high_clipping_present"
        return True, "waiting_for_stable_window"

    def update_state(
        self,
        state: PerSerialState,
        samples: list[float],
        features: dict[str, float],
    ) -> None:
        profile = state.profile
        gate_start = int(profile["gate_start"])
        gate_end = int(profile["gate_end"])
        baseline_samples = int(profile["baseline_samples"])
        baseline = median(samples[:baseline_samples])
        gate = [value - baseline for value in samples[gate_start:gate_end]]
        template = profile["template_gate"]
        for i, value in enumerate(gate):
            template[i] = (1.0 - self.template_alpha) * template[i] + self.template_alpha * value
        for name in FEATURES:
            stat = state.feature_stats[name]
            stat["center"] = (1.0 - self.center_alpha) * stat["center"] + self.center_alpha * features[name]
        state.updates += 1


def median(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return 0.5 * (ordered[mid - 1] + ordered[mid])


def confidence_from_score(score: float, thresholds: dict[str, float]) -> float:
    suspect = max(thresholds["suspect"], 1e-9)
    anomaly = max(thresholds["anomaly"], suspect + 1e-9)
    if score <= suspect:
        return max(0.80, 1.0 - 0.20 * score / suspect)
    if score <= anomaly:
        span = anomaly - suspect
        return max(0.30, 0.80 - 0.50 * (score - suspect) / span)
    return max(0.0, 0.30 * math.exp(-(score - anomaly)))


def utc_timestamp_ms() -> str:
    now_ms = int(time.time() * 1000)
    seconds = now_ms // 1000
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(seconds)) + f".{now_ms % 1000:03d}Z"


def pub_only_confidence(sq_label: str | None, metadata: dict[str, Any]) -> float:
    if sq_label == "good":
        confidence = 0.78
    elif sq_label == "fair":
        confidence = 0.62
    elif sq_label == "poor":
        confidence = 0.40
    else:
        confidence = 0.54
    if metadata.get("diagnose_dt_ns") is not None:
        confidence += 0.06
    if metadata.get("pub_fs_mps") is not None or metadata.get("pub_fr_m3h") is not None:
        confidence += 0.04
    return max(0.0, min(0.86, confidence))


def pub_only_health(confidence: float, sq_label: str | None, metadata: dict[str, Any]) -> dict[str, Any]:
    score = round(confidence * 100.0, 1)
    drivers = ["meter/pub telemetry received", "waveform not available for this device"]
    if sq_label and sq_label != "unknown":
        drivers.append(f"signal quality label: {sq_label}")
    if metadata.get("onboard_temperature_c") is not None:
        drivers.append("onboard temperature available")
    return {
        "version": "pub_only_v1",
        "score": score,
        "label": "Telemetry only",
        "color": "#64748b",
        "meaning": "Publish telemetry is available, but no waveform/acoustic capture was received.",
        "subscores": {
            "telemetry": score,
            "acoustic_pattern_match": None,
        },
        "weights": {
            "telemetry": 1.0,
            "acoustic_pattern_match": 0.0,
        },
        "drivers": drivers,
    }


def build_pub_only_record(
    serial: str,
    metadata: dict[str, Any],
    analyzer: AdaptiveAnalyzer,
    sq_tracker: SqTracker,
    thr_poor: float = DEFAULT_THR_POOR,
    thr_fair: float = DEFAULT_THR_FAIR,
) -> dict[str, Any]:
    sq, age = sq_tracker.get(serial)
    sq_label = sq_tracker.label(serial, thr_poor, thr_fair) if sq is not None else "unknown"
    dt_ns, dt_age_ms, dt_delta_ns = sq_tracker.get_dt(serial)
    merged = dict(metadata)
    confidence = pub_only_confidence(sq_label, merged)
    return {
        "timestamp": utc_timestamp_ms(),
        "serial": serial,
        "source_type": "pub_only",
        "waveform_supported": False,
        "seq": merged.get("seq", merged.get("sq", sq)),
        "sq": sq,
        "sq_age_ms": int(age) if age is not None else None,
        "sq_label": sq_label,
        "pub_dt_ns": dt_ns,
        "pub_dt_age_ms": int(dt_age_ms) if dt_age_ms is not None else None,
        "pub_dt_delta_ns": dt_delta_ns,
        "pub_meta_age_ms": 0,
        "ots": merged.get("ots"),
        "onboard_temperature_c": merged.get("onboard_temperature_c"),
        "pub_fs_mps": merged.get("pub_fs_mps"),
        "pub_fr_m3h": merged.get("pub_fr_m3h"),
        "pub_tfs_mps": merged.get("pub_tfs_mps"),
        "pub_tfr_m3h": merged.get("pub_tfr_m3h"),
        "raw_flow_speed_mps": merged.get("raw_flow_speed_mps"),
        "raw_flow_rate_m3h": merged.get("raw_flow_rate_m3h"),
        "raw_flow_total_m3": merged.get("raw_flow_total_m3"),
        "pub_flow_total_m3": merged.get("pub_flow_total_m3"),
        "zero_corr_fs_mps": merged.get("zero_corr_fs_mps"),
        "zero_corr_fr_m3h": merged.get("zero_corr_fr_m3h"),
        "corr_fs_mps": merged.get("corr_fs_mps"),
        "corr_fr_m3h": merged.get("corr_fr_m3h"),
        "flow_speed_zero_offset_mps": merged.get("flow_speed_zero_offset_mps"),
        "low_flow_cutoff_mps": merged.get("low_flow_cutoff_mps"),
        "measure_zero": merged.get("measure_zero"),
        "measure_lfc": merged.get("measure_lfc"),
        "measure_ledlfc": merged.get("measure_ledlfc"),
        "measure_kf": merged.get("measure_kf"),
        "diagnose_dt_ns": merged.get("diagnose_dt_ns"),
        "diagnose_tt_ns": merged.get("diagnose_tt_ns"),
        "pipe_outer_diameter_mm": merged.get("pipe_outer_diameter_mm"),
        "pipe_wall_thickness_mm": merged.get("pipe_wall_thickness_mm"),
        "pipe_inner_diameter_mm": merged.get("pipe_inner_diameter_mm"),
        "pipe_area_from_geometry_m2": merged.get("pipe_area_from_geometry_m2"),
        "pipe_area_m2": merged.get("pipe_area_m2"),
        "score": None,
        "label": "pub_only",
        "peak_mode": "pub_only",
        "mode_aware": False,
        "score_thresholds": analyzer.thresholds,
        "raw_pipe_state": "telemetry_only",
        "pipe_state": "telemetry_only",
        "measurement_confidence": confidence,
        "flow_meter_health": pub_only_health(confidence, sq_label, merged),
        "cnn_analysis": None,
        "cnn_health_weight": analyzer.cnn_health_weight,
        "diagnostic_reasons": ["pub telemetry only; waveform not available"],
        "features": {},
        "top_z_reasons": [],
        "self_training": {
            "enabled": False,
            "trained_this_sample": False,
            "updates": 0,
            "total_updates": analyzer.total_updates,
            "stable_ratio": 0,
            "freeze_reason": "pub_only_no_waveform",
            "serial_known": serial in analyzer.states,
        },
        "transitioned": False,
        "previous_pipe_state": "telemetry_only",
        "state_frames": 0,
        "state_entered_at": None,
        "state_confirmation": 1.0,
        "pending_pipe_state": None,
        "pending_state_frames": 0,
        "condition": "telemetry_only",
        "active_conditions": [],
        "condition_entered_at": {},
        "detection_events": [],
        "empty_window_count": 0,
        "empty_window_size": 0,
        "air_corr_std": None,
        "air_corr_threshold": None,
    }


def write_jsonl(path: Path | None, record: dict[str, Any]) -> None:
    if path is None:
        return
    with path.open("a") as f:
        f.write(json.dumps(record, separators=(",", ":")) + "\n")


def parse_csv_list(raw: str | None) -> list[str]:
    return [item.strip() for item in (raw or "").split(",") if item.strip()]


def env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def normalize_smtp_password(raw: str | None) -> str:
    # Gmail app passwords are often copied as four space-separated groups.
    return "".join((raw or "").split())


class EmailNotifier:
    def __init__(
        self,
        *,
        enabled: bool,
        smtp_host: str,
        smtp_port: int,
        smtp_username: str,
        smtp_password: str,
        email_from: str,
        email_to: list[str],
        starttls: bool,
        ssl_enabled: bool,
        min_gpm: float,
        cooldown_s: float,
        notifications_jsonl: Path | None,
        notify_diagnostics: bool,
    ) -> None:
        self.enabled = enabled
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        self.smtp_username = smtp_username
        self.smtp_password = normalize_smtp_password(smtp_password)
        self.email_from = email_from or smtp_username
        self.email_to = email_to
        self.starttls = starttls
        self.ssl_enabled = ssl_enabled
        self.min_gpm = min_gpm
        self.cooldown_s = max(0.0, cooldown_s)
        self.notifications_jsonl = notifications_jsonl
        self.notify_diagnostics = notify_diagnostics
        self.last_sent_at: dict[str, float] = {}
        self.warned_disabled = False
        if self.notifications_jsonl is not None:
            self.notifications_jsonl.parent.mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_args(cls, args: argparse.Namespace) -> "EmailNotifier":
        return cls(
            enabled=args.email_enabled,
            smtp_host=args.email_smtp_host,
            smtp_port=args.email_smtp_port,
            smtp_username=args.email_smtp_username,
            smtp_password=args.email_smtp_password,
            email_from=args.email_from,
            email_to=parse_csv_list(args.email_to),
            starttls=args.email_starttls,
            ssl_enabled=args.email_ssl,
            min_gpm=args.email_min_gpm,
            cooldown_s=args.email_cooldown_minutes * 60.0,
            notifications_jsonl=args.notifications_jsonl,
            notify_diagnostics=args.email_notify_diagnostics,
        )

    def configured(self) -> bool:
        return bool(
            self.enabled
            and self.smtp_host
            and self.smtp_port
            and self.email_from
            and self.email_to
            and (not self.smtp_username or self.smtp_password)
        )

    def warn_if_needed(self) -> None:
        if self.enabled and not self.configured() and not self.warned_disabled:
            print(
                "email notifications enabled but SMTP settings are incomplete; "
                "set EMAIL_TO, EMAIL_FROM/SMTP_USERNAME, SMTP_HOST, and SMTP_PASSWORD.",
                file=sys.stderr,
            )
            self.warned_disabled = True

    def notify_record(self, record: dict[str, Any]) -> None:
        if not self.enabled:
            return
        self.warn_if_needed()
        flow_gpm = self.record_gpm(record)
        if flow_gpm is not None and flow_gpm >= self.min_gpm:
            self.send(
                key=f"{record.get('serial')}:flow_above_threshold",
                subject=f"Flow alert {record.get('serial')}: {flow_gpm:.2f} GPM",
                body=self.record_body(
                    record,
                    headline=f"Flow is above the email threshold ({flow_gpm:.2f} GPM >= {self.min_gpm:.2f} GPM).",
                ),
                event={
                    "event": "email_flow_threshold",
                    "serial": record.get("serial"),
                    "timestamp": record.get("timestamp"),
                    "gpm": flow_gpm,
                    "threshold_gpm": self.min_gpm,
                },
            )

    def notify_event(self, event: dict[str, Any], context: dict[str, Any] | None = None) -> None:
        if not self.enabled:
            return
        if not self.notify_diagnostics:
            return
        self.warn_if_needed()
        serial = event.get("serial") or (context or {}).get("serial")
        event_name = event.get("event", "meter_event")
        subject = f"Meter diagnostic {serial}: {event_name}"
        lines = [
            f"Event: {event_name}",
            f"Serial: {serial or 'unknown'}",
            f"Timestamp: {event.get('timestamp') or (context or {}).get('timestamp') or 'unknown'}",
        ]
        if context:
            flow_gpm = self.record_gpm(context)
            if flow_gpm is not None:
                lines.append(f"Flow: {flow_gpm:.3f} GPM")
            health = context.get("flow_meter_health") or {}
            if isinstance(health, dict) and health.get("score") is not None:
                lines.append(f"Health: {health.get('score')} / 100 ({health.get('label', 'unknown')})")
            lines.append(f"Pipe state: {context.get('pipe_state') or 'unknown'}")
        lines.append("")
        lines.append("Event payload:")
        lines.append(json.dumps(event, indent=2, ensure_ascii=False))
        self.send(
            key=f"{serial}:{event_name}",
            subject=subject,
            body="\n".join(lines),
            event={
                "event": "email_diagnostic_event",
                "serial": serial,
                "timestamp": event.get("timestamp") or (context or {}).get("timestamp"),
                "diagnostic_event": event_name,
            },
        )

    def record_gpm(self, record: dict[str, Any]) -> float | None:
        for key in ("corr_fr_m3h", "pub_tfr_m3h", "pub_fr_m3h", "zero_corr_fr_m3h", "raw_flow_rate_m3h"):
            value = record.get(key)
            if isinstance(value, (int, float)) and math.isfinite(value):
                return max(0.0, float(value)) * M3H_TO_GPM
        return None

    def record_body(self, record: dict[str, Any], *, headline: str) -> str:
        flow_gpm = self.record_gpm(record)
        health = record.get("flow_meter_health") or {}
        health_line = ""
        if isinstance(health, dict) and health.get("score") is not None:
            health_line = f"\nHealth: {health.get('score')} / 100 ({health.get('label', 'unknown')})"
        return (
            f"{headline}\n\n"
            f"Serial: {record.get('serial') or 'unknown'}\n"
            f"Timestamp: {record.get('timestamp') or 'unknown'}\n"
            f"Flow: {flow_gpm:.3f} GPM\n"
            f"FR raw: {record.get('pub_fr_m3h')} m3/h\n"
            f"FS raw: {record.get('pub_fs_mps')} m/s\n"
            f"Temperature OTS: {record.get('onboard_temperature_c') or record.get('ots')} C\n"
            f"Diagnose dt: {record.get('diagnose_dt_ns')} ns\n"
            f"Diagnose tt: {record.get('diagnose_tt_ns')} ns\n"
            f"SQ: {record.get('sq')} ({record.get('sq_label')})\n"
            f"Pipe state: {record.get('pipe_state') or 'unknown'}"
            f"{health_line}\n"
        )

    def send(self, *, key: str, subject: str, body: str, event: dict[str, Any]) -> None:
        now = time.time()
        event = dict(event)
        event.setdefault("email_subject", subject)
        event["email_to"] = self.email_to
        event["cooldown_key"] = key
        last = self.last_sent_at.get(key, 0.0)
        if now - last < self.cooldown_s:
            event["status"] = "cooldown"
            event["cooldown_remaining_s"] = int(self.cooldown_s - (now - last))
            write_jsonl(self.notifications_jsonl, event)
            return
        if not self.configured():
            self.last_sent_at[key] = now
            event["status"] = "disabled"
            write_jsonl(self.notifications_jsonl, event)
            return
        try:
            message = EmailMessage()
            message["Subject"] = subject
            message["From"] = self.email_from
            message["To"] = ", ".join(self.email_to)
            message.set_content(body)
            if self.ssl_enabled:
                with smtplib.SMTP_SSL(self.smtp_host, self.smtp_port, timeout=10, context=ssl.create_default_context()) as smtp:
                    self.login_and_send(smtp, message)
            else:
                with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=10) as smtp:
                    if self.starttls:
                        smtp.starttls(context=ssl.create_default_context())
                    self.login_and_send(smtp, message)
            self.last_sent_at[key] = now
            event["status"] = "sent"
            print(f"email notification sent: {subject}")
        except Exception as exc:  # noqa: BLE001 - notification failure must not stop ingest
            event["status"] = "error"
            event["error"] = str(exc)
            print(f"email notification failed: {exc}", file=sys.stderr)
        write_jsonl(self.notifications_jsonl, event)

    def login_and_send(self, smtp: smtplib.SMTP, message: EmailMessage) -> None:
        if self.smtp_username:
            smtp.login(self.smtp_username, self.smtp_password)
        smtp.send_message(message)


def save_model(path: Path | None, model: dict[str, Any]) -> None:
    if path is None:
        return
    path.write_text(json.dumps(model, indent=2))


# ── Stdin mode (unchanged contract) ───────────────────────────────────────────


def run_stdin(args: argparse.Namespace, analyzer: AdaptiveAnalyzer, csv_writer: GuiCsvWriter | None) -> None:
    last_save_at = 0
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            samples, metadata = parse_sig_payload(line)
            result = analyzer.analyze(samples, metadata)
            print(json.dumps(result, ensure_ascii=False))
            write_jsonl(args.log_jsonl, result)
            if csv_writer is not None:
                csv_writer.write(metadata, samples)
            total = analyzer.total_updates
            if total and total - last_save_at >= args.save_every:
                save_model(args.adapted_model_out, analyzer.snapshot_for_save())
                last_save_at = total
        except Exception as exc:  # noqa: BLE001 - surface to stderr per stream contract
            print(json.dumps({"error": str(exc), "payload": line[:200]}), file=sys.stderr)
    save_model(args.adapted_model_out, analyzer.snapshot_for_save())


# ── MQTT mode (matches the GUI's topic structure) ─────────────────────────────


def run_mqtt(args: argparse.Namespace, analyzer: AdaptiveAnalyzer, csv_writer: GuiCsvWriter | None) -> None:
    try:
        import paho.mqtt.client as mqtt
    except ModuleNotFoundError as exc:
        raise SystemExit(
            "Missing dependency: paho-mqtt. Install it with `python3 -m pip install paho-mqtt` "
            "or run with `--stdin` for JSONL testing."
        ) from exc

    sq_tracker = SqTracker()
    email_notifier = EmailNotifier.from_args(args)

    # Build MQTT subscription templates. With --serials-json, {serial} or a '+'
    # topic segment is expanded from the UI-managed serial list instead of
    # subscribing to broker wildcards.
    topic_templates = [args.topic] if args.topic else [args.sig_topic, args.pub_topic, args.processed_topic]
    topic_templates = [topic for topic in topic_templates if topic]
    static_topics = set(topic_templates) if not args.serials_json else set()
    subscribed_topics: set[str] = set()
    last_seen_serials: list[str] = []
    waiting_for_serials_printed = False
    pub_only_last_console_ts: dict[str, float] = {}
    last_sig_seen_wall_ts: dict[str, float] = {}
    first_pub_seen_wall_ts: dict[str, float] = {}

    client_id = (args.client_id or "lens_cnn_{uuid}").format(uuid=uuid.uuid4())
    callback_api_version = getattr(getattr(mqtt, "CallbackAPIVersion", None), "VERSION2", None)
    if callback_api_version is not None:
        client = mqtt.Client(
            callback_api_version=callback_api_version,
            client_id=client_id,
        )
    else:
        client = mqtt.Client(client_id=client_id)
    if args.username:
        client.username_pw_set(args.username, args.password)
    if args.tls:
        client.tls_set()

    def desired_topics() -> tuple[set[str], list[str]]:
        if not args.serials_json:
            return set(static_topics), []
        serials = load_subscription_serials(args.serials_json, args.max_serials)
        return expand_subscription_topics(topic_templates, serials), serials

    def sync_subscriptions(force: bool = False) -> None:
        nonlocal subscribed_topics, last_seen_serials, waiting_for_serials_printed
        topics, serials = desired_topics()
        if args.serials_json and serials != last_seen_serials:
            last_seen_serials = list(serials)
            if serials:
                print(f"ui serial subscriptions: {', '.join(serials)}")
            elif not waiting_for_serials_printed:
                print(f"waiting for UI serial subscriptions in {args.serials_json}")
                waiting_for_serials_printed = True
        if force:
            subscribed_topics = set()
        for topic in sorted(topics - subscribed_topics):
            client.subscribe(topic, qos=args.qos)
            print(f"  subscribed: {topic}")
        for topic in sorted(subscribed_topics - topics):
            client.unsubscribe(topic)
            print(f"  unsubscribed: {topic}")
        subscribed_topics = topics

    def on_connect(client, _userdata, _flags, reason_code, _properties=None):
        is_failure = getattr(reason_code, "is_failure", None)
        connected = (not is_failure) if isinstance(is_failure, bool) else False
        if not connected:
            try:
                connected = int(reason_code) == 0
            except (TypeError, ValueError):
                connected = str(reason_code).lower() in {"0", "success"}
        if connected:
            print(f"connected broker={args.broker}:{args.port} client_id={client_id}")
            sync_subscriptions(force=True)
        else:
            print(f"mqtt connect failed reason={reason_code}", file=sys.stderr)

    def on_message(client, _userdata, message):
        topic = message.topic
        tid = topic_id(topic)

        # Diagnostic streams are not waveforms. Cache their SQ/dt metadata for
        # future sig frames, and emit a pub-only live record for devices that do
        # not publish meter/sig waveform captures.
        if topic.startswith("meter/pub/") or topic.startswith("processed/meter/"):
            try:
                parsed = json.loads(message.payload.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return
            sq = extract_sq_from_pub(parsed)
            dt_ns = extract_dt_from_pub(parsed)
            pub_meta = extract_pub_flow_metadata(parsed)
            if sq is not None or dt_ns is not None or pub_meta:
                sq_tracker.update(tid, sq, dt_ns, pub_meta)
            if pub_meta:
                now_wall = time.time()
                first_pub_seen_wall_ts.setdefault(tid, now_wall)
                recent_sig_age_s = now_wall - last_sig_seen_wall_ts.get(tid, 0.0)
                if recent_sig_age_s <= PUB_ONLY_SIG_GRACE_S:
                    return
                if tid not in last_sig_seen_wall_ts and now_wall - first_pub_seen_wall_ts[tid] < PUB_ONLY_INITIAL_WAIT_S:
                    return
                result = build_pub_only_record(
                    tid,
                    pub_meta,
                    analyzer,
                    sq_tracker,
                    args.thr_poor,
                    args.thr_fair,
                )
                write_jsonl(args.log_jsonl, result)
                email_notifier.notify_record(result)
                if args.publish_topic:
                    client.publish(args.publish_topic, json.dumps(result), qos=args.qos)

                last_pub_only_log = pub_only_last_console_ts.get(tid, 0.0)
                if args.log_mode == "every" or now_wall - last_pub_only_log >= args.heartbeat_s:
                    pub_only_last_console_ts[tid] = now_wall
                    sq_disp = f"{result['sq']:.0f}" if isinstance(result["sq"], (int, float)) else "  —"
                    fs_disp = (
                        f"{result['pub_fs_mps']:.6f}m/s"
                        if isinstance(result["pub_fs_mps"], (int, float))
                        else "—m/s"
                    )
                    fr_disp = (
                        f"{result['pub_fr_m3h']:.6f}m3/h"
                        if isinstance(result["pub_fr_m3h"], (int, float))
                        else "—m3/h"
                    )
                    temp_disp = (
                        f"{result['onboard_temperature_c']:.2f}C"
                        if isinstance(result["onboard_temperature_c"], (int, float))
                        else "—C"
                    )
                    print(
                        f"{result['timestamp']} pub-only sn={tid} sq={sq_disp} "
                        f"sq_label={result['sq_label'] or 'unknown':<7} fs={fs_disp} "
                        f"fr={fr_disp} ots={temp_disp} "
                        f"conf={result['measurement_confidence']:.2f} "
                        f"health={result['flow_meter_health']['score']:.1f}/"
                        f"{result['flow_meter_health']['label']}"
                    )
            return

        # Otherwise treat as a waveform.
        try:
            samples, metadata = parse_sig_payload(message.payload)
        except Exception as exc:  # noqa: BLE001
            print(f"failed to parse waveform on {topic}: {exc}", file=sys.stderr)
            return

        metadata.setdefault("topic", topic)
        metadata.setdefault("serial", tid)
        metadata.setdefault("timestamp", time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()))
        last_sig_seen_wall_ts[tid] = time.time()

        sq, age = sq_tracker.get(tid)
        if sq is not None:
            metadata["sq"] = sq
            metadata["sq_age_ms"] = int(age) if age is not None else None
            metadata["sq_label"] = sq_tracker.label(tid, args.thr_poor, args.thr_fair)
        else:
            metadata.setdefault("sq_label", "unknown")

        dt_ns, dt_age_ms, dt_delta_ns = sq_tracker.get_dt(tid)
        if dt_ns is not None:
            metadata["pub_dt_ns"] = dt_ns
            metadata["pub_dt_age_ms"] = int(dt_age_ms) if dt_age_ms is not None else None
            metadata["pub_dt_delta_ns"] = dt_delta_ns
        pub_meta, pub_meta_age_ms = sq_tracker.get_pub_metadata(tid)
        if pub_meta:
            metadata.update(pub_meta)
            metadata["pub_meta_age_ms"] = int(pub_meta_age_ms) if pub_meta_age_ms is not None else None

        try:
            result = analyzer.analyze(samples, metadata)
        except Exception as exc:  # noqa: BLE001
            print(f"analyze failed for {topic}: {exc}", file=sys.stderr)
            return

        write_jsonl(args.log_jsonl, result)
        email_notifier.notify_record(result)
        if csv_writer is not None:
            csv_writer.write(metadata, samples)

        # State transition / heartbeat console logging. ``--log-mode every``
        # restores the old per-frame behavior; the default ``transitions``
        # only prints when pipe_state changes plus one heartbeat per
        # --heartbeat-s seconds per serial.
        state = analyzer.get_state(tid)
        now_wall = time.time()
        should_print = False
        line_tag = ""
        if args.log_mode == "every":
            should_print = True
            line_tag = " "
        else:
            if result["detection_events"]:
                should_print = True
                ev_name = result["detection_events"][0]["event"].upper()
                line_tag = f" {ev_name}"
            elif result["transitioned"]:
                should_print = True
                line_tag = " TRANSITION"
            elif (now_wall - state.last_console_wall_ts) >= args.heartbeat_s:
                should_print = True
                line_tag = " heartbeat "

        if should_print:
            state.last_console_wall_ts = now_wall
            sq_disp = f"{result['sq']:.0f}" if isinstance(result["sq"], (int, float)) else "  —"
            if result["pub_dt_age_ms"] is not None:
                dt_disp = f"dt_age={result['pub_dt_age_ms']:>5}ms"
            else:
                dt_disp = "dt_age=  —ms"
            prev_disp = f" (was {result['previous_pipe_state']})" if result["transitioned"] else ""
            raw_disp = (
                f" raw={result['raw_pipe_state']}"
                if result["raw_pipe_state"] != result["pipe_state"]
                else ""
            )
            print(
                f"{result['timestamp']}{line_tag} sn={metadata.get('serial')} sq={sq_disp} "
                f"sq_label={result['sq_label'] or 'unknown':<7} {dt_disp} "
                f"mode={result['peak_mode']} state={result['pipe_state']}{prev_disp}{raw_disp} "
                f"label={result['label']} "
                f"score={result['score']:.3f} conf={result['measurement_confidence']:.2f} "
                f"health={result['flow_meter_health']['score']:.1f}/"
                f"{result['flow_meter_health']['label']} "
                f"train={result['self_training']['trained_this_sample']} "
                f"freeze={result['self_training']['freeze_reason'] or '-'}"
            )

        # Emit a dedicated transition event so downstream tools don't have to
        # scan every per-frame record to find the interesting moments.
        if result["transitioned"]:
            event = {
                "event": "pipe_state_transition",
                "timestamp": result["timestamp"],
                "serial": result["serial"],
                "from": result["previous_pipe_state"],
                "to": result["pipe_state"],
                "raw_pipe_state": result["raw_pipe_state"],
                "peak_mode": result["peak_mode"],
                "mode_aware": result["mode_aware"],
                "score": result["score"],
                "label": result["label"],
                "flow_meter_health": result["flow_meter_health"],
                "diagnostic_reasons": result["diagnostic_reasons"],
                "sq": result["sq"],
                "sq_label": result["sq_label"],
            }
            write_jsonl(args.events_jsonl, event)
            email_notifier.notify_event(event, result)
        # Confirmed detection events (empty pipe, refilled, ...).
        for ev in result["detection_events"]:
            write_jsonl(args.events_jsonl, ev)
            email_notifier.notify_event(ev, result)

        if args.publish_topic:
            client.publish(args.publish_topic, json.dumps(result), qos=args.qos)
        nonlocal last_save_at
        total = analyzer.total_updates
        if total and total - last_save_at >= args.save_every:
            save_model(args.adapted_model_out, analyzer.snapshot_for_save())
            last_save_at = total

    last_save_at = 0
    client.on_connect = on_connect
    client.on_message = on_message
    try:
        client.connect(args.broker, args.port, keepalive=args.keepalive)
    except OSError as exc:
        raise SystemExit(
            f"Could not connect to MQTT broker at {args.broker}:{args.port}: {exc}\n"
            "\n"
            "Start a broker locally, or point --broker/--port at the broker used by the GUI/device.\n"
            "Examples:\n"
            "  brew services start mosquitto\n"
            "  mosquitto -p 1883\n"
            "  docker run --rm -p 1883:1883 eclipse-mosquitto:2 "
            "mosquitto -c /mosquitto-no-auth.conf\n"
            "\n"
            "For offline smoke tests, run with --stdin and feed JSON waveform payloads."
        ) from exc
    try:
        last_sync_at = 0.0
        while True:
            client.loop(timeout=1.0)
            if args.serials_json and (time.time() - last_sync_at) >= args.serials_poll_s:
                sync_subscriptions()
                last_sync_at = time.time()
    finally:
        save_model(args.adapted_model_out, analyzer.snapshot_for_save())


# ── CLI ───────────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, default=Path("oneclass_meter_model.json"))
    parser.add_argument("--adapted-model-out", type=Path, default=Path("adaptive_meter_model.json"))
    parser.add_argument(
        "--cnn-model", type=Path, default=None,
        help="Optional cnn_autoencoder_model.pt checkpoint. When supplied, each "
             "waveform gets CNN reconstruction and embedding-neighbor scores "
             "that feed flow_meter_health.acoustic_pattern_match.",
    )
    parser.add_argument(
        "--cnn-device", default="auto",
        help="Device for --cnn-model scoring: auto, cpu, mps, or cuda (default auto).",
    )
    parser.add_argument(
        "--cnn-top-k", type=int, default=3,
        help="Number of nearest reference embeddings to include in cnn_analysis.",
    )
    parser.add_argument(
        "--cnn-health-weight",
        type=float,
        default=0.0,
        help="How much CNN score contributes to flow health when --cnn-model is supplied "
             "(0=report CNN only, 1=CNN fully drives acoustic pattern score; default 0.0).",
    )
    parser.add_argument("--log-jsonl", type=Path, default=Path("mqtt_analysis_log.jsonl"))
    parser.add_argument(
        "--events-jsonl", type=Path, default=Path("mqtt_events.jsonl"),
        help="JSONL file for state-transition events (separate from per-frame log).",
    )
    parser.add_argument(
        "--notifications-jsonl",
        type=Path,
        default=Path(os.environ.get("NOTIFICATIONS_JSONL", "mqtt_notifications.jsonl")),
        help="JSONL audit log for email notification attempts.",
    )
    parser.add_argument(
        "--email-enabled",
        action=argparse.BooleanOptionalAction,
        default=env_bool("EMAIL_ENABLED", False),
        help="Enable SMTP email notifications. Defaults from EMAIL_ENABLED.",
    )
    parser.add_argument("--email-to", default=os.environ.get("EMAIL_TO", ""))
    parser.add_argument("--email-from", default=os.environ.get("EMAIL_FROM", ""))
    parser.add_argument("--email-smtp-host", default=os.environ.get("SMTP_HOST", os.environ.get("EMAIL_SMTP_HOST", "")))
    parser.add_argument("--email-smtp-port", type=int, default=int(os.environ.get("SMTP_PORT", os.environ.get("EMAIL_SMTP_PORT", "587"))))
    parser.add_argument("--email-smtp-username", default=os.environ.get("SMTP_USERNAME", os.environ.get("EMAIL_SMTP_USERNAME", "")))
    parser.add_argument("--email-smtp-password", default=os.environ.get("SMTP_PASSWORD", os.environ.get("EMAIL_SMTP_PASSWORD", "")))
    parser.add_argument(
        "--email-starttls",
        action=argparse.BooleanOptionalAction,
        default=env_bool("EMAIL_STARTTLS", True),
        help="Use SMTP STARTTLS for email notifications (default true).",
    )
    parser.add_argument(
        "--email-ssl",
        action=argparse.BooleanOptionalAction,
        default=env_bool("EMAIL_SSL", False),
        help="Use SMTP SSL/TLS from connection start, usually port 465.",
    )
    parser.add_argument(
        "--email-min-gpm",
        type=float,
        default=env_float("EMAIL_MIN_GPM", 0.5),
        help="Send flow email when the best available flow rate is at or above this GPM.",
    )
    parser.add_argument(
        "--email-cooldown-minutes",
        type=float,
        default=env_float("EMAIL_COOLDOWN_MINUTES", 30.0),
        help="Minimum minutes between repeated emails for the same meter/reason.",
    )
    parser.add_argument(
        "--email-notify-diagnostics",
        action=argparse.BooleanOptionalAction,
        default=env_bool("EMAIL_NOTIFY_DIAGNOSTICS", True),
        help="Also email confirmed acoustic diagnostic events such as empty pipe or air bubble.",
    )
    parser.add_argument(
        "--log-mode", choices=["transitions", "every"], default="transitions",
        help="Console verbosity. 'transitions' prints on pipe_state change plus a "
             "heartbeat every --heartbeat-s seconds; 'every' restores the old "
             "per-frame line.",
    )
    parser.add_argument(
        "--heartbeat-s", type=float, default=60.0,
        help="In transitions log mode, print one heartbeat line per serial after "
             "this many seconds of unchanged state (default 60s).",
    )
    parser.add_argument("--save-csv", type=Path, default=None,
                        help="Optional CSV path; rows are written in the GUI's capture format "
                             "(timestamp,serial,sq,sq_age_ms,label,n_samples,s_0…).")
    parser.add_argument("--stdin", action="store_true", help="Read JSON payloads from stdin instead of MQTT.")
    parser.add_argument("--self-train", action="store_true", help="Enable conservative online adaptation.")
    parser.add_argument("--allow-fair", action="store_true",
                        help="Allow self-training on rows the GUI would label 'fair'. "
                             "Off by default — only 'good' rows adapt the model.")
    parser.add_argument("--stable-window", type=int, default=64)
    parser.add_argument("--template-alpha", type=float, default=0.001)
    parser.add_argument("--center-alpha", type=float, default=0.001)
    parser.add_argument("--save-every", type=int, default=100)
    parser.add_argument(
        "--stable-ratio", type=float, default=0.6,
        help="Fraction of recent frames that must pass the gates before "
             "self-training is allowed to fire (default 0.6).",
    )
    parser.add_argument(
        "--corr-percentile", choices=["p01", "p05", "p50", "p95", "p99"], default="p05",
        help="Reject frames whose template_corr is below this percentile of "
             "training-time values (default p05 — only the worst 5%% are rejected).",
    )
    parser.add_argument(
        "--snr-percentile", choices=["p01", "p05", "p50"], default="p05",
        help="Reject frames whose SNR is below this percentile (default p05).",
    )
    parser.add_argument(
        "--gate-energy-percentile", choices=["p01", "p05", "p50"], default="p05",
        help="Reject frames whose gate RMS is below this percentile (default p05).",
    )
    parser.add_argument(
        "--score-headroom", type=float, default=0.95,
        help="Fraction of the suspect threshold a frame's score must stay below "
             "(default 0.95 — i.e. only reject if score > 95%% of suspect).",
    )
    parser.add_argument(
        "--pub-dt-stale-ms", type=float, default=5000.0,
        help="Freeze self-training if the most recent meter/pub dt (device-side "
             "ns timestamp) is older than this many wall-clock ms (default 5000). "
             "Pass 0 to disable the check.",
    )
    # ── Detection: sustained empty pipe ───────────────────────────────────
    parser.add_argument(
        "--empty-window-m", type=int, default=5,
        help="Detection: look at the last M pipe_state values per device "
             "(default 5).",
    )
    parser.add_argument(
        "--empty-window-n", type=int, default=3,
        help="Detection: require N of the last M to indicate empty/lost-path "
             "before emitting empty_pipe_detected (default 3).",
    )
    parser.add_argument(
        "--empty-recovery-n", type=int, default=1,
        help="Detection: emit pipe_refilled once bad count drops to this or "
             "fewer (default 1 — single clean frame in the window).",
    )
    parser.add_argument(
        "--empty-strict", action="store_true",
        help="Detection: only count the strong "
             "empty_or_lost_acoustic_path_candidate state. Default also counts "
             "weak_signal_or_air_candidate, which is more sensitive.",
    )
    # ── Detection: intermittent coupling / air bubbles ────────────────────
    parser.add_argument(
        "--air-window-n", type=int, default=20,
        help="Detection: rolling window length (frames) for template_corr "
             "variance (default 20).",
    )
    parser.add_argument(
        "--air-corr-std-mult", type=float, default=5.0,
        help="Detection: emit air_bubble_detected when the rolling stddev of "
             "template_corr exceeds (multiplier × training robust_sigma). "
             "Default 5.0.",
    )
    parser.add_argument(
        "--air-recovery-factor", type=float, default=0.5,
        help="Detection: emit air_bubble_cleared once rolling stddev drops "
             "below (recovery_factor × detection threshold). Default 0.5.",
    )
    parser.add_argument(
        "--state-enter-frames", type=int, default=3,
        help="Console/customer state smoothing: require this many consecutive "
             "non-normal frames before leaving normal_acoustic_state (default 3).",
    )
    parser.add_argument(
        "--state-recover-frames", type=int, default=5,
        help="Console/customer state smoothing: require this many consecutive "
             "normal frames before recovering from a non-normal state (default 5).",
    )

    parser.add_argument("--broker", default="localhost")
    parser.add_argument("--port", type=int, default=1883)
    # GUI-style multi-topic subscription. Empty --topic means use these three.
    parser.add_argument("--topic", default="",
                        help="Legacy single-topic override (e.g. 'meters/+/adc'). "
                             "If empty, --sig-topic / --pub-topic / --processed-topic are used.")
    parser.add_argument("--sig-topic", default="meter/sig/+",
                        help="MQTT topic carrying waveforms (server.js 'meter/sig/<NUI>').")
    parser.add_argument("--pub-topic", default="meter/pub/+",
                        help="MQTT topic carrying diagnose.sq from devices.")
    parser.add_argument("--processed-topic", default="processed/meter/+",
                        help="MQTT topic carrying processed meter messages with diagnose.sq.")
    parser.add_argument(
        "--serials-json",
        type=Path,
        default=None,
        help="Optional UI-managed JSON file with a serials array. When present, {serial} "
             "or '+' topic segments are expanded from this file and resynced live.",
    )
    parser.add_argument("--serials-poll-s", type=float, default=2.0)
    parser.add_argument("--max-serials", type=int, default=MAX_DYNAMIC_SERIALS)
    parser.add_argument("--thr-poor", type=float, default=DEFAULT_THR_POOR,
                        help=f"sq < this → label 'poor' (default {DEFAULT_THR_POOR}; matches GUI).")
    parser.add_argument("--thr-fair", type=float, default=DEFAULT_THR_FAIR,
                        help=f"sq < this → label 'fair' (default {DEFAULT_THR_FAIR}; matches GUI).")
    parser.add_argument("--publish-topic", default="")
    parser.add_argument(
        "--client-id", default="lens_cnn_{uuid}",
        help="MQTT client id. Supports a {uuid} placeholder. Default: lens_cnn_{uuid}.",
    )
    parser.add_argument("--username", default="")
    parser.add_argument("--password", default="")
    parser.add_argument("--tls", action="store_true")
    parser.add_argument("--qos", type=int, default=0)
    parser.add_argument("--keepalive", type=int, default=60)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    model = json.loads(args.model.read_text())
    cnn_scorer = None
    if args.cnn_model is not None:
        if not args.cnn_model.exists():
            raise SystemExit(f"CNN model not found: {args.cnn_model}")
        cnn_scorer = CnnEmbeddingScorer(
            args.cnn_model,
            device=args.cnn_device,
            top_k=args.cnn_top_k,
        )

    # When --allow-fair is on, treat 'fair' rows as acceptable too — but
    # 'poor' and 'unknown' still freeze learning. Implemented by widening
    # the gate check inside the analyzer.
    if args.allow_fair:
        orig = AdaptiveAnalyzer.is_safe_training_sample

        def widened(
            self,
            state,
            features,
            score,
            label,
            metadata=None,
            feature_stats=None,
            thresholds=None,
        ):
            if metadata is not None:
                sq_label = metadata.get("sq_label")
                if sq_label in ("poor", "unknown"):
                    return False, f"sq_label_{sq_label}"
                # Temporarily strip sq_label so the original gate doesn't refuse 'fair'.
                metadata = {k: v for k, v in metadata.items() if k != "sq_label"}
            return orig(
                self,
                state,
                features,
                score,
                label,
                metadata,
                feature_stats=feature_stats,
                thresholds=thresholds,
            )

        AdaptiveAnalyzer.is_safe_training_sample = widened  # type: ignore[assignment]

    analyzer = AdaptiveAnalyzer(
        model,
        self_train=args.self_train,
        stable_window=args.stable_window,
        template_alpha=args.template_alpha,
        center_alpha=args.center_alpha,
        corr_min_percentile=args.corr_percentile,
        snr_min_percentile=args.snr_percentile,
        gate_energy_min_percentile=args.gate_energy_percentile,
        stable_ratio_threshold=args.stable_ratio,
        score_headroom=args.score_headroom,
        pub_dt_stale_ms=(args.pub_dt_stale_ms if args.pub_dt_stale_ms > 0 else None),
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
        cnn_scorer=cnn_scorer,
    )
    csv_writer = GuiCsvWriter(args.save_csv) if args.save_csv else None

    if args.stdin:
        run_stdin(args, analyzer, csv_writer)
    else:
        run_mqtt(args, analyzer, csv_writer)


if __name__ == "__main__":
    main()
