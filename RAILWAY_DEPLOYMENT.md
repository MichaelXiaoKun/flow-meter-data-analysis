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
METER_SERIAL=BB8100017587
MQTT_BROKER=mqtt-prod.bluebot.com
MQTT_PORT=1883
MQTT_SIG_TOPIC=meter/sig/{serial}
MQTT_PUB_TOPIC=meter/pub/{serial}
MQTT_PROCESSED_TOPIC=
START_MQTT_ANALYZER=1
SELF_TRAIN=1
ENABLE_CNN=0
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

## Persistent data

Attach a Railway Volume to the service if you want logs, waveform CSV, and the
adapted model to survive restarts. Railway exposes the mount path as
`RAILWAY_VOLUME_MOUNT_PATH`, and the entrypoint will use it automatically.

Files written there:

- `live_mqtt_analysis.jsonl`
- `live_mqtt_events.jsonl`
- `live_<serial>_realtime.csv`
- `live_adaptive_meter_model.json`

Without a volume, these files are written to `/tmp/flow-meter-data` and may be
lost on restart.

## Useful endpoints

- `/temperature_zero_flow_prototype.html` - main UI.
- `/stream?serial=BB8100017587&backlog=300` - Server-Sent Events.
- `/waveform.csv?serial=BB8100017587` - waveform CSV used by the UI.
- `/health` - Railway health check.
- `/status` - health and file-path status.

When `APP_TOKEN` is set, `/stream`, `/waveform.csv`, and `/status` require the
same `?token=...` query parameter.

## Local smoke test

Run only the web UI without starting MQTT:

```bash
PORT=8766 START_MQTT_ANALYZER=0 python railway_start.py
```

Then open:

```text
http://127.0.0.1:8766/temperature_zero_flow_prototype.html
```
