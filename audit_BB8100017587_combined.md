# Waveform CSV Audit: BB8100017587.csv

## Summary

- Rows: 17047
- Sample columns: 1024
- Time range: 2026-05-21T17:51:08.883000+00:00 to 2026-05-22T03:42:02.045000+00:00
- Duration seconds: 35453.162
- Median interval seconds: 2.006
- CSV labels: `{'good': 17047}`
- Analyzer labels: `{'normal': 16970, 'suspect': 60, 'anomaly': 17}`
- Peak modes: `{'peak_minus_100': 11824, 'peak_center': 4821, 'peak_plus_50': 402}`
- Raw states: `{'normal_acoustic_state': 16965, 'signal_quality_suspect': 60, 'waveform_anomaly': 17, 'weak_signal_or_air_candidate': 5}`
- Stable states: `{'normal_acoustic_state': 17047}`
- Health labels: `{'Excellent': 12183, 'Healthy': 4373, 'Watch': 473, 'Degraded': 18}`
- Detection events: `{}`

## Scores

| Metric | n | min | p50 | mean | p95 | max |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Health | 17047 | 55.000 | 96.500 | 91.960 | 98.400 | 98.900 |
| Pattern | 17047 | 35.900 | 98.100 | 94.000 | 99.200 | 99.800 |
| SQ | 17047 | 95.000 | 99.000 | 98.883 | 100.000 | 100.000 |
| SQ age ms | 17047 | 0.000 | 1001.000 | 1000.908 | 1057.000 | 5563.000 |

## Per Mode

### peak_center

| Metric | n | min | p50 | mean | p95 | max |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| noise_rms_v | 4821 | 0.234 | 0.242 | 0.244 | 0.260 | 0.267 |
| gate_rms_v | 4821 | 0.992 | 1.007 | 1.021 | 1.119 | 1.129 |
| template_corr | 4821 | 0.990 | 0.997 | 0.997 | 0.998 | 0.998 |
| snr_db | 4821 | 12.022 | 12.424 | 12.443 | 12.688 | 12.989 |
| low_clip_ratio | 4821 | 0.023 | 0.031 | 0.035 | 0.062 | 0.067 |
| peak_offset_samples | 4821 | -2.000 | 0.000 | -0.066 | 1.000 | 2.000 |
| first_arrival_offset_samples | 4821 | -102.000 | -100.000 | -100.364 | -100.000 | 0.000 |
| score | 4821 | 0.036 | 0.265 | 0.660 | 3.564 | 3.923 |
| health | 4821 | 55.000 | 97.100 | 95.435 | 98.600 | 98.900 |

### peak_minus_100

| Metric | n | min | p50 | mean | p95 | max |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| noise_rms_v | 11824 | 0.233 | 0.255 | 0.251 | 0.260 | 0.269 |
| gate_rms_v | 11824 | 0.992 | 1.091 | 1.064 | 1.098 | 1.109 |
| template_corr | 11824 | 0.982 | 0.997 | 0.997 | 0.998 | 0.998 |
| snr_db | 11824 | 11.640 | 12.549 | 12.532 | 12.701 | 12.887 |
| low_clip_ratio | 11824 | 0.022 | 0.057 | 0.049 | 0.062 | 0.065 |
| peak_offset_samples | 11824 | -102.000 | -99.000 | -98.981 | -98.000 | -96.000 |
| first_arrival_offset_samples | 11824 | -129.000 | -101.000 | -99.997 | -100.000 | 9999.000 |
| score | 11824 | 0.093 | 0.372 | 1.136 | 3.166 | 3.514 |
| health | 11824 | 55.000 | 96.200 | 90.497 | 98.200 | 98.700 |

### peak_plus_50

