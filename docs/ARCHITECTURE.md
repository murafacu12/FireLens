# FireLens architecture

## Goal

A visual, more precise successor to NASA FIRMS. It offers harmonized MODIS + VIIRS
detections grouped into fire events on a 3D Earth twin (CesiumJS), plus filters, a
historical fire calendar, alerts, and fire-danger prediction from weather, fuel and trends.

```
                 ┌──────────────────────── backend (FastAPI, Python) ────────────────────────┐
 NASA FIRMS ───► │ ingest/firms_client ─► ingest/normalize ─► services/store ─► ingest/clustering │
 (MODIS, VIIRS)  │   (CSV, chunked)        (canonical model)   (upsert, SP>NRT)   (fire events)    │
                 │                                                    │                         │
                 │                                       services/alerts (rules → alerts,      │
                 │                                       webhooks)                              │
 Open-Meteo ───► │ services/risk (HDW + dryness + activity + fuel)                               │
 Copernicus  ──► │ services/ndvi (Sentinel-2 L2A NDVI, Statistical API)                          │
                 │ api.py  /api/detections /events /calendar /alerts /risk /ndvi /ingest        │
                 └───────────────────────────────▲───────────────────────────────────────────────┘
                                                 │ JSON
                 ┌──────────────── frontend (Vite + TypeScript + CesiumJS) ────────────────┐
 Esri / OSM ───► │ globe.ts: base layers, NASA GIBS (VIIRS true colour, NDVI), detections,  │
 NASA GIBS  ───► │           events, alert zones, picking                                   │
                 │ main.ts: filters · layers · calendar · alerts · details.ts (risk)        │
                 └──────────────────────────────────────────────────────────────────────────┘
```

## Backend modules

| Module | Responsibility |
|---|---|
| `ingest/sources.py` | Registry of FIRMS products (instrument, platform, NRT/SP, resolution). |
| `ingest/firms_client.py` | FIRMS area API client. Splits long ranges into chunks within `DAY_RANGE` and redacts the MAP_KEY from errors. |
| `ingest/normalize.py` | MODIS/VIIRS → `FireDetection`. See [DATA_HARMONIZATION.md](DATA_HARMONIZATION.md). |
| `ingest/clustering.py` | Detections → fire events (space-time union-find, footprint area). |
| `ingest/demo.py` | Synthetic FIRMS-format CSVs, so the full pipeline runs without a key. |
| `services/store.py` | Upsert (SP supersedes NRT). Rebuilds only the events touched by new data (the window grows until no event straddles it). |
| `services/queries.py` | Filters: time, bbox (antimeridian-safe), point+radius, sensor, satellite, confidence, FRP, day/night, event size/status. Daily calendar aggregates. |
| `services/alerts.py` | Rules (box or circle + thresholds) evaluated on active events after each ingest. Deduplicated per (rule, event). Severity levels. Best-effort webhooks. |
| `services/risk.py` | Baseline fire-danger forecast (7 days). Transparent heuristic, see below. |
| `services/ndvi.py` | Copernicus Data Space OAuth + Statistical API, cloud-masked mean NDVI. |
| `services/pipeline.py` | Orchestrates fetch → normalize → store → events → alerts. Logs each run. |
| `main.py` | App factory, lifespan (DB, HTTP client, demo seed, optional scheduler). |

### Fire-danger baseline (`/api/risk`)

`score = 100 × Σ wᵢ·cᵢ / Σ wᵢ`, with components in 0–1:

* **HDW** (Hot-Dry-Windy Index = VPD[hPa] × wind[m/s], Srock et al. 2018) from Open-Meteo daily Tmax, RHmin and max wind. `c = HDW/250`. Weight 0.45.
* **Dryness**: days since ≥ 1 mm of rain, using 14 past days. `c = days/14`. Weight 0.30.
* **Activity**: FireLens detections within 50 km in the last 7 days. `c = n/50`. Weight 0.15. A trend compares this with the previous 7 days.
* **Fuel** (optional): Sentinel-2 NDVI, where sparse or cured vegetation scores higher. Weight 0.10.

It is deliberately simple and explainable, and it's the baseline an ML model must beat.

## Frontend

* CesiumJS without an ion token by default: Esri World Imagery, OSM, and NASA GIBS **VIIRS NOAA-20 daily true colour** (date-selectable) as base layers. **VIIRS NDVI 8-day** is an overlay. `VITE_CESIUM_ION_TOKEN` turns on 3D world terrain.
* Detections use a `PointPrimitiveCollection` (tens of thousands of points). Colour shows FRP on a log scale. The outline shows the sensor (white = VIIRS, dark = MODIS).
* Events are circles sized by footprint area, coloured by status.
* Tabs: Filters · Layers · Calendar (monthly heat map → that day's fires) · Alerts (rule from current view or point+radius).

## Roadmap

1. **Data platform**: PostgreSQL + PostGIS (geometry columns, GiST indexes, `ST_ClusterDBSCAN`), Alembic migrations, a job queue for ingestion (FIRMS rate limit: 5,000 transactions / 10 min). Backfill the archive (SP products) for the calendar.
2. **More sources**: GOES/Himawari geostationary fire products (10-minute revisit), Landsat/Sentinel-2 active fire, MCD64A1 burned area, Sentinel-2 dNBR burn severity, ERA5 / Open-Meteo history, land cover (ESA WorldCover) as fuel type.
3. **Prediction**: build a training set (detections × weather × NDVI × land cover × terrain), then a gradient-boosted ignition/spread probability per grid cell, validated against held-out seasons. Expose it as a risk raster layer on the globe.
4. **Alerts**: email/Telegram/WhatsApp channels, user accounts, per-user zones, quiet hours.
5. **Globe**: time-slider animation (Cesium clock), 3D smoke plumes, event perimeter polygons (concave hull), vector tiles for very large result sets.
