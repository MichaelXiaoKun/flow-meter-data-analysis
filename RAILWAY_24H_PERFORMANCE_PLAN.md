# Railway 24h Performance Plan

## Summary

This plan targets the current Railway live dashboard architecture:

- Improve UI responsiveness when live data points grow.
- Improve backend processing and query speed.
- Support stable 24-hour data display.
- Keep GPU support optional and focused on CNN scoring, not UI rendering.

The default route is:

1. Stop the current UI hot spots first.
2. Add durable storage and 24-hour aggregation APIs.
3. Move real-time zero/phantom/event computation to backend incremental state.
4. Add optional GPU scoring for CNN analysis without blocking the live path.

## Current Bottlenecks

- The browser currently processes each live SSE frame immediately.
- Each live frame can trigger full `runZeroTracker()` recomputation over the current sample window.
- Each live frame can trigger full `render()`, table rebuilds, event list rebuilds, waveform checks, and all canvas redraws.
- SSE backlog is useful for reconnecting recent state, but it should not be the 24-hour history mechanism.
- JSONL and CSV files are useful debug artifacts, but they are not efficient primary storage for 24-hour time-series queries.
- Waveform samples are heavy and should not be included in the main chart data path.

## Success Criteria

- A single meter can load a 24-hour view in under 2 seconds under normal Railway conditions.
- Main chart rendering uses at most 1,600 to 2,000 points per visible series.
- Browser memory stays bounded when raw backend data grows beyond 24 hours.
- Live frame arrival does not trigger full historical recomputation in the browser.
- UI remains responsive while live data is arriving.
- System works with `ENABLE_CNN=0`.
- GPU acceleration, when enabled, does not block MQTT ingest, latest-frame display, or 24-hour chart loading.

## Target Architecture

```text
MQTT meter data
  -> Analyzer Worker
     - parse waveform/pub payloads
     - extract features
     - run incremental zero/phantom/event state
     - optionally enqueue CNN scoring
  -> Postgres
     - meter_frames
     - meter_events
     - waveforms
  -> FastAPI Web API
     - latest
     - 24h aggregated series
     - events
     - waveform detail
     - live SSE stream
  -> Browser UI
     - load 24h aggregated series
     - receive only latest frames over SSE
     - render bounded/downsampled points
```

## TODO Roadmap

### Phase 0: Baseline Confirmation

- [x] Measure current SSE frame arrival rate per connected meter.
- [x] Measure current `render()` frequency during live streaming.
- [x] Measure canvas redraw time for temperature, flow, display GPM, probability, and waveform charts.
- [x] Measure browser memory after 10 minutes, 1 hour, and simulated 24 hours of data.
- [x] Measure `mqtt_stream_analyzer.py` frames processed per second.
- [x] Measure JSONL and CSV file growth per hour per meter.
- [x] Record current Railway CPU and memory usage during live data.
- [x] Define the baseline test serial and sample rate used for comparisons.

Phase 0 baseline record, captured locally on 2026-05-29:

- Baseline serial: `BB8100017587`.
- Local analysis sample: `mqtt_analysis_log.jsonl`, 123,911 rows, 144,612,502 bytes, timestamp range `2026-05-22T06:08:16.000Z` to `2026-05-29T03:32:22.000Z`.
- Derived local average: 165.40 hours, 749.2 rows/hour, 12.49 rows/minute, projected 17,980 rows/24h for this single-meter sample.
- Local event log sample: `mqtt_events.jsonl`, 1,053 rows, 1,070,801 bytes.
- Local waveform CSV sample: `live_BB8100017587.csv`, 142 rows, 591,878 bytes, 1,024 sample columns per waveform row.
- UI baseline before storage/API work: SSE backlog is 1,200 frames, live sample retention is 1,200 rows, live batch flush is 150 ms, live perf logging is every 5 s, and main chart visible points are capped at 1,600 after Phase 1 Step 2.
- Analyzer hot path: `analyzer.analyze(...)` writes every analysis record to `--log-jsonl`; optional GUI waveform CSV writes happen through `--save-csv`; MQTT loop uses `client.loop(timeout=1.0)`.
- Railway CPU/memory: not observable from this local workspace. The source of truth is Railway service metrics during a live run; record those numbers beside this baseline before Phase 2 rollout. The UI now logs enough local frame/render counters to compare before/after behavior in the browser console.