| Metric | n | min | p50 | mean | p95 | max |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| noise_rms_v | 402 | 0.240 | 0.246 | 0.250 | 0.261 | 0.266 |
| gate_rms_v | 402 | 1.024 | 1.039 | 1.062 | 1.114 | 1.122 |
| template_corr | 402 | 0.993 | 0.996 | 0.996 | 0.997 | 0.997 |
| snr_db | 402 | 12.326 | 12.558 | 12.567 | 12.710 | 12.834 |
| low_clip_ratio | 402 | 0.030 | 0.036 | 0.043 | 0.062 | 0.062 |
| peak_offset_samples | 402 | 48.000 | 50.000 | 50.206 | 51.950 | 52.000 |
| first_arrival_offset_samples | 402 | -102.000 | -100.000 | -99.963 | -99.000 | -1.000 |
| score | 402 | 0.054 | 0.310 | 0.972 | 2.515 | 2.689 |
| health | 402 | 55.000 | 96.000 | 93.336 | 98.295 | 98.800 |

## Worst Rows By Score

- row 15798 2026-05-22T03:00:16.801Z: score=3.923, health=55.0/Degraded, mode=peak_center, label=anomaly, raw=waveform_anomaly; low_clip_ratio z=7.2, gate_rms_v z=5.6, noise_rms_v z=4.7
- row 15948 2026-05-22T03:05:17.629Z: score=3.901, health=55.0/Degraded, mode=peak_center, label=anomaly, raw=waveform_anomaly; low_clip_ratio z=6.6, gate_rms_v z=5.9, noise_rms_v z=5.0
- row 16047 2026-05-22T03:08:36.224Z: score=3.880, health=55.0/Degraded, mode=peak_center, label=anomaly, raw=waveform_anomaly; low_clip_ratio z=6.8, gate_rms_v z=5.7, noise_rms_v z=4.9
- row 15933 2026-05-22T03:04:47.600Z: score=3.833, health=55.0/Degraded, mode=peak_center, label=anomaly, raw=waveform_anomaly; low_clip_ratio z=6.4, gate_rms_v z=6.1, noise_rms_v z=4.5
- row 15901 2026-05-22T03:03:43.370Z: score=3.828, health=55.0/Degraded, mode=peak_center, label=anomaly, raw=waveform_anomaly; low_clip_ratio z=6.8, gate_rms_v z=5.9, peak_abs_gate_v z=4.0
- row 16000 2026-05-22T03:07:01.975Z: score=3.823, health=55.0/Degraded, mode=peak_center, label=anomaly, raw=waveform_anomaly; low_clip_ratio z=6.8, gate_rms_v z=5.9, peak_abs_gate_v z=4.0
- row 15801 2026-05-22T03:00:22.748Z: score=3.819, health=74.0/Watch, mode=peak_center, label=suspect, raw=signal_quality_suspect; low_clip_ratio z=7.0, gate_rms_v z=5.7, noise_rms_v z=4.1
- row 15906 2026-05-22T03:03:53.396Z: score=3.818, health=73.3/Watch, mode=peak_center, label=suspect, raw=signal_quality_suspect; low_clip_ratio z=6.4, gate_rms_v z=5.8, noise_rms_v z=4.7
- row 15967 2026-05-22T03:05:55.766Z: score=3.813, health=74.0/Watch, mode=peak_center, label=suspect, raw=signal_quality_suspect; low_clip_ratio z=6.4, gate_rms_v z=6.1, noise_rms_v z=4.3
- row 15918 2026-05-22T03:04:17.456Z: score=3.811, health=74.0/Watch, mode=peak_center, label=suspect, raw=signal_quality_suspect; low_clip_ratio z=6.4, gate_rms_v z=6.0, noise_rms_v z=4.5
- row 15904 2026-05-22T03:03:49.405Z: score=3.810, health=74.0/Watch, mode=peak_center, label=suspect, raw=signal_quality_suspect; low_clip_ratio z=6.8, gate_rms_v z=6.0, peak_abs_gate_v z=4.0
- row 15899 2026-05-22T03:03:39.346Z: score=3.808, health=74.0/Watch, mode=peak_center, label=suspect, raw=signal_quality_suspect; low_clip_ratio z=6.6, gate_rms_v z=5.9, peak_abs_gate_v z=4.0
