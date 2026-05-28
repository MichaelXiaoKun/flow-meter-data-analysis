# MQTT Streaming Analyzer

The online side of the self-training pipeline. It subscribes to the same
MQTT topics that `bluebot-rainbird-test-gui` uses, so a single broker session
can feed both the dashboard and this analyzer with identical data.

## Topics (default — match `server.js`)

| Purpose          | Topic                  | Payload                                                                                  |
| ---------------- | ---------------------- | ---------------------------------------------------------------------------------------- |
| Waveform frames  | `meter/sig/<NUI>`      | JSON `{"numbers":[…]}` *or* raw hex bytes (each byte → `byte/10`)                        |
| Signal quality   | `meter/pub/<NUI>`      | JSON object containing `diagnose.sq`                                                     |
| Processed        | `processed/meter/<NUI>`| JSON object also containing `diagnose.sq`                                                |

The analyzer caches the latest `diagnose.sq` per device with a 5-minute
freshness window (same rule the GUI applies) and attaches the derived
`sq_label` (`good` / `fair` / `poor` / `unknown`) to each waveform.

Legacy free-form payloads (`samples`, `waveform`, or `s_0…s_N` keys) and the
single-topic override (`--topic 'meters/+/adc'`) still work.

## Run

```bash
python3 -m pip install -r requirements.txt
```

Subscribe to the production broker and analyze in real time:

```bash
python3 mqtt_stream_analyzer.py \
  --broker mqtt-prod.bluebot.com \
  --port 1883 \
  --model oneclass_meter_model.json \
  --log-jsonl mqtt_analysis_log.jsonl
```

Enable conservative self-training:

```bash
python3 mqtt_stream_analyzer.py \
  --broker mqtt-prod.bluebot.com \
  --model oneclass_meter_model.json \
  --self-train \
  --adapted-model-out adaptive_meter_model.json
```

Also capture frames to a CSV in the GUI's exact format (drop-in for the
offline trainers):

```bash
python3 mqtt_stream_analyzer.py \
  --broker mqtt-prod.bluebot.com \
  --model oneclass_meter_model.json \
  --save-csv ~/BluebotDatasets/live_capture.csv
```

Test without a broker by piping JSONL:

```bash
python3 mqtt_stream_analyzer.py --stdin --self-train < sample_payloads.jsonl
```

## Self-Training Guard

The analyzer only updates the local profile when **all** of these are true:

- `sq_label == "good"` (i.e. fresh `diagnose.sq` ≥ `--thr-fair`, default 80)
- one-class label is `normal`
- anomaly score is comfortably below the suspect threshold
- template correlation, SNR, and gate energy are healthy
- clipping is not excessive
- a rolling window is at least 95 % stable

`fair`, `poor`, and `unknown` (no fresh SQ) all freeze learning so empty
pipe, air bubbles, weak coupling, ADC clipping, or zero-drift events do
not become the new normal. Pass `--allow-fair` to also adapt on `fair`
rows; `poor` and `unknown` still freeze unconditionally.

## Offline training on GUI captures

The CSVs that the dashboard writes already carry per-row labels. Train
the one-class model on `good`-only rows:

```bash
python3 train_oneclass_meter_model.py BB8100017587.csv --labels good
```

Same flag is available on the unsupervised clustering trainer:

```bash
python3 train_unsupervised_meter_model.py BB8100017587.csv --labels good,fair
```