### Phase 1: UI Immediate Relief

- [x] Add a frontend live-frame queue.
- [x] Push SSE `frame` events into the queue instead of calling `appendLiveRecord()` immediately.
- [x] Flush queued frames every 100 to 200 ms.
- [x] During a flush, append all queued samples first, then render once.
- [x] Keep selected index on the latest frame only after the batch is applied.
- [x] Add a maximum visible chart point target, default `1600`.
- [x] Add a chart downsampling helper that maps full computed rows to visible rows.
- [x] Use visible/downsampled rows for all main charts.
- [x] Keep tooltip index mapping from visible rows back to original computed row indexes.
- [x] Keep audit table limited to roughly 80 to 120 rows.
- [x] Prefer latest rows, event rows, and selected row in the audit table.
- [x] Throttle `renderMeterTabs()` to at most once per second during live streaming.
- [x] Throttle training status and event list refreshes to at most once per second unless user selection changes.
- [x] Stop calling waveform loading from every full render.
- [x] Load waveform CSV only when the user selects a row or when a bounded auto-refresh interval allows it.
- [x] Verify live frame arrival does not cause one full UI render per raw frame.

Progress note:

- Step 1 implemented in `prototype/temperature_zero_flow_prototype.html`: per-meter pending live frame queue, 150 ms flush timer, batched recompute/render, stale timer cleanup, and console-only live perf counters.
- Step 2 implemented in `prototype/temperature_zero_flow_prototype.html`: `CHART_MAX_VISIBLE_POINTS=1600`, cached chart-row downsampling, min/max preservation per chart bucket, and hover/selection mapping from visible rows back to original computed row indexes.
- Step 3 implemented in `prototype/temperature_zero_flow_prototype.html`: live-mode `renderMeterTabs()`, event list, and training panel are throttled to 1 s; waveform loading is no longer triggered by every full render and remains selection-driven.
- Existing audit-table downsampling already limits displayed rows to 80 plus event/selected rows, preserving event and selected context while keeping DOM size bounded.
- Step 4 verification completed with a local extraction test of the actual queue/flush functions: 10 queued frames scheduled 1 flush, appended 10 records, ran 1 recompute, and triggered 1 render.
- Verified JavaScript syntax and local Railway UI HTTP smoke test. Full browser automation was not run because the local Node environment did not have Playwright installed.

### Phase 2: Storage and 24h Query API

- [x] Add Railway Postgres as the v1 durable storage layer.
- [x] Keep JSONL and CSV outputs as debug logs.
- [x] Add a database module that can initialize required tables.
- [x] Add `meter_frames` table.
- [x] Add `meter_events` table.
- [x] Add `waveforms` table.
- [x] Add an index on `meter_frames(serial, timestamp)`.
- [x] Add an index on `meter_events(serial, start_time)`.
- [x] Add an index on `waveforms(serial, timestamp)`.
- [x] Write analyzer frame summaries into `meter_frames`.
- [x] Write detection and notification-worthy segments into `meter_events`.
- [x] Write waveform samples into `waveforms` only when waveform data exists.
- [x] Store waveform references on frame rows with `waveform_id`.
- [x] Batch database writes, defaulting to either 100 frames or 1 second per flush.
- [x] Add a FastAPI app for the web API.
- [x] Add `GET /api/meters/{serial}/latest`.
- [x] Add `GET /api/meters/{serial}/series?range=24h&max_points=1600`.
- [x] Add `GET /api/meters/{serial}/events?range=24h`.
- [x] Add `GET /api/waveforms/{waveform_id}`.
- [x] Keep `/health` available for Railway health checks.
- [x] Keep existing static UI serving behavior while the new API is introduced.

Phase 2 progress note:

