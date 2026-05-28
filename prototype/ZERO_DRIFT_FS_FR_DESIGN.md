# Temperature Drift, FS, and FR Prototype Design

## Core idea

Store zero drift in `fs` first, then derive `fr`.

`fs` is flow speed in `m/s`. `fr` is volume flow in `m3/h`.

```text
pipe_inner_diameter_mm = pd_mm - 2 * pt_mm
area_m2 = pi * pipe_inner_diameter_m^2 / 4
fr_m3h = fs_mps * area_m2 * 3600
fs_mps = fr_m3h / (area_m2 * 3600)
```

`pd` is pipe outer diameter and `pt` is pipe wall thickness. Do not use `pd`
directly as the flow cross-section diameter.

Temperature zero drift is closer to a velocity / time-offset error than a billing volume error, so the primary correction should be:

```text
fs_corrected = fs_raw - fs_zero_drift
fr_corrected = fs_corrected * area_m2 * 3600
```

Do not learn independent `fs_zero` and `fr_zero` unless there is a known pipe-area or volume-calibration issue. Otherwise they can fight each other.

## Zero tracking model

Use three pieces:

```text
fs_zero_drift = fs_temp_model(T, dT/dt, direction) + fs_adaptive_zero
```

`fs_temp_model` handles expected thermal drift:

```text
fs_temp_model =
  a_heat * (T - T_ref) + b_heat * dT/dt    when warming
  a_cool * (T - T_ref) + b_cool * dT/dt    when cooling
```

Heating and cooling should be separate because thermal lag can be asymmetric.

`fs_adaptive_zero` is a slow residual learner. It only updates when there is strong no-flow evidence:

```text
residual_fs = fs_raw - fs_temp_model
fs_adaptive_zero = EWMA(residual_fs)
```

Use higher learning rate for user-confirmed `zero_flow`, lower learning rate for machine-only high-confidence zero.

## OTS from MQTT pub payload

The current `meter/pub` payload exposes:

```json
{
  "flow": {
    "fs": "0.001014",
    "fr": "0.001256",
    "tfs": "0.001161",
    "tfr": "0.001438",
    "ft": "1.224219",
    "ots": "41.477200"
  }
}
```

`flow.ots` is the onboard temperature sensor in degrees C. Treat it as a
thermal state variable, not as an offset. It is often a better predictor for
electronics / transducer delay drift than ambient or pipe temperature alone.

The same payload's `diagnose` block carries transducer timing in nanoseconds:

```text
diagnose.dt -> diagnose_dt_ns   # transducer time difference
diagnose.tt -> diagnose_tt_ns   # transducer total time
```

These are useful for separating the physics:

```text
diagnose_tt_ns changes mostly with sound-speed / path / thermal state
diagnose_dt_ns carries the raw upstream/downstream timing imbalance
fs_zero_drift is learned from diagnose_dt_ns when the user confirms zero flow
```

Store both the raw reading and a reference-relative delta:

```text
onboard_temperature_c = ots
onboard_temp_delta_c = onboard_temperature_c - onboard_temp_reference_c
onboard_temp_rate_c_per_min = d(onboard_temperature_c)/dt
```

Use OTS directly in the thermal model:

```text
fs_temp_model =
  a_heat * onboard_temp_delta_c + b_heat * onboard_temp_rate_c_per_min
  a_cool * onboard_temp_delta_c + b_cool * onboard_temp_rate_c_per_min
```

If another temperature is available, keep it too. The thermal gradient can be
useful:

```text
thermal_gradient_c = onboard_temperature_c - pipe_or_fluid_temperature_c
```

Recommended persisted fields:

```text
onboard_temperature_c
onboard_temp_reference_c
onboard_temp_delta_c
onboard_temp_rate_c_per_min
pipe_or_fluid_temperature_c
thermal_gradient_c
```

## Decision logic

Each frame should produce:

```text
fs_raw
fr_raw
fs_zero_drift
fr_zero_drift
fs_corrected
fr_corrected
fs_published
fr_published
phantom_flow_probability
event_flow_probability
zero_flow_probability
measurement_confidence
```

The important rule:

```text
temperature_change + small flow != automatic zero
```

Suppress only when:

```text
phantom_flow_probability is high
event_flow_probability is low
abs(fs_corrected) is inside the low-flow band
waveform drift matches the thermal model
```

Confirmed `event_flow` must block zero learning for that interval.

## Drift state storage

Keep a compact current state per meter, plus append-only event history.

```json
{
  "serial": "BB8100017587",
  "version": 1,
  "pipe_outer_diameter_mm": 26.67,
  "pipe_wall_thickness_mm": 2.87,
  "pipe_inner_diameter_mm": 20.93,
  "area_m2": 0.000344066,
  "updated_at": "2026-05-28T20:00:00Z",
  "thermal_model": {
    "reference_temp_c": 22.0,
    "heat_coeff_fs_per_c": 0.0020,
    "cool_coeff_fs_per_c": 0.0028,
    "heat_rate_coeff_fs_per_c_per_min": 0.0042,
    "cool_rate_coeff_fs_per_c_per_min": 0.0060
  },
  "adaptive_zero": {
    "fs_mps": 0.0014,
    "fr_m3h": 0.0016,
    "confidence": 0.82,
    "confirmed_zero_count": 14,
    "machine_zero_count": 260,
    "last_user_label_at": "2026-05-28T19:42:00Z"
  },
  "guards": {
    "max_abs_zero_fs_mps": 0.0800,
    "max_update_fs_mps_per_min": 0.0020,
    "min_event_probability_to_freeze_learning": 0.65
  },
  "recent_rollup": [
    {
      "bucket_start": "2026-05-28T19:40:00Z",
      "temperature_c": 27.4,
      "temp_rate_c_per_min": -0.03,
      "raw_fs_mps": 0.0061,
      "zero_fs_mps": 0.0058,
      "corrected_fs_mps": 0.0003,
      "raw_fr_m3h": 0.0069,
      "corrected_fr_m3h": 0.0003,
      "onboard_temperature_c": 41.4772,
      "onboard_temp_delta_c": 0.3185,
      "thermal_gradient_c": 3.5775,
      "phantom_probability": 0.86,
      "event_probability": 0.04,
      "user_label": "zero_flow",
      "template_corr": 0.9959,
      "noise_rms_v": 0.281
    }
  ]
}
```

Store `fr_m3h` in the state as a derived convenience value, but treat `fs_mps` as authoritative.

## Event storage

Events should be append-only so the model can be audited.

```json
{
  "event_id": "BB8100017587-20260528T194000Z",
  "serial": "BB8100017587",
  "start_at": "2026-05-28T19:40:00Z",
  "end_at": "2026-05-28T19:48:00Z",
  "system_type": "phantom_flow_candidate",
  "user_label": "zero_flow",
  "notification_level": "watch",
  "action_taken": "suppressed_low_confidence_flow",
  "raw_fr_total_m3": 0.0009,
  "published_fr_total_m3": 0.0,
  "model_snapshot": {
    "phantom_probability_max": 0.91,
    "event_probability_max": 0.08,
    "temperature_delta_c": -2.1,
    "zero_fs_before_mps": 0.0037,
    "zero_fs_after_mps": 0.0044
  }
}
```

## Optimizing `fs` and `fr`

For zero-flow confirmed segments:

```text
target_zero_fs = median(fs_raw - fs_temp_model)
update fs_adaptive_zero toward target_zero_fs
```

For event-flow confirmed segments:

```text
do not update zero drift
lower phantom suppression for similar future waveforms
use these samples to tune event probability
```

For known-volume calibration events, optimize the `fr` gain separately:

```text
fr_final = fs_corrected * area_m2 * 3600 * fr_gain
```

Only learn `fr_gain` when there is trusted ground truth volume. Do not use phantom-flow or no-flow periods to learn `fr_gain`; those periods are for zero drift.

## Customer workflow

Customer-facing choices should be simple:

```text
Zero flow    -> confirmed no water use; learn zero drift
Event flow   -> confirmed real water use; freeze zero learning
Not sure     -> keep low confidence; do not learn aggressively
```

Notifications should explain impact and protection:

```text
Temperature changed quickly and the meter detected possible zero drift.
Low-confidence small flow was suppressed while the meter rechecks stability.
Please confirm whether there was water use during this period.
```
