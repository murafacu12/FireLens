# FIRELENS

Fire detection and prediction on a 3D virtual Earth. FireLens harmonizes **NASA MODIS and
VIIRS** active-fire data (NASA FIRMS as the base reference), groups detections into
**fire events**, and shows everything as layers on a **CesiumJS** globe. It adds filters,
a historical fire calendar, alert rules and a fire-danger forecast (Open-Meteo weather,
Copernicus Sentinel-2 NDVI).

- **[docs/FIRELENS_DOCUMENTATION.md](docs/FIRELENS_DOCUMENTATION.md): the complete documentation**
  (features, usage, architecture, API, data, limits, changelog, roadmap)
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): components, data flow, roadmap
- [docs/DATA_HARMONIZATION.md](docs/DATA_HARMONIZATION.md): how MODIS and VIIRS become one model
- [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md): server deployment guide (Oracle Cloud + Vercel; prepared, not deployed)

## Try FIRELENS on Windows (no installation of Python or Node)

1. Open the repository's **[Releases](https://github.com/murafacu12/FireLens/releases/latest)** and
   download **`FIRELENS-1.0.0-windows-x64.zip`** (about 100 MB).
2. Unzip it anywhere (e.g. Desktop) and double-click **`FIRELENS\FIRELENS.exe`**.
3. Windows may show *"Windows protected your PC"* because the app is not code-signed: click
   **More info → Run anyway**.
4. A console window opens and, a moment later, FIRELENS opens in your browser
   (`http://127.0.0.1:8765`). **Keep the window open** while you use it; close it to stop FIRELENS.

What to expect:

- **First start** (internet needed): FIRELENS downloads the last 24 hours of real NASA FIRMS fire
  data for the whole planet, about a minute or two. The map fills in as it arrives. While it runs,
  it refreshes every hour and keeps what it loaded, so the history grows.
- **Without internet** on the first start it shows **demo data** (synthetic fires), so every
  screen can still be explored.
- Everything is stored in your user folder, `%LOCALAPPDATA%\FIRELENS` (database, map-context
  tiles, log). Delete that folder to start fresh. Nothing is installed system-wide and no
  administrator rights are needed.
- Requirements: Windows 10 or 11 (64-bit), 4 GB RAM or more, a recent Chrome, Edge or Firefox.
- Optional keys go in `%LOCALAPPDATA%\FIRELENS\.env`: `FIRELENS_FIRMS_MAP_KEY` (load any past
  dates) and `FIRELENS_COPERNICUS_CLIENT_ID` / `_SECRET` (Sentinel satellite readings per fire).
  FIRELENS works fully without them.