- Implemented `meter_data_store.py` with optional Postgres support behind `DATABASE_URL`, schema creation, indexed `meter_frames` / `meter_events` / `waveforms`, gzip-compressed waveform samples, and 100-frame / 1-second write flushing.
- `mqtt_stream_analyzer.py` now writes frame summaries, events, and waveform records to the store while preserving existing JSONL/CSV outputs.
- `prototype/live_server.py` now exposes the planned `/api/meters/{serial}/latest`, `/series`, `/events`, and `/api/waveforms/{waveform_id}` endpoints. They read from Postgres when `DATABASE_URL` is set and fall back to the existing JSONL logs otherwise.
- The browser now prefetches `/api/meters/{serial}/series?range=24h&max_points=1600` before opening a live SSE stream and keeps the result as a 24h history cache/status preview. The main charts still use live computed rows until backend computed fields fully replace the frontend zero tracker.
- Added standalone `web_api.py` FastAPI app with the same `/health`, `/api/meters/{serial}/latest`, `/series`, `/events`, and `/api/waveforms/{waveform_id}` contract. The current Railway entrypoint still uses the stdlib live server while the FastAPI app is tested separately with `uvicorn web_api:app`.

### Phase 3: Backend Incremental Computation

- [x] Port the browser zero-tracker logic to Python backend code.
- [x] Maintain one rolling zero-tracker state per meter serial.
- [x] On each new frame, compute only the new output row.
- [x] Do not recompute 24 hours of historical rows on each new frame.
- [x] Write backend-computed fields into `meter_frames`.
- [x] Include `zero_estimate_fs`.
- [x] Include `corrected_fs_mps`.
- [x] Include `displayed_gpm`.
- [x] Include `phantom_probability`.
- [x] Include `event_probability`.
- [x] Include `zero_probability`.
- [x] Include `state_name`.
- [x] Include `quality_status`.
- [x] Update frontend sample parsing to prefer backend-computed fields when present.
- [x] Remove the frontend dependency on full historical `runZeroTracker()` for live mode.
- [x] Keep CSV import/offline mode able to recompute locally until a replacement path exists.
- [x] Change page-load behavior to request `/api/meters/{serial}/series?range=24h` first.
- [x] Connect `/stream?serial=...` after historical series has loaded.
- [x] Use SSE only for latest incremental frames, not for 24-hour replay.

Phase 3 progress note:

- Added `backend_zero_tracker.py`, an incremental Python port of the browser zero-flow tracker. It keeps one rolling state per serial, computes only the arriving frame, and emits `zero_estimate_fs`, `corrected_fs_mps`, `displayed_gpm`, `phantom_probability`, `event_probability`, `zero_probability`, `state_name`, and `quality_status`.
- `mqtt_stream_analyzer.py` now enriches waveform and pub-only records before JSONL, Postgres, email/publish, and SSE consumers see them.
- `meter_data_store.py` now prioritizes backend computed fields and includes `state_name` / `quality_status` in bounded `/series` buckets.
- The frontend now converts `/series` history into backend-computed display rows, uses SSE with `backlog=0`, and appends live backend rows without running full-history `runZeroTracker()` in live mode. CSV/offline imports still use the browser tracker.

### Phase 4: Optional GPU Enhancement

- [x] Keep `ENABLE_CNN=0` as the default fully supported mode.
- [x] Keep CPU scoring available with `CNN_DEVICE=cpu`.
- [x] Support GPU scoring with `ENABLE_CNN=1` and `CNN_DEVICE=cuda`.
- [x] Treat GPU as optional CNN acceleration, not a requirement for 24-hour display.
- [x] Add an async CNN scoring queue.
- [x] Do not block MQTT ingest on CNN scoring.
- [x] Do not block latest-frame UI updates on CNN scoring.
- [x] Batch waveform scoring when GPU is enabled.
- [x] Write CNN results back to the related `meter_frames.cnn_analysis` field.
- [x] If GPU is unavailable, fallback to CPU scoring or skip CNN scoring based on configuration.
- [x] Log GPU availability and fallback decisions at startup.

