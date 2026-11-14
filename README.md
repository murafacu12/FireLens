# FIRELENS

Fire detection and prediction on a 3D virtual Earth. FireLens harmonizes **NASA MODIS and
VIIRS** active-fire data (NASA FIRMS as the base reference), groups detections into
**fire events**, and shows everything as layers on a **CesiumJS** globe. It adds filters,
a historical fire calendar, alert rules and a fire-danger forecast (Open-Meteo weather,
Copernicus Sentinel-2 NDVI).

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): components, data flow, roadmap
- [docs/DATA_HARMONIZATION.md](docs/DATA_HARMONIZATION.md): how MODIS and VIIRS become one model

## Quick start

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
| **Live, keyless** (default) | no `FIRELENS_FIRMS_MAP_KEY` | Real NASA FIRMS detections from the public files for one region (`FIRELENS_PUBLIC_REGION`, default `South_America`). The last 7 days are loaded on first start and refreshed hourly. |
| **Live + archive** | MAP_KEY set in `.env` | Everything above, plus **fire history**: any date range for a country or the current globe view, from NASA's science-quality archive (SP) and NRT for recent months. |
| **Demo** | `FIRELENS_DEMO_MODE=true` | Synthetic FIRMS-format data for offline UI work. |

Demo and live data use separate databases (`firelens-demo.db`, `firelens.db`) and never mix.

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
| `GET /api/detections` | Harmonized pixels. Filters: `start`, `end`, `bbox=w,s,e,n`, `lat`+`lon`+`radius_km`, `instrument`, `satellite`, `min_confidence`, `min_frp`, `max_frp`, `daynight`, `event_id` |
| `GET /api/detections/compact` | Same filters, columnar arrays (`id, lat, lon, frp, conf, viirs, t`) for map rendering. About 10x smaller, gzipped |
| `GET /api/detections/{id}` | One full detection (loaded when a point is clicked) |
| `GET /api/events` | Fire events. Filters: time overlap, area, `min_area`/`max_area`, `min_frp`, `min_detections`, `status`, `instrument`, `sort=recent\|area\|frp` |
| `GET /api/events/{id}` | Event with all its detections |
| `GET /api/calendar` | Daily detections / new events / FRP (`start`, `end`, optional area) |
| `GET/POST/DELETE /api/alerts/rules`, `GET /api/alerts` | Alert rules (box or circle + thresholds, optional webhook) and triggered alerts |
| `GET /api/risk?lat&lon` | 7-day fire-danger baseline (HDW, dryness, activity, NDVI) |
| `GET /api/ndvi?lat&lon` | Sentinel-2 NDVI (needs Copernicus credentials) |
| `POST /api/ingest` | Run an ingestion now (public files by `region`/`window`; `area`/`date`/`days` need the key) |
| `POST /api/history/backfill` | Load history for `start`..`end` and a `country` or `bbox` (needs the key) |
| `GET /api/history/jobs[/{id}]`, `DELETE /api/history/jobs/{id}` | Backfill progress / cancel |
| `GET /api/history/availability` | Date span per FIRMS product and your key's transaction usage |
| `GET /api/history/countries` | Countries loadable by name (mapped to bounding boxes) |

## Tests

```bash
cd backend && .venv/Scripts/python -m pytest
cd frontend && npm run typecheck
```

## Data sources & credits

NASA FIRMS (MODIS C6.1, VIIRS 375 m) · NASA EOSDIS GIBS imagery · Open-Meteo ·
Copernicus Sentinel-2 (Copernicus Data Space Ecosystem) · Esri World Imagery ·
OpenStreetMap contributors · CesiumJS.
