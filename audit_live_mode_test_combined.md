# Waveform CSV Audit: live_mode_test.csv

## Summary

- Rows: 493
- Sample columns: 1024
- Time range: 2026-05-28T17:24:16+00:00 to 2026-05-28T17:40:43+00:00
- Duration seconds: 987.0
- Median interval seconds: 2.000
- CSV labels: `{'good': 493}`
- Analyzer labels: `{'normal': 478, 'suspect': 12, 'anomaly': 3}`
- Peak modes: `{'peak_minus_100': 492, 'peak_center': 1}`
- Raw states: `{'normal_acoustic_state': 474, 'signal_quality_suspect': 9, 'weak_signal_or_air_candidate': 7, 'waveform_anomaly': 3}`
- Stable states: `{'normal_acoustic_state': 493}`
- Health labels: `{'Healthy': 402, 'Excellent': 76, 'Watch': 12, 'Degraded': 3}`
- Detection events: `{}`

## Scores

| Metric | n | min | p50 | mean | p95 | max |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Health | 493 | 55.000 | 81.600 | 83.555 | 91.600 | 94.600 |
| Pattern | 493 | 36.500 | 83.100 | 87.462 | 96.300 | 97.700 |
| SQ | 493 | 99.000 | 100.000 | 99.968 | 100.000 | 100.000 |
| SQ age ms | 493 | 554.000 | 1001.000 | 1000.093 | 1057.200 | 1447.000 |

## Per Mode

### peak_center

| Metric | n | min | p50 | mean | p95 | max |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| noise_rms_v | 1 | 0.263 | 0.263 | 0.263 | 0.263 | 0.263 |
| gate_rms_v | 1 | 1.085 | 1.085 | 1.085 | 1.085 | 1.085 |
| template_corr | 1 | 0.997 | 0.997 | 0.997 | 0.997 | 0.997 |
| snr_db | 1 | 12.315 | 12.315 | 12.315 | 12.315 | 12.315 |
| low_clip_ratio | 1 | 0.056 | 0.056 | 0.056 | 0.056 | 0.056 |
| peak_offset_samples | 1 | -1.000 | -1.000 | -1.000 | -1.000 | -1.000 |
| first_arrival_offset_samples | 1 | -101.000 | -101.000 | -101.000 | -101.000 | -101.000 |
| score | 1 | 2.692 | 2.692 | 2.692 | 2.692 | 2.692 |
| health | 1 | 84.600 | 84.600 | 84.600 | 84.600 | 84.600 |

### peak_minus_100

| Metric | n | min | p50 | mean | p95 | max |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| noise_rms_v | 492 | 0.259 | 0.283 | 0.276 | 0.291 | 0.295 |
| gate_rms_v | 492 | 1.078 | 1.175 | 1.134 | 1.184 | 1.191 |
| template_corr | 492 | 0.993 | 0.996 | 0.996 | 0.998 | 0.998 |
| snr_db | 492 | 12.041 | 12.264 | 12.260 | 12.376 | 12.449 |
| low_clip_ratio | 492 | 0.050 | 0.071 | 0.064 | 0.076 | 0.080 |
| peak_offset_samples | 492 | -102.000 | -100.000 | -99.992 | -98.000 | -97.000 |
| first_arrival_offset_samples | 492 | -102.000 | -101.000 | -100.762 | -100.000 | -97.000 |
| score | 492 | 0.384 | 2.820 | 1.976 | 3.274 | 3.498 |
| health | 492 | 55.000 | 81.600 | 83.552 | 91.600 | 94.600 |

## Worst Rows By Score

- row 105 2026-05-28T17:27:45.000Z: score=3.498, health=55.0/Degraded, mode=peak_minus_100, label=anomaly, raw=waveform_anomaly; noise_rms_v z=8.0, gate_rms_v z=4.6, low_clip_ratio z=3.1
- row 157 2026-05-28T17:29:29.000Z: score=3.430, health=55.0/Degraded, mode=peak_minus_100, label=anomaly, raw=waveform_anomaly; noise_rms_v z=7.7, gate_rms_v z=4.5, low_clip_ratio z=3.5
- row 87 2026-05-28T17:27:09.000Z: score=3.427, health=55.0/Degraded, mode=peak_minus_100, label=anomaly, raw=waveform_anomaly; noise_rms_v z=7.5, gate_rms_v z=4.7, low_clip_ratio z=3.9
- row 89 2026-05-28T17:27:13.000Z: score=3.396, health=67.6/Watch, mode=peak_minus_100, label=suspect, raw=signal_quality_suspect; noise_rms_v z=7.4, gate_rms_v z=4.6, low_clip_ratio z=3.9
- row 144 2026-05-28T17:29:03.000Z: score=3.377, health=68.2/Watch, mode=peak_minus_100, label=suspect, raw=weak_signal_or_air_candidate; noise_rms_v z=7.5, gate_rms_v z=4.3, low_clip_ratio z=3.5
- row 100 2026-05-28T17:27:35.000Z: score=3.374, health=70.0/Watch, mode=peak_minus_100, label=suspect, raw=signal_quality_suspect; noise_rms_v z=7.6, gate_rms_v z=4.4, low_clip_ratio z=3.5
- row 3 2026-05-28T17:24:20.000Z: score=3.374, health=69.9/Watch, mode=peak_minus_100, label=suspect, raw=signal_quality_suspect; noise_rms_v z=7.5, gate_rms_v z=4.6, low_clip_ratio z=3.5
- row 221 2026-05-28T17:31:37.000Z: score=3.373, health=69.2/Watch, mode=peak_minus_100, label=suspect, raw=signal_quality_suspect; noise_rms_v z=7.7, gate_rms_v z=4.4, low_clip_ratio z=3.1
- row 352 2026-05-28T17:36:00.000Z: score=3.368, health=70.2/Watch, mode=peak_minus_100, label=suspect, raw=weak_signal_or_air_candidate; noise_rms_v z=7.5, gate_rms_v z=4.6, low_clip_ratio z=3.3
- row 112 2026-05-28T17:27:59.000Z: score=3.349, health=71.9/Watch, mode=peak_minus_100, label=suspect, raw=weak_signal_or_air_candidate; noise_rms_v z=7.7, gate_rms_v z=4.5, low_clip_ratio z=2.7
- row 43 2026-05-28T17:25:40.000Z: score=3.347, health=74.0/Watch, mode=peak_minus_100, label=suspect, raw=signal_quality_suspect; noise_rms_v z=7.4, gate_rms_v z=4.5, low_clip_ratio z=3.7
- row 391 2026-05-28T17:37:18.000Z: score=3.343, health=73.7/Watch, mode=peak_minus_100, label=suspect, raw=signal_quality_suspect; noise_rms_v z=7.5, gate_rms_v z=4.4, low_clip_ratio z=3.5
