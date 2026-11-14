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

Without configuration the API starts in **demo mode**. It generates 30 days of synthetic
data in the exact FIRMS CSV format, which runs through the real normalization and
clustering pipeline. The UI shows a `DEMO DATA` badge.

### Live NASA data

1. Get a free MAP_KEY at https://firms.modaps.eosdis.nasa.gov/api/map_key/
2. Copy `.env.example` to `.env` and set `FIRELENS_FIRMS_MAP_KEY`.
3. Delete `firelens.db` (demo data) and restart the API, then load data:

```bash
curl -X POST localhost:8000/api/ingest -H "Content-Type: application/json" -d '{"days": 2}'
```

Set `FIRELENS_INGEST_INTERVAL_MINUTES=60` for continuous near-real-time ingestion. For
history (calendar), ingest the SP archive: `{"sources": ["VIIRS_SNPP_SP"], "date": "2025-01-01", "days": 30, "area": "-75,-56,-53,-21"}`.

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
| `POST /api/ingest` | Run an ingestion (FIRMS or demo) |

## Tests

```bash
cd backend && .venv/Scripts/python -m pytest
cd frontend && npm run typecheck
```

## Data sources & credits

NASA FIRMS (MODIS C6.1, VIIRS 375 m) · NASA EOSDIS GIBS imagery · Open-Meteo ·
Copernicus Sentinel-2 (Copernicus Data Space Ecosystem) · Esri World Imagery ·
OpenStreetMap contributors · CesiumJS.
