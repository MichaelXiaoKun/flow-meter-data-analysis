# Waveform CSV Audit: live_BB8100017587.csv

## Summary

- Rows: 141
- Sample columns: 1024
- Time range: 2026-05-28T05:12:00+00:00 to 2026-05-28T05:16:41+00:00
- Duration seconds: 281.0
- Median interval seconds: 2.000
- CSV labels: `{'good': 140, 'unknown': 1}`
- Analyzer labels: `{'normal': 138, 'suspect': 3}`
- Peak modes: `{'peak_minus_100': 71, 'peak_plus_50': 70}`
- Raw states: `{'normal_acoustic_state': 136, 'weak_signal_or_air_candidate': 4, 'signal_quality_suspect': 1}`
- Stable states: `{'normal_acoustic_state': 141}`
- Health labels: `{'Healthy': 136, 'Watch': 5}`
- Detection events: `{}`

## Scores

| Metric | n | min | p50 | mean | p95 | max |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Health | 141 | 66.400 | 80.700 | 81.507 | 87.800 | 88.600 |
| Pattern | 141 | 47.500 | 82.600 | 82.190 | 84.500 | 85.100 |
| SQ | 140 | 99.000 | 100.000 | 99.886 | 100.000 | 100.000 |
| SQ age ms | 140 | 560.000 | 1000.000 | 990.900 | 1046.100 | 1134.000 |

## Per Mode

### peak_minus_100

| Metric | n | min | p50 | mean | p95 | max |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| noise_rms_v | 71 | 0.279 | 0.287 | 0.287 | 0.291 | 0.292 |
| gate_rms_v | 71 | 1.179 | 1.187 | 1.187 | 1.190 | 1.193 |
| template_corr | 71 | 0.992 | 0.994 | 0.994 | 0.995 | 0.995 |
| snr_db | 71 | 12.189 | 12.324 | 12.326 | 12.453 | 12.593 |
| low_clip_ratio | 71 | 0.068 | 0.071 | 0.072 | 0.075 | 0.077 |
| peak_offset_samples | 71 | -101.000 | -99.000 | -99.282 | -98.000 | -98.000 |
| first_arrival_offset_samples | 71 | -102.000 | -99.000 | -99.437 | -98.500 | -98.000 |
| score | 71 | 2.821 | 3.058 | 3.078 | 3.285 | 3.392 |
| health | 71 | 66.400 | 78.400 | 78.175 | 80.650 | 81.400 |

### peak_plus_50

| Metric | n | min | p50 | mean | p95 | max |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| noise_rms_v | 70 | 0.256 | 0.261 | 0.261 | 0.264 | 0.268 |
| gate_rms_v | 70 | 1.088 | 1.093 | 1.093 | 1.097 | 1.100 |
| template_corr | 70 | 0.994 | 0.997 | 0.996 | 0.997 | 0.997 |
| snr_db | 70 | 12.204 | 12.433 | 12.440 | 12.558 | 12.587 |
| low_clip_ratio | 70 | 0.051 | 0.055 | 0.054 | 0.057 | 0.057 |
| peak_offset_samples | 70 | 49.000 | 50.000 | 49.929 | 51.000 | 52.000 |
| first_arrival_offset_samples | 70 | -101.000 | -100.000 | -98.629 | -99.000 | -1.000 |
| score | 70 | 1.927 | 2.099 | 2.113 | 2.272 | 2.293 |
| health | 70 | 74.400 | 85.000 | 84.887 | 88.300 | 88.600 |

## Worst Rows By Score

- row 2 2026-05-28T05:12:02.000Z: score=3.392, health=66.4/Watch, mode=peak_minus_100, label=suspect, raw=weak_signal_or_air_candidate; noise_rms_v z=7.4, gate_rms_v z=4.8, low_clip_ratio z=3.3
- row 68 2026-05-28T05:14:14.000Z: score=3.374, health=70.2/Watch, mode=peak_minus_100, label=suspect, raw=weak_signal_or_air_candidate; noise_rms_v z=7.5, gate_rms_v z=4.9, low_clip_ratio z=3.1
- row 64 2026-05-28T05:14:06.000Z: score=3.326, health=74.0/Watch, mode=peak_minus_100, label=suspect, raw=signal_quality_suspect; noise_rms_v z=6.9, gate_rms_v z=4.8, low_clip_ratio z=4.1
- row 57 2026-05-28T05:13:52.000Z: score=3.286, health=76.4/Healthy, mode=peak_minus_100, label=normal, raw=normal_acoustic_state; noise_rms_v z=7.0, gate_rms_v z=4.9, low_clip_ratio z=3.3
- row 37 2026-05-28T05:13:12.000Z: score=3.284, health=77.2/Healthy, mode=peak_minus_100, label=normal, raw=normal_acoustic_state; noise_rms_v z=6.9, gate_rms_v z=4.9, low_clip_ratio z=3.7
- row 80 2026-05-28T05:14:38.000Z: score=3.277, health=77.7/Healthy, mode=peak_minus_100, label=normal, raw=normal_acoustic_state; noise_rms_v z=6.9, gate_rms_v z=4.8, low_clip_ratio z=3.7
- row 98 2026-05-28T05:15:14.000Z: score=3.266, health=76.5/Healthy, mode=peak_minus_100, label=normal, raw=weak_signal_or_air_candidate; noise_rms_v z=7.3, gate_rms_v z=4.8, low_clip_ratio z=2.7
- row 119 2026-05-28T05:15:56.000Z: score=3.228, health=76.0/Healthy, mode=peak_minus_100, label=normal, raw=normal_acoustic_state; noise_rms_v z=7.2, gate_rms_v z=4.7, low_clip_ratio z=2.5
- row 60 2026-05-28T05:13:58.000Z: score=3.224, health=76.3/Healthy, mode=peak_minus_100, label=normal, raw=normal_acoustic_state; noise_rms_v z=7.0, gate_rms_v z=4.6, low_clip_ratio z=3.3
- row 136 2026-05-28T05:16:31.000Z: score=3.222, health=76.3/Healthy, mode=peak_minus_100, label=normal, raw=normal_acoustic_state; noise_rms_v z=6.9, gate_rms_v z=4.8, low_clip_ratio z=3.1
- row 59 2026-05-28T05:13:56.000Z: score=3.207, health=77.2/Healthy, mode=peak_minus_100, label=normal, raw=normal_acoustic_state; noise_rms_v z=6.8, gate_rms_v z=4.8, low_clip_ratio z=3.3
- row 11 2026-05-28T05:12:20.000Z: score=3.200, health=77.4/Healthy, mode=peak_minus_100, label=normal, raw=normal_acoustic_state; noise_rms_v z=6.9, gate_rms_v z=4.9, low_clip_ratio z=2.9
