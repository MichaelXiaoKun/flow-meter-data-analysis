# Railway deployment

This repo can run the temperature zero-flow prototype as one Railway web
service. The entrypoint is `railway_start.py`, which starts:

- `mqtt_stream_analyzer.py` for live MQTT waveform/pub ingestion.
- `prototype/live_server.py` for the browser UI, SSE stream, and waveform CSV.

## Required Railway settings

Create a Railway service from this GitHub repo. The checked-in `railway.json`
sets the start command:

```bash
python railway_start.py
```

Generate a public domain for the service, then open:

```text
https://<your-service>.up.railway.app/temperature_zero_flow_prototype.html
```

## Recommended variables

Set these in Railway service variables:

```bash
MQTT_BROKER=mqtt-prod.bluebot.com
MQTT_PORT=1883
MQTT_CLIENT_ID=lens_data_{uuid}
MQTT_SIG_TOPIC=meter/sig/{serial}
MQTT_PUB_TOPIC=meter/pub/{serial}
MQTT_PROCESSED_TOPIC=
START_MQTT_ANALYZER=1
SELF_TRAIN=1
ENABLE_CNN=0
```

CNN/GPU scoring is optional and runs outside the live ingest path. Leave
`ENABLE_CNN=0` for the stable default. To enable async CNN scoring, provide a
checkpoint and choose a device:

```bash
ENABLE_CNN=1
CNN_MODEL_PATH=/app/cnn_autoencoder_model.pt
CNN_DEVICE=cpu          # cpu, cuda, mps, or auto
CNN_MODE=async
CNN_FALLBACK=cpu        # cpu, skip, or error if the requested device is unavailable
CNN_BATCH_SIZE=8
CNN_QUEUE_SIZE=512
CNN_FLUSH_MS=100
CNN_TOP_K=3
CNN_HEALTH_WEIGHT=0
```

With `CNN_DEVICE=cuda`, the worker uses GPU when PyTorch can see CUDA. If CUDA
is unavailable, the default `CNN_FALLBACK=cpu` keeps the 24-hour UI and MQTT
ingest running and logs the fallback decision. Async CNN results are written
back to `meter_frames.cnn_analysis` when Postgres is enabled; latest-frame UI
updates do not wait for CNN scoring.

Local Phase 4 smoke test:

```bash
python3 phase4_cnn_smoke.py
```

On a non-CUDA machine, this should still pass and report a CUDA request falling
back to CPU. On a GPU Railway service, the same smoke should report
`active: cuda` when `CNN_DEVICE=cuda` and a compatible PyTorch/CUDA runtime are
available.

For 24-hour history APIs, attach Railway Postgres and expose its connection
string as:

```bash
DATABASE_URL=<railway-postgres-connection-url>
SERIES_DEFAULT_RANGE=24h
SERIES_MAX_POINTS=1600
DB_WRITE_BATCH_SIZE=100
DB_WRITE_FLUSH_MS=1000
```

When `DATABASE_URL` is not set, the API endpoints remain available but read
from the existing JSONL debug logs instead of Postgres.

The analyzer now computes display-ready zero-flow fields in Python before data
is written to JSONL/Postgres. These optional environment variables tune the
backend tracker and default to the prototype UI values:

```bash
ZERO_TRACKER_PIPE_OD_MM=26.67
ZERO_TRACKER_PIPE_WALL_MM=2.87
ZERO_TRACKER_DEADBAND_M3H=0.030
ZERO_TRACKER_TEMP_COEFF_FS=0.0020
ZERO_TRACKER_ALPHA=0.045
ZERO_TRACKER_ALERT_THRESHOLD=0.70
ZERO_TRACKER_SUPPRESS=1
ZERO_TRACKER_FEEDBACK=1
ZERO_TRACKER_SPLIT_COOLING=1
```

The API contract is also available as a standalone FastAPI app:

```bash
uvicorn web_api:app --host 0.0.0.0 --port $PORT
```

The current `railway_start.py` still uses `prototype/live_server.py` so the
static UI and SSE stream remain unchanged while the FastAPI app is tested.