**Build the app yourself** (developers): with Node 20+ and the backend's `.venv`
(`pip install -e ".[desktop]"`), run
`powershell -ExecutionPolicy Bypass -File backend\desktop\build.ps1`. It produces the app folder,
the portable zip and, if [Inno Setup 6](https://jrsoftware.org/isinfo.php) is installed, an
installer `FIRELENS-Setup-1.0.0.exe`. See `backend/firelens.spec` and `backend/desktop/`.

## Quick start (from source)

Requirements: Python 3.11+, Node 20+.

```bash
cd backend
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]"   # macOS/Linux: .venv/bin/python
.venv/Scripts/python -m uvicorn app.main:app --port 8000
```

```bash
cd frontend
npm install
npm run dev        # http://localhost:5173 (proxies /api to :8000)
```

## Data modes

| Mode | When | What you get |
|---|---|---|
| **Live, keyless** (default) | no `FIRELENS_FIRMS_MAP_KEY` | Real NASA FIRMS detections from the public files for the whole world (`FIRELENS_PUBLIC_REGION`, default `Global`). The last 7 days are loaded on first start and refreshed hourly. |
| **Live + archive** | MAP_KEY set in `.env` | Everything above, plus **fire history**: any date range for a country or the current globe view, from NASA's science-quality archive (SP) and NRT for recent months. |
| **Demo** | `FIRELENS_DEMO_MODE=true` | Synthetic FIRMS-format data for offline UI work. |

Demo and live data use separate databases (`firelens-demo.db`, `firelens.db`) and never mix.

### Latest vs History (Filters → Time)

- **Latest**: a rolling window back from now (24 h, 48 h or 7 days). It shows how old the newest
  satellite pass is (NASA publishes about 3 h after the overpass), refreshes itself every 5 minutes,
  and has a button to pull the newest NASA data immediately.
- **History**: pick a day or a range (‹ › step through days; or click a day, or drag across several, in the Calendar). FireLens
  records which days and areas it has loaded (`GET /api/history/coverage`). If the selected days
  aren't loaded for the area in view, it loads them from the NASA archive automatically, for views
  under 60° across and up to 31 days. This needs a MAP_KEY; each day costs one request per satellite.

### Loading fire history

1. Get a free MAP_KEY at https://firms.modaps.eosdis.nasa.gov/api/map_key/
2. Copy `.env.example` to `.env` (git-ignored), set `FIRELENS_FIRMS_MAP_KEY`, and restart the API.
3. In the UI, open **Calendar → Fire history**, pick dates and a country (or the current view), and press **Load history**.

Or use the API:

```bash
curl -X POST localhost:8000/api/history/backfill -H "Content-Type: application/json" -d '{"start": "2026-07-25", "end": "2026-08-24", "country": "ARG"}'
```

For each satellite and day the job uses the SP archive where FIRMS has it, otherwise NRT. It
splits requests to the API's 5-day limit and stores and clusters each chunk as it arrives. Progress
is at `GET /api/history/jobs/{id}`. Measured: one month of Argentina took 28 requests and ~2 minutes,
producing ~41k detections. A MAP_KEY allows 5,000 requests per 10 minutes.

FIRMS has retired its country endpoint, so countries are loaded by bounding box
(`GET /api/history/countries`). A box also includes slivers of neighbouring countries.

## API

Interactive docs at http://localhost:8000/docs.

| Endpoint | Purpose |
|---|---|
| `GET /api/tiles/{cells\|points}/{z}/{x}/{y}` | Map tiles by zoom level: grouped markers per sensor (z 0–7) or individual detections + events (z 7). Same filters plus `hours` (rolling window). ETag + 304 |
| `GET /api/analysis/summary` | Fire statistics and trends for an area and period (`bbox`, `hours` or `start`/`end`). See [docs/ANALYSIS_ENGINE.md](docs/ANALYSIS_ENGINE.md) |
| `GET /api/analysis/sensors` | MODIS vs VIIRS on coincident observations: agreement, FRP of the same fire, a VIIRS-vs-VIIRS baseline. See [docs/SENSOR_COMPARISON.md](docs/SENSOR_COMPARISON.md) |
| `GET /api/tiles/meta` | `data_version` and current hour: when the map's tiles go stale |
| `GET /api/export/{modis\|viirs}.csv` | One sensor's detections with its native field names (`start`, `end`, `bbox`) |
| `GET /api/detections` | Harmonized pixels. Filters: `start`, `end`, `bbox=w,s,e,n`, `lat`+`lon`+`radius_km`, `instrument`, `satellite`, `min_confidence`, `min_frp`, `max_frp`, `daynight`, `event_id` |
| `GET /api/detections/compact` | Same filters, columnar arrays (`id, lat, lon, frp, conf, viirs, t`) for map rendering. About 10x smaller, gzipped |
| `GET /api/detections/{id}` | One full detection (loaded when a point is clicked) |
| `GET /api/events` | Fire events. Filters: time overlap, area, `min_area`/`max_area`, `min_frp`, `min_detections`, `status`, `lifecycle_state`, `min_evidence`, `instrument`, `sort=recent\|area\|frp` |
| `GET /api/events/{id}` | Event with all its detections and its `perimeter` (outline of the detected footprint). An id that was merged or renamed resolves to the current event (`resolved_from`) |
| `GET /api/events/{id}/timeline` | Whole-fire FRP per satellite overpass (MODIS and VIIRS kept apart) |
| `GET /api/events/{id}/history`, `/lineage` | Every recorded change (created, grown, state, merged, split, renamed) and where ids went |
| `GET /api/calendar` | Daily detections / new events / FRP (`start`, `end`, optional area) |
| `GET/POST/DELETE /api/alerts/rules`, `GET /api/alerts` | Alert rules (box or circle + thresholds, optional webhook) and triggered alerts |
| `GET /api/risk?lat&lon` | 7-day fire-danger baseline (HDW, dryness, activity, NDVI) |
| `GET /api/ndvi?lat&lon` | Sentinel-2 NDVI (needs Copernicus credentials) |
| `POST /api/ingest` | Run an ingestion now (public files by `region`/`window`; `area`/`date`/`days` need the key) |
| `POST /api/history/backfill` | Load history for `start`..`end` and a `country` or `bbox` (needs the key) |
| `GET /api/history/jobs[/{id}]`, `DELETE /api/history/jobs/{id}` | Backfill progress / cancel |
| `GET /api/history/availability` | Date span per FIRMS product and your key's transaction usage |
| `GET /api/history/countries` | Countries loadable by name (mapped to bounding boxes) |

Database layout, zoom pyramid and caching: [docs/DATABASE.md](docs/DATABASE.md).
Fire events: [model](docs/FIRE_EVENT_MODEL.md) · [algorithm](docs/CLUSTERING_ALGORITHM.md) · [audit](docs/FIRE_CLUSTERING_AUDIT.md) · [benchmarks](docs/CLUSTERING_BENCHMARKS.md) · [limitations](docs/CLUSTERING_LIMITATIONS.md) · [migration v3](docs/MIGRATION_V3.md) · [release report](docs/RELEASE_V3.md).

## Tests

```bash
cd backend && .venv/Scripts/python -m pytest
cd frontend && npm run typecheck
```

## Data sources & credits

NASA FIRMS (MODIS C6.1, VIIRS 375 m) · NASA EOSDIS GIBS imagery · Open-Meteo ·
Copernicus Sentinel-2 (Copernicus Data Space Ecosystem) · Esri World Imagery ·
OpenStreetMap contributors · CesiumJS.