Phase 4 progress note:

- Added `async_cnn_worker.py`, a background CNN worker with bounded queue, micro-batching, queue-full dropping for CNN-only work, and a nonblocking `submit()` path for live ingest.
- `cnn_embedding_runtime.py` now supports `score_many()` batching and logs requested vs active device, including CUDA/MPS fallback decisions.
- `mqtt_stream_analyzer.py` now treats CNN analysis as optional async enrichment. Latest frame output is written immediately with a queued/dropped CNN status, while completed CNN results are written back to Postgres through `meter_frames.cnn_analysis`.
- `railway_start.py` now wires `CNN_MODE`, `CNN_FALLBACK`, `CNN_BATCH_SIZE`, `CNN_QUEUE_SIZE`, `CNN_FLUSH_MS`, and `CNN_TOP_K`. `ENABLE_CNN=0` or a missing model continues without CNN.
- Added `phase4_cnn_smoke.py` to verify the async worker, missing-model skip fallback, real CPU CNN scoring, and CUDA-request fallback on non-CUDA machines.
- Local verification passed with the bundled `cnn_autoencoder_model.pt`: CPU scoring works, and `CNN_DEVICE=cuda` falls back to CPU with a clear message on this non-CUDA machine. The only remaining GPU item requires a CUDA-capable Railway/runtime environment.

## Data Interfaces

### `meter_frames`

Required fields:

- `id`
- `serial`
- `timestamp`
- `raw_fs_mps`
- `raw_fr_m3h`
- `temperature_c`
- `ots_temp_c`
- `zero_estimate_fs`
- `corrected_fs_mps`
- `displayed_gpm`
- `phantom_probability`
- `event_probability`
- `zero_probability`
- `state_name`
- `quality_status`
- `waveform_id`
- `cnn_analysis`

Implementation notes:

- Use `serial` and `timestamp` as the primary query path.
- Store `cnn_analysis` as JSON.
- Store fields nullable where a meter does not publish the source data.

### `meter_events`

Required fields:

- `id`
- `serial`
- `start_time`
- `end_time`
- `kind`
- `severity`
- `message`

Implementation notes:

- Events should represent user-relevant intervals, not every raw frame.
- Keep enough detail for the notification panel and audit review.

### `waveforms`

Required fields:

- `id`
- `serial`
- `timestamp`
- `samples_compressed`
- `baseline`
- `gate_start`
- `gate_end`
- `peak_index`
- `peak_abs`

Implementation notes:

- Do not send waveform samples in the main 24-hour series API.
- Load waveform detail only when a user selects a frame.
- Compression can be gzip-compressed JSON or another simple binary-safe format in v1.

## API Interfaces

### `GET /api/meters/{serial}/latest`

Returns the newest backend-computed frame for one meter.

Expected response shape:

```json
{
  "ok": true,
  "serial": "BB8100017587",
  "frame": {}
}
```

### `GET /api/meters/{serial}/series?range=24h&max_points=1600`

Returns an aggregated 24-hour chart series.

Rules:

- Default `range` is `24h`.
- Default `max_points` is `1600`.
- Clamp `max_points` to a safe server maximum.
- Return no more than `max_points` buckets.
- Each bucket should include `timestamp`, `avg`, `min`, `max`, and `latest` values for charted fields.

Expected response shape:

```json
{
  "ok": true,
  "serial": "BB8100017587",
  "range": "24h",
  "max_points": 1600,
  "points": []
}
```

### `GET /api/meters/{serial}/events?range=24h`

Returns user-relevant events in the selected time range.

Expected response shape:

```json
{
  "ok": true,
  "serial": "BB8100017587",
  "events": []
}
```

### `GET /api/waveforms/{waveform_id}`

Returns one waveform detail payload.

Expected response shape:

```json
{
  "ok": true,
  "waveform": {}
}
```

### `GET /stream?serial=...`

Streams latest frames only.

Rules:

- Do not use this endpoint as the 24-hour history transport.
- Keep a small reconnect backlog only if needed for resilience.
- Browser should load history through `/series` before opening the stream.