The topic values above are templates. The browser UI is the device selector:
add up to 10 serial numbers in the left rail and click `Connect live` for the
meters you want active. The server writes those connected UI serials into
`live_meter_serials.json`; the MQTT analyzer replaces `{serial}` with each UI
serial and subscribes to concrete topics such as
`meter/sig/BB8100017587` and `meter/pub/BB8100017587`. No redeploy is needed
when adding or removing UI tabs.

Meters that do not support waveform still work through `meter/pub/{serial}`:
the analyzer emits `source_type=pub_only` live samples using publish-side
fs/fr, diagnose dt/tt, SQ, pipe geometry, and onboard temperature when those
fields are present. The UI shows the waveform panel as telemetry-only instead
of trying to read a waveform CSV row.

For compatibility, a `+` topic segment is also treated as a UI serial
placeholder by the analyzer when `live_meter_serials.json` is active. It does
not subscribe to broker wildcards in the default Railway flow.

If you are running `mqtt_stream_analyzer.py` directly without Railway's
UI-managed `--serials-json` flow, pass concrete topics:

```bash
python mqtt_stream_analyzer.py \
  --sig-topic meter/sig/BB8100017587 \
  --pub-topic meter/pub/BB8100017587
```

For a public Railway domain, set a read token:

```bash
APP_TOKEN=<long-random-token>
```

Then open the app with:

```text
https://<your-service>.up.railway.app/temperature_zero_flow_prototype.html?token=<long-random-token>
```

The token is required for live SSE data and waveform CSV. The `/health`
endpoint remains open for Railway health checks.

If the broker requires auth or TLS, also set:

```bash
MQTT_USERNAME=<username>
MQTT_PASSWORD=<password>
MQTT_TLS=1
```

## Notifications

SMTP email notifications are disabled in this prototype. Keep email/SMTP
credentials out of Railway variables; the dashboard still shows local review
messages and writes live analysis/events to the data directory.

## Persistent data

Attach a Railway Volume to the service if you want logs, waveform CSV, and the
adapted model to survive restarts. Railway exposes the mount path as
`RAILWAY_VOLUME_MOUNT_PATH`, and the entrypoint will use it automatically.

Files written there:

- `live_mqtt_analysis.jsonl`
- `live_mqtt_events.jsonl`
- `live_notifications.jsonl`
- `live_mqtt_waveforms.csv`
- `live_meter_serials.json`
- `live_adaptive_meter_model.json`

Without a volume, these files are written to `/tmp/flow-meter-data` and may be
lost on restart.

The browser UI only reconnects on page load for serial numbers that were
previously connected. It first loads bounded 24-hour history from `/series`,
then opens `/stream` with backlog disabled and consumes only new SSE frames. A
browser refresh only restarts the browser connection; the Railway analyzer
process keeps ingesting MQTT for the connected UI serials listed in
`live_meter_serials.json`.

## Useful endpoints

- `/temperature_zero_flow_prototype.html` - main UI.
- `/stream?serial=BB8100017587&backlog=0` - latest-only Server-Sent Events.
- `/api/meters/BB8100017587/latest` - newest analysis frame.
- `/api/meters/BB8100017587/series?range=24h&max_points=1600` - bounded 24h series.
- `/api/meters/BB8100017587/events?range=24h` - recent event history.
- `/api/waveforms/<waveform_id>` - single waveform detail when Postgres is enabled.
- `/waveform.csv?serial=BB8100017587` - waveform CSV used by the UI.
- `/meters` - UI-managed list of serials the MQTT analyzer should subscribe to.
- `POST /clear-data?serial=BB8100017587` - remove that serial's file-backed
  analysis rows, event rows, and waveform CSV rows.
- `/health` - Railway health check.
- `/status` - health and file-path status.

When `APP_TOKEN` is set, `/stream`, `/waveform.csv`, `/meters`, `/clear-data`,
and `/status` require the same `?token=...` query parameter.

## Local smoke test

Run only the web UI without starting MQTT:

```bash
PORT=8766 START_MQTT_ANALYZER=0 python railway_start.py
```

Then open:

```text
http://127.0.0.1:8766/temperature_zero_flow_prototype.html
```
