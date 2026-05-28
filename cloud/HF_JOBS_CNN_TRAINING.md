# Hugging Face Jobs CNN Training

This folder packages the local 1D CNN autoencoder trainer for Hugging Face Jobs.

The remote job:

1. Downloads a CSV from a Hugging Face Dataset repo.
2. Runs `train_cnn_embedding_analyzer.py`.
3. Saves `cnn_autoencoder_model.pt`, `cnn_embedding_report.json`, and
   `cnn_embedding_analysis.jsonl`.
4. Uploads those artifacts to an output Hugging Face repo.

## One-Time Setup

Install the Hub client and authenticate:

```bash
python3 -m pip install "huggingface_hub>=0.35.0"
hf auth login
```

Hugging Face Jobs require a paid HF plan. Use a token with write access if you
want the job to create/upload repos.

## First Run

Replace `YOUR_HF_USER` with your Hugging Face username or organization:

```bash
python3 cloud/hf_submit_cnn_job.py \
  --local-csv BB8100017587.csv \
  --upload-csv \
  --dataset-repo YOUR_HF_USER/bluebot-waveforms \
  --csv-path BB8100017587.csv \
  --output-repo YOUR_HF_USER/bluebot-meter-cnn \
  --flavor cpu-upgrade \
  --timeout 2h \
  --epochs 12 \
  --batch-size 128
```

For a small GPU:

```bash
python3 cloud/hf_submit_cnn_job.py \
  --dataset-repo YOUR_HF_USER/bluebot-waveforms \
  --csv-path BB8100017587.csv \
  --output-repo YOUR_HF_USER/bluebot-meter-cnn \
  --flavor l4x1 \
  --timeout 2h \
  --epochs 12 \
  --batch-size 256
```

The script prints the HF Job id and URL. When the job finishes, artifacts are
uploaded under:

```text
YOUR_HF_USER/bluebot-meter-cnn/runs/<UTC timestamp>/
```

## Local Dry Run

Generate the remote UV script without submitting:

```bash
python3 cloud/hf_submit_cnn_job.py \
  --dataset-repo YOUR_HF_USER/bluebot-waveforms \
  --output-repo YOUR_HF_USER/bluebot-meter-cnn \
  --dry-run-script /private/tmp/hf_cnn_train_job.py

python3 -m py_compile /private/tmp/hf_cnn_train_job.py
```

## Production Pattern

Keep realtime MQTT inference local or on your application server:

```bash
.venv/bin/python mqtt_stream_analyzer.py \
  --broker mqtt-prod.bluebot.com \
  --port 1883 \
  --model oneclass_meter_model.json \
  --cnn-model cnn_autoencoder_model.pt \
  --sig-topic meter/sig/BB8100017587 \
  --pub-topic meter/pub/BB8100017587 \
  --processed-topic ""
```

Use HF Jobs for batch retraining:

```text
prod MQTT logs -> filter high-confidence good samples -> upload CSV -> HF Job
-> validate report -> download/promote cnn_autoencoder_model.pt
```

Avoid letting a cloud training job subscribe directly to production MQTT for
online learning. Batch retraining gives you a review gate before replacing the
model used by realtime monitoring.