## Railway Deployment Plan

- [ ] Keep the current single-service deployment while Phase 1 is implemented.
- [ ] Add Railway Postgres for Phase 2.
- [ ] Introduce FastAPI inside the existing web process or as a new web entrypoint.
- [ ] Keep `railway_start.py` responsible for starting the analyzer and web server in v1.
- [ ] Consider splitting into two Railway services after the API path is stable:
  - `worker`: MQTT analyzer and database writer.
  - `web`: FastAPI and static frontend.
- [ ] Keep `APP_TOKEN` protection for private data endpoints.
- [ ] Keep `/health` unauthenticated for Railway health checks.

## Environment Variables

Existing variables to keep:

- `START_MQTT_ANALYZER`
- `SELF_TRAIN`
- `ENABLE_CNN`
- `CNN_DEVICE`
- `CNN_MODEL_PATH`
- `CNN_MODE`
- `CNN_FALLBACK`
- `CNN_BATCH_SIZE`
- `CNN_QUEUE_SIZE`
- `CNN_FLUSH_MS`
- `CNN_TOP_K`
- `APP_TOKEN`
- `RAILWAY_VOLUME_MOUNT_PATH`
- `DATA_DIR`

New variables to add:

- `DATABASE_URL`
- `SERIES_MAX_POINTS`, default `1600`
- `SERIES_DEFAULT_RANGE`, default `24h`
- `DB_WRITE_BATCH_SIZE`, default `100`
- `DB_WRITE_FLUSH_MS`, default `1000`
- `GPU_SCORING_MODE`, superseded by `CNN_MODE=async`

## Test Plan

### Backend Tests

- [ ] Generate 24 hours of simulated frames for one serial.
- [ ] Verify `meter_frames` insert throughput is acceptable.
- [ ] Verify `/api/meters/{serial}/latest` returns the newest frame.
- [ ] Verify `/api/meters/{serial}/series?range=24h&max_points=1600` returns no more than 1600 points.
- [ ] Verify `/api/meters/{serial}/events?range=24h` returns expected event intervals.
- [ ] Verify `/api/waveforms/{waveform_id}` returns exactly one waveform payload.
- [ ] Verify API behavior when no data exists for a serial.
- [ ] Verify API behavior when a serial has pub-only data and no waveform.

### Frontend Tests

- [ ] Verify UI loads 24-hour series before opening live stream.
- [ ] Verify live frame arrival is queued and batched.
- [ ] Verify chart rendering uses bounded/downsampled rows.
- [ ] Verify browser memory does not grow linearly with raw backend point count.
- [ ] Verify table row count stays bounded.
- [ ] Verify waveform is loaded only after row selection.
- [ ] Verify reconnect does not replay 24 hours through SSE.

### GPU Tests

- [x] Verify `ENABLE_CNN=0` keeps the full system usable.
- [x] Verify `CNN_DEVICE=cpu` produces CNN analysis when a real model is configured.
- [ ] Verify `CNN_DEVICE=cuda` uses GPU when available.
- [x] Verify unavailable CUDA logs a clear fallback or skip decision.
- [x] Verify CNN scoring queue backlog does not block latest-frame updates.

### Railway Smoke Tests

- [ ] Verify `/health` returns OK.
- [ ] Verify static UI loads.
- [ ] Verify `/api/meters/{serial}/latest` returns OK or a clear no-data response.
- [ ] Verify `/api/meters/{serial}/series?range=24h` returns OK or a clear no-data response.
- [ ] Verify `/stream?serial=...` connects.
- [ ] Verify `APP_TOKEN` still protects private endpoints.

## Implementation Defaults

- Plan file path: `RAILWAY_24H_PERFORMANCE_PLAN.md`.
- v1 storage: Railway Postgres.
- v1 chart max points: `1600`.
- v1 default history range: `24h`.
- v1 waveform loading: on row selection only.
- v1 GPU role: optional CNN scoring acceleration.
- v1 debug logs: keep JSONL and CSV.
- v1 deployment: keep current Railway flow until API and DB path are stable.
