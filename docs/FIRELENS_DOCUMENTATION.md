# FIRELENS: complete documentation

Status as of 15 November 2026 (branch `analysis-engine`, commit `0f885ec`).

This is the single document that describes the whole application: what it does, how to use it, how
it works inside, its API, its data, and its limits. The detailed technical notes it summarises are
linked in each section and remain the reference for the algorithms.

**Contents**

1. [What FIRELENS is](#1-what-firelens-is)
2. [Getting started](#2-getting-started)
3. [Using the app](#3-using-the-app)
4. [Fire events and perimeters](#4-fire-events-and-perimeters)
5. [Analysis engine](#5-analysis-engine)
6. [Ground and satellite context](#6-ground-and-satellite-context)
7. [Fire-danger forecast and spread outlook](#7-fire-danger-forecast-and-spread-outlook)
8. [Alerts](#8-alerts)
9. [Architecture](#9-architecture)
10. [Data and storage](#10-data-and-storage)
11. [API reference](#11-api-reference)
12. [Configuration](#12-configuration)
13. [Offline tools (`ml/`)](#13-offline-tools-ml)
14. [Testing and quality](#14-testing-and-quality)
15. [Limits and honest caveats](#15-limits-and-honest-caveats)
16. [Changelog](#16-changelog)
17. [Roadmap](#17-roadmap)
18. [Data sources and credits](#18-data-sources-and-credits)

---

## 1. What FIRELENS is

FIRELENS is a satellite fire-monitoring application. It shows where the planet is burning on a 3D
globe (or a flat 2D map), groups satellite detections into fires, and explains each fire with
statistics, ground data and other satellites.

It is built on **NASA FIRMS** active-fire data from two sensor families:

| Sensor | Satellites | Pixel | Notes |
|---|---|---|---|
| **VIIRS** | Suomi NPP, NOAA-20, NOAA-21 | 375 m | finer, sees smaller fires |
| **MODIS** | Terra, Aqua | 1 km | longer record (since 2000), orbits drifting since ~2020 |

On top of the detections it adds:

- **Fire events**: detections grouped into fires, with size, power, duration and state.
- **Fire perimeters**: the outline of where each fire was detected.
- **An analysis engine**: statistics, trends, a seasonal comparison with past years, and a careful
  MODIS vs VIIRS comparison.
- **Ground and satellite context**: land cover, terrain, Copernicus Sentinel-2 burn scar and
  vegetation, Sentinel-3 ground temperature, Sentinel-5P smoke.
- **A fire calendar**, **alerts**, a **fire-danger forecast** and an experimental **spread outlook**.

The interface comes in a **Simple** view (the essentials, more graphics) and an **Advanced** view
(every number), in **English** (main) and **Spanish**, with the FIRELENS black / orange / white
identity. Developed by ÆOn Software Solutions.

---

## 2. Getting started

### Windows app (no Python or Node needed)

Download `FIRELENS-<version>-windows-x64.zip` from the GitHub Releases, unzip it and run
`FIRELENS.exe` (see the README for the steps and what to expect). It starts the API on a local
port, serves the website from the same address and opens the browser. Data lives in
`%LOCALAPPDATA%\FIRELENS`. First start loads the last 24 hours of NASA data worldwide (or demo
data without internet); the hourly refresh keeps adding to it.

How it is built: `backend/app/desktop.py` (launcher), `backend/desktop_entry.py`,
`backend/firelens.spec` (PyInstaller, bundles rasterio's GDAL/PROJ and the built website),
`backend/desktop/build.ps1` (website + app + zip + optional Inno Setup installer),
`backend/desktop/installer.iss`.

### From source

**Requirements:** Python 3.11+ and Node 20+.

```bash
cd backend
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]"     # macOS/Linux: .venv/bin/python
.venv/Scripts/python -m uvicorn app.main:app --port 8000
```

```bash
cd frontend
npm install
npm run dev          # http://localhost:5173, proxies /api to :8000
```

Interactive API documentation: http://localhost:8000/docs.

### Data modes

| Mode | When | What you get |
|---|---|---|
| **Live, keyless** (default) | no `FIRELENS_FIRMS_MAP_KEY` | Real NASA FIRMS detections from the public files for the whole world. The last 7 days load on first start; refreshed every hour. |
| **Live + archive** | MAP_KEY in `.env` | Everything above, plus fire history: any date range for a country or the current view, from NASA's science-quality archive (SP) and NRT for recent months. |
| **Demo** | `FIRELENS_DEMO_MODE=true` | Synthetic FIRMS-format data for offline work. |

Demo and live data use separate databases (`firelens-demo.db`, `firelens.db`) and never mix.

### Credentials (`.env`, git-ignored)

Copy `.env.example` to `.env` and fill in what you have. None is required to run the app.

| Variable | Enables |
|---|---|
| `FIRELENS_FIRMS_MAP_KEY` | History loading from the NASA archive ([free key](https://firms.modaps.eosdis.nasa.gov/api/map_key/)) |
| `FIRELENS_COPERNICUS_CLIENT_ID` / `_SECRET` | Sentinel-2/3/5P measurements and NDVI (Copernicus Data Space) |
| `FIRELENS_METEOMATICS_USERNAME` / `_PASSWORD` | Weather for training the learned spread model (offline only) |
| `VITE_CESIUM_ION_TOKEN` (frontend) | 3D world terrain |

Secrets are never logged: the FIRMS key is redacted from every error message.

---

## 3. Using the app

### 3.1 Layout

- **Header:** FIRELENS logo, language (EN / ES), help (?) and the button that collapses the panel.
- **View switch:** **Simple** / **Advanced**. Remembered per browser.
- **Live badge:** "Live · FIRMS + archive" (or Demo).
- **Headline numbers:** detections in view and fires.
- **Tabs:** Filters · Layers · Analysis · Calendar · Alerts · Guide.
- **Map:** the globe, with home and 3D / 2D / Columbus buttons (top right), zoom + / − (bottom
  right), the legend (bottom left) and the ÆOn credit (bottom right).
- On phones the panel becomes a bottom sheet over the map.

### 3.2 The map

- **3D globe or 2D map.** Use the mode button at the top right. Fires reload for the new view
  when the switch finishes.
- **Base layers:** Esri World Imagery (default), OpenStreetMap, NASA GIBS VIIRS daily true colour
  (date selectable). Overlay: VIIRS NDVI 8-day.
- **Level of detail.**
  - Zoomed out: grouped bubbles, one per grid cell, with the count inside. The colour is the
    strongest fire power in the cell.
  - Each zoom step halves the cell size, from 11.25° down to about 20 km.
  - From regional zoom (or whenever the view holds a few thousand fires or fewer): individual
    detections, plus fire events as circles sized by their footprint.
- **Detections:** colour = fire power (FRP, log scale, yellow to magenta). The ring shows the
  sensor: white = VIIRS, dark = MODIS.
- **Events:** orange circle = active, brown = no recent detection.
- **Click** a bubble to zoom in; a detection, fire or empty place to open its details.
- **Hover** shows a short description. **Legend** explains the colours.

### 3.3 Filters

**Time (both views)**
- **Latest:** a rolling window of 24 h, 48 h or 7 days. It shows how old the newest satellite
  pass is (NASA publishes ~3 h after the pass), refreshes itself every 5 minutes, and can pull the
  newest data immediately.
- **History:** a day or a range (‹ › step through days, or pick it in the Calendar). If the days
  are not loaded for the area in view, FIRELENS loads them from the NASA archive automatically
  (views under 60° across, up to 31 days, needs a MAP_KEY).

**Sensor (both views)**
- MODIS and VIIRS on / off.
- **Hide low-confidence detections.** Keeps nominal and high confidence only. It is a shortcut for
  the Advanced Confidence filter and stays in step with it, with its chip and with Reset. Off by
  default, so the map matches NASA's.

**Advanced only**
- Satellites (Terra, Aqua, Suomi NPP, NOAA-20, NOAA-21).
- Detection: confidence (all / nominal and high / high), day or night, fire power min / max.
- Fire events: footprint min / max, minimum detections, active or inactive.
- Place: a point and radius (pick it on the map).

Active filters appear as removable chips under the headline numbers.

### 3.4 Layers

Base map, NDVI overlay, detections, fire events, non-vegetation heat sources (gas flares, volcanoes,
industry: hidden by default), alert zones.

### 3.5 Analysis tab

Analyses the area the map shows, for the period chosen under Filters → Time. Simple view shows the
headline results and charts. Advanced view shows every number. See [section 5](#5-analysis-engine).

### 3.6 Calendar tab

A GitHub-style heatmap of **26 weeks**: one square per day, Monday on top, the current week last.

- **Colour:** detections that day, in four orange levels (quartiles of the active days).
- **Hatched squares:** no data loaded for that day.
- **Hover:** date, detections, new fires, strongest pixel.
- **‹ ›** moves 26 weeks back or forward.
- **World / This area.**
  - *World* counts the whole planet. A day with zero detections worldwide is a day that is not
    loaded (satellites always see fire somewhere).
  - *This area* counts the area the map shows when chosen; "Use current view" refreshes it.
    In an area a zero can be a quiet day, so no-data days come from the loading records.
    A view of almost the whole globe asks you to zoom in first.
- **Select a day or a period.** Click a day, or drag across several (mouse or finger).
  - The map switches to History mode for that period.
  - A card shows the dates, number of days, detections, new fires and the strongest pixel, with a
    warning if some days have no data.
  - **Analyse this period** opens the Analysis tab for it. **×** clears the selection.
  - Below: the period's 15 largest fires (in the chosen area). Click one to open it.
- **Totals for the 26 weeks:** detections, new fires, days with data, and a link to the busiest day.
- **Advanced:** load fire history from the NASA archive for a country or the current view.

### 3.7 Alerts tab

Create a rule from the current view or a point and radius, with thresholds. See [section 8](#8-alerts).

### 3.8 Guide tab and help

- **Guide:** how FIRELENS works, what each layer means, and where the data comes from.
- **? buttons:** plain-language explanations (detection, fire power, confidence, sensors, day /
  night, fire event, state, data freshness, location accuracy, NDVI).
- **First visit:** a short guided introduction.

### 3.9 Details panel

**Fire (event)**
- *Simple:* state, size, intensity word with a power gauge, time observed, detections, trend in
  words, the perimeter line ("orange outline on the map: about N km² inside it"), the fire-power
  timeline, where it burns, terrain, Sentinel readings.
- *Advanced:* id, location, first / last detection, evidence (passes), detections per sensor and
  satellite, peak and mean fire power, trend, footprint, **perimeter area**, detection span,
  growth, confidence, change history.
- Buttons: show its detections, fire-danger forecast here, spread outlook (experimental).

**Detection:** sensor, satellite, time, fire power, confidence, brightness, pixel size, day / night.

**Place:** coordinates and the fire-danger forecast.

---

## 4. Fire events and perimeters

### 4.1 Fire events

A *fire event* is a group of detections that are contiguous in space and time: FIRELENS's best
estimate that they come from one fire.

- **Linking:** two detections join when their pixel footprints touch (plus a 1 km tolerance) within
  24 h; a longer continuity gap (72 h) keeps long fires together. It is a space-time union-find,
  run incrementally on the detections that changed.
- **Identity:** `evt_` + hash of the earliest detection. Ids are never lost: merges, splits and
  renames are recorded in `event_lineage`, and an old id resolves to the current event
  (`resolved_from`).
- **Measures:**
  - *footprint* (`area_km2`): union of nominal-resolution pixels on a 125 m grid, VIIRS when
    available (no double counting with MODIS);
  - *detection span* (convex hull of detection centres);
  - peak whole-fire power in one pass, strongest pixel, mean pixel power per sensor;
  - passes, time observed, trend, footprint growth (km²/day, with a standard error).
- **State:** active (detected in the last 24 h) or inactive; archived after 30 days without detection.
- Non-vegetation sources (gas flares, volcanoes, offshore platforms) are excluded.

Detailed notes: [model](FIRE_EVENT_MODEL.md) · [algorithm](CLUSTERING_ALGORITHM.md) ·
[limitations](CLUSTERING_LIMITATIONS.md) · [audit](FIRE_CLUSTERING_AUDIT.md) ·
[benchmarks](CLUSTERING_BENCHMARKS.md) · [migration v3](MIGRATION_V3.md) · [release](RELEASE_V3.md).

### 4.2 Fire perimeters (new)

The outline of where an event's fire was detected, drawn in orange when the fire is opened.

**How it is built** (`backend/app/services/perimeter.py`):
1. Each detection's pixel (375 m VIIRS, or 1 km MODIS if the fire has no VIIRS) is drawn on a
   125 m grid. Very large fires use coarser cells (at most 2,500 per side).
2. Gaps up to two pixels wide are closed (a morphological closing with a disc).
3. Holes inside are filled.
4. The outer edges are traced (`rasterio.features.shapes`) and simplified (Douglas-Peucker), then
   converted back to longitude / latitude. Antimeridian-safe.

**Output** (`perimeter` in `GET /api/events/{id}`): outer rings, the area inside them, the sensor
used and the gap closed. `null` with fewer than 3 pixels.

**Cost:** computed on request, nothing stored, no database change. Under 0.1 s for the largest
current fire (1,394 detections).

**Reading it:**
- It is where active fire was *detected*, at sensor resolution: not a mapped burn scar. For the
  burned area use the Sentinel-2 burn scar (section 6).
- Separate patches stay separate. Large grassland fires often come out as several fronts.
- The area inside the outline is usually larger than the footprint, because gaps and holes are
  filled.

---

## 5. Analysis engine

`GET /api/analysis/summary` and the **Analysis** tab. Read-only; reads the map pyramid, detections
and events. Details: [ANALYSIS_ENGINE.md](ANALYSIS_ENGINE.md).

| Section | Content |
|---|---|
| Totals | detections and fires started, with the change against the previous period |
| Per day | VIIRS and MODIS stacked, unusual days marked (3 robust standard deviations, MAD-based) |
| Trend | second half vs first half: rising / falling (±25 %) / stable |
| Time of day | per hour, local solar time (views up to 45° wide) or UTC |
| By sensor | detections, strongest and mean pixel power |
| Confidence, evidence | shares of high / nominal / low; multi-pass / single-pass / single-detection |
| Top fires | five largest footprints, five strongest fires |
| What is burning | land-cover classes under the detections |

**Rules that keep it honest**
- Coverage first: days not loaded are reported, and no trend or comparison is stated for an
  incomplete period.
- Detections are not fires, and fire power is never summed over time or across sensors.
- Areas up to 3° across are counted exactly; wider areas use the pyramid (approximate at edges).
- 15–380 ms per summary; cached until new data arrives.

### 5.1 Seasonal baseline ("is this normal for the time of year?")

`GET /api/analysis/baseline`. Compares the period's whole days with the same calendar days of past
years, per satellite group (VIIRS NOAA-20 headline, MODIS underneath; NOAA-21 never counted).

- Archive stored per 0.5° cell and day in `data/baseline.db`, separate from production and opened
  read-only.
- The same area every year; a year the archive does not cover is left out, never read as quiet.
- Verdict by the range of past seasons (`above`, `well_above`, `within_range`, `below`,
  `well_below`), with counting noise; no verdict with fewer than 2 seasons or too few detections.
- **Coverage:** Argentina, Nov 2023 – Nov 2026. Worldwide 2021–2025: downloading (about 87 % done
  on 15 Nov 2026), resumable with `python -m ml.download_baseline`.

### 5.2 MODIS vs VIIRS

`GET /api/analysis/sensors`. Compares the sensors only where both demonstrably imaged the same
place within a time window, matches pixels by footprint, compares the fire power of the same fire
in one pass, and uses NOAA-20 vs NOAA-21 (identical sensors) as the baseline for how much
disagreement time and cloud alone cause. Vectorised (numpy / scipy k-d tree).
Details: [SENSOR_COMPARISON.md](SENSOR_COMPARISON.md).

---

## 6. Ground and satellite context

Everything is computed on demand and cached in separate files under `data/`; the production
database is never modified. Details: [GROUND_AND_SATELLITES.md](GROUND_AND_SATELLITES.md).

| What | Source | Resolution | Endpoint |
|---|---|---|---|
| Land cover | ESA WorldCover 2021 | read at ~150 m | `/api/analysis/landcover`, `/api/events/{id}/ground` |
| Elevation, slope, aspect | Copernicus DEM GLO-90 | 90 m | `/api/events/{id}/ground` |
| Burn scar (dNBR), NDVI, NDMI | Sentinel-2 L2A | 10–20 m | `/api/events/{id}/satellites` |
| Ground temperature | Sentinel-3 SLSTR LST (daytime, warmest tenth) | 1 km | same |
| Smoke | Sentinel-5P TROPOMI (CO, aerosol index) | ~5.5 × 3.5 km | same |
| NDVI at a point | Sentinel-2 | 10 m | `/api/ndvi` |

- Land-cover and DEM tiles come from public buckets (no quota) and are cached in `data/geo`.
  `python -m ml.prefetch_geodata` downloads every tile under the database's detections ahead of time.
- Sentinel measurements use the Copernicus Statistical API (processing units). Reports are cached
  per event version in `data/enrichment.db`: refreshed daily while a fire burns, retried for up to
  45 days for a clear post-fire image. The first report takes about 20 s.

---

## 7. Fire-danger forecast and spread outlook

### 7.1 Fire-danger forecast (`/api/risk`)

A 7-day, transparent score for any point:
`score = 100 × Σ wᵢ·cᵢ / Σ wᵢ`, from

- **HDW** (Hot-Dry-Windy Index, Open-Meteo daily weather), weight 0.45;
- **dryness** (days since ≥ 1 mm of rain, 14 days back), weight 0.30;
- **activity** (FIRELENS detections within 50 km in 7 days, with a trend), weight 0.15;
- **fuel** (Sentinel-2 NDVI, optional), weight 0.10.

Deliberately simple and explainable. A trained model is planned (section 13) and must beat it.

### 7.2 Spread outlook — **experimental, not validated**

`GET /api/events/{id}/spread`, for fires detected in the last day. The map shows the cells the
fire could reach within 6, 12 and 24 h.

- **Model:** minimum travel time on a ~150 m grid, 20 km around the fire. Rate of spread from fuel
  (land cover), wind (hourly forecast, elliptical spread), slope and humidity.
- **Backtest** (300 fires, equal-area comparison): it **never beat a plain circle**, and was worse
  for larger zones. Most next-day "spread" is new ignitions and jumps, in light wind.
- **Second attempt:** a gradient-boosted model learned from 4,064 FIRELENS fire-days with
  Meteomatics weather. Against a fair baseline (a ring around the front, excluding burned ground)
  it was −1.5 to −0.1 points for zones up to 10 km² and +1.0 / +3.2 points at 20 / 40 km²:
  no meaningful gain.
- **Status:** enabled at the owner's request, labelled **Experimental** everywhere, with its
  measured hit rate shown next to every outlook and a note that it is never a safe zone.

Details: [FIRE_SPREAD.md](FIRE_SPREAD.md).

---

## 8. Alerts

- **Rule:** a box or a circle, plus thresholds (minimum confidence, minimum footprint), optionally
  a webhook URL.
- Evaluated on active events after every ingest; one alert per rule and event (deduplicated across
  merges and renames), with a severity level.
- **Delivery:** in the app (Alerts tab, alert zones on the map) and by webhook (best effort,
  failures logged).
- Not yet: email, Telegram, WhatsApp, user accounts.

---

## 9. Architecture

```
NASA FIRMS ──► ingest/firms_client ─► ingest/normalize ─► services/store ─► ingest/clustering
(MODIS/VIIRS)   (public files, area API)  (one model)      (upsert, SP>NRT)   (fire events)
                                                              │
                                    services/cells (zoom pyramid) · services/alerts
Open-Meteo, Copernicus, WorldCover, DEM ─► services/risk, sentinel, geodata, landscape, spread
                                                              │
                         api.py (FastAPI, /api/*) ─── JSON ───┘
                                                              │
frontend (Vite + TypeScript + CesiumJS): globe.ts · tiles.ts · main.ts · details.ts · analysis.ts
                                         heatmap.ts · guide.ts · prefs.ts / strings.ts (EN/ES)
```

### Backend modules (`backend/app/`)

| Module | Responsibility |
|---|---|
| `ingest/firms_client.py` | FIRMS: keyless public files; with a key, the area API, availability, key status. Rate-limit aware; redacts the key. |
| `ingest/normalize.py` | MODIS / VIIRS rows → one harmonized detection ([DATA_HARMONIZATION.md](DATA_HARMONIZATION.md)). |
| `ingest/clustering.py` | Detections → fire events; footprint, span, growth. |
| `services/store.py` | Upsert (archive supersedes NRT), incremental re-clustering, history. |
| `services/cells.py` | Zoom pyramid (`fire_cells`) and map tiles. |
| `services/queries.py` | Filters (antimeridian-safe), events, daily calendar. |
| `services/pipeline.py` | fetch → normalize → store → pyramid + events → alerts; hourly scheduler. |
| `services/history.py` | Background history loading from the archive, with progress. |
| `services/coverage.py` | Which days and areas are loaded. |
| `services/analysis.py` | Area statistics and trends. |
| `services/baseline.py` | Seasonal comparison with past years. |
| `services/sensor_compare.py` | MODIS vs VIIRS on coincident observations. |
| `services/geodata.py` | WorldCover and DEM tiles (rasterio, cached). |
| `services/landscape.py` | Land cover and terrain around a fire. |
| `services/sentinel.py` | Sentinel-2/3/5P measurements (Copernicus Statistical API). |
| `services/fire_report.py` | Per-fire ground and satellite report, cached. |
| `services/perimeter.py` | **New.** Fire perimeter from the detected footprint. |
| `services/spread.py`, `spread_ml.py` | Spread outlook (physics model; learned-model features). |
| `services/meteomatics.py` | Meteomatics client (training weather). |
| `services/risk.py`, `ndvi.py` | Fire-danger forecast; Sentinel-2 NDVI. |
| `services/alerts.py` | Alert rules, alerts, webhooks. |

### Frontend modules (`frontend/src/`)

| Module | Responsibility |
|---|---|
| `globe.ts` | Cesium viewer, base layers, zoom, view box (3D and 2D), events, alert zones, **perimeter layer**, 3D/2D switch handling. |
| `tiles.ts` | Tile manager: loads only tiles never loaded for the current filters; bubbles, detections, events. |
| `main.ts` | App wiring: filters, time modes, layers, calendar, alerts, history jobs. |
| `heatmap.ts` | **New.** The calendar heatmap (SVG) and period totals. |
| `details.ts`, `firereport.ts` | Details panel; per-fire ground and satellite report; spread outlook. |
| `analysis.ts` | Analysis tab. |
| `guide.ts`, `about.ts` | Guide tab, glossary (? buttons), legend, tooltips, first-visit guide. |
| `prefs.ts`, `strings.ts` | Language and view; every text in EN and ES (a missing translation fails the build). |
| `layout.ts`, `render.ts`, `util.ts` | Panel and mobile sheet; drawing helpers; formatting. |

---

## 10. Data and storage

| File | Content | Written by |
|---|---|---|
| `firelens.db` | Production database (SQLite): detections, pyramid, events, coverage, alerts | the API |
| `firelens-demo.db` | Demo data | the API in demo mode |
| `data/baseline.db` | Seasonal archive per 0.5° cell and day | `ml.build_baseline`, `ml.download_baseline` |
| `data/enrichment.db` | Cached per-fire ground and satellite reports | the API |
| `data/geo/` | WorldCover and DEM tiles | the API, `ml.prefetch_geodata` |
| `data/training/` | Training archive and cached weather (Argentina, spread cases) | `ml/` scripts |

**Database layers** ([DATABASE.md](DATABASE.md)):
1. `detections`: one row per satellite fire pixel, both sensors in one harmonized table, every
   native value kept; per-sensor views `modis_detections`, `viirs_detections`.
2. `fire_cells`: the zoom pyramid, for map tiles and the worldwide calendar.
3. `fire_events` (+ `event_passes`, `event_lineage`, `event_history`).
4. Bookkeeping: `coverage`, `ingest_runs`, `app_meta`, `alert_rules`, `alerts`.

Each layer can be rebuilt from the one above. Schema changes need
`FIRELENS_ALLOW_SCHEMA_UPGRADE=true`. Map tiles carry an ETag and are cached per data version.

---

## 11. API reference

All under `/api`. Interactive docs at `/docs`.

| Endpoint | Purpose |
|---|---|
| `GET /health`, `GET /sources` | Status; FIRMS products |
| `POST /ingest` | Run an ingestion now |
| `GET /tiles/meta` | Data version and current hour (when tiles go stale) |
| `GET /tiles/{cells\|points}/{z}/{x}/{y}` | Map tiles: grouped cells or individual detections + events. Filters + `hours`. ETag / 304 |
| `GET /detections` | Harmonized detections. Filters: `start`, `end`, `bbox`, `lat`+`lon`+`radius_km`, `instrument`, `satellite`, `min_confidence`, `min_frp`, `max_frp`, `daynight`, `event_id` |
| `GET /detections/compact`, `/detections/grid` | Columnar arrays for rendering; gridded counts |
| `GET /detections/{id}` | One detection |
| `GET /events`, `GET /events/count` | Fire events (filters, `sort=recent\|area\|frp`); count in view |
| `GET /events/{id}` | Event with its detections and **`perimeter`**. Old ids resolve (`resolved_from`) |
| `GET /events/{id}/timeline` | Whole-fire power per overpass |
| `GET /events/{id}/history`, `/lineage` | Recorded changes; where ids went |
| `GET /events/{id}/ground` | Land cover and terrain around the fire |
| `GET /events/{id}/satellites` | Sentinel-2 burn scar and vegetation, Sentinel-3 ground heat, Sentinel-5P smoke |
| `GET /events/{id}/spread` | Spread outlook (experimental) |
| `GET /analysis/summary` | Statistics and trends for an area and period |
| `GET /analysis/baseline` | Comparison with past seasons |
| `GET /analysis/landcover` | What is burning |
| `GET /analysis/sensors` | MODIS vs VIIRS |
| `GET /calendar` | Daily detections, new events, strongest pixel (`start`, `end`, optional `bbox`; at most 400 days) |
| `GET /export/{modis\|viirs}.csv` | One sensor's detections with native field names |
| `GET/POST/DELETE /alerts/rules`, `GET /alerts` | Alert rules and triggered alerts |
| `GET /risk?lat&lon` | 7-day fire-danger forecast |
| `GET /ndvi?lat&lon` | Sentinel-2 NDVI |
| `GET /history/availability` | Date span per FIRMS product; key usage |
| `GET /history/coverage` | Loaded days for an area (status per day) |
| `GET /history/countries` | Countries loadable by name |
| `POST /history/backfill` | Load history (`start`, `end`, `country` or `bbox`) |
| `GET /history/jobs[/{id}]`, `DELETE /history/jobs/{id}` | Progress; cancel |

---

## 12. Configuration

Environment variables (prefix `FIRELENS_`), set in `.env`:

| Variable | Default | Meaning |
|---|---|---|
| `FIRMS_MAP_KEY` | — | NASA FIRMS key (history) |
| `DEMO_MODE` | `false` | Synthetic data |
| `DATABASE_URL` | `firelens.db` | Database location |
| `PUBLIC_REGION` | `Global` | Region of the keyless public files |
| `INGEST_INTERVAL_MINUTES` | `60` | Refresh interval; 0 disables the scheduler |
| `INGEST_SOURCES` | MODIS + VIIRS NRT | Products refreshed |
| `CLUSTER_TOLERANCE_KM` | `1.0` | Extra link distance between pixels |
| `CLUSTER_MAX_GAP_HOURS` | `24` | Time gap that still links detections |
| `EVENT_ACTIVE_HOURS` | `24` | A fire is active if detected within this |
| `EVENT_ARCHIVE_DAYS` | `30` | Archived after this long without detection |
| `BASELINE_DB_PATH` | `data/baseline.db` | Seasonal archive |
| `COPERNICUS_CLIENT_ID` / `_SECRET` | — | Copernicus Data Space |
| `METEOMATICS_USERNAME` / `_PASSWORD` | — | Meteomatics (training only) |
| `ALLOW_SCHEMA_UPGRADE` | `false` | Permit schema migrations at start |
| `ADMIN_TOKEN` | — | Production: key (header `X-Admin-Token`) for ingest, history loading and alert rules. Unset = open (development) |
| `DATA_DIR` | `data/` | Folder for the seasonal archive, report cache and map-context tiles (`/data` in the container) |
| `CORS_ORIGINS` | localhost | Website addresses allowed to call the API (JSON list) |

Run only one API process: two would each run the ingest scheduler.

**Production:** Oracle Cloud Always Free VM (Docker + Caddy HTTPS) for the API and Vercel for the
website, at no cost. Step by step: [DEPLOYMENT.md](DEPLOYMENT.md).

---

## 13. Offline tools (`ml/`)

Run from `backend/` with `.venv/Scripts/python -m ml.<name>`.

| Script | Purpose |
|---|---|
| `download_firms` | Training archive of detections (Argentina) |
| `download_weather` | Daily weather for the fire-danger model (Open-Meteo; paced by its daily quota) |
| `dataset`, `train` | Build the fire-danger training set and train the model (pending the weather download) |
| `build_baseline` | Seasonal archive from the training archive (Argentina) |
| `download_baseline` | Worldwide seasonal archive 2021–2025 (resumable, ~5 h, ~500 MB) |
| `prefetch_geodata` | Download WorldCover and DEM tiles ahead of time |
| `backtest_spread` | Backtest of the spread model against a circle |
| `spread_cases`, `fetch_spread_weather`, `train_spread` | Learned spread model (Meteomatics weather) |
| `check_meteomatics` | Check the Meteomatics account |

---

## 14. Testing and quality

```bash
cd backend && .venv/Scripts/python -m pytest       # 221 tests
cd frontend && npm run typecheck && npm run build
```

- Backend tests (`backend/tests/`) cover harmonization and data quality, clustering and the event
  engine (incremental = full rebuild), map tiles, the API (including the calendar, worldwide vs
  bounding box day by day), analysis, seasonal baseline, sensor comparison and matching, ground
  context and spread, the learned spread features, the fire-danger forecast and training set,
  perimeters, and checks on real data.
- The frontend typecheck fails on any missing Spanish translation.
- New UI is checked in the browser on desktop and phone sizes, in both languages, in 3D and 2D.

---

## 15. Limits and honest caveats

- **Detections are not fires, and footprints are not burned area.** Pixels are 375 m or 1 km;
  the perimeter is where fire was detected. Use the Sentinel-2 burn scar for burned area.
- **Satellites pass a few times a day.** A short fire between passes is missed; "inactive" can
  mean cloud or no pass.
- **Archive gaps.** The worldwide database is not loaded for parts of September–October 2026;
  those days show as "no data" in the calendar and History mode.
- **Seasonal baseline:** Argentina complete; worldwide download in progress.
- **Spread outlook:** experimental and not better than a ring around the fire. Never a safe zone.
- **Fire-danger forecast:** a heuristic, not a trained model yet.
- **Land cover is a 2021 map**; recent clearing shows its old class.
- **Single machine:** SQLite, one API process, no user accounts. Fine for one user; not yet a
  multi-user deployment.
- **Alerts:** in the app and by webhook only.
- **Admin actions in production** (loading history, refresh now, alert rules) need the admin key;
  the website asks for it once and keeps it in that browser.

---

## 16. Changelog

Most recent first.

**`0f885ec` — Calendar, perimeters, low-confidence switch, 2D fixes**
- Calendar redesigned as a 26-week GitHub-style heatmap, with World / This area scope, no-data days
  from the loading records, click or drag to select a period, a period card with "Analyse this
  period" and the period's largest fires.
- Fix: the worldwide calendar counted one hour per day (true division); now integer division.
- Area calendar ~30 ms (skips the time index for small areas).
- Fire perimeters (`services/perimeter.py`), returned by `/api/events/{id}` and drawn when a fire
  is opened.
- "Hide low-confidence detections" switch in both views.
- 2D map: the 3D/2D switch no longer stalls half-way, and fires reload when it finishes.
- 2D map: fires load across the whole visible map (Cesium swapped west and east past half the world).

**`47b14cf` — Branding**
- Official FIRELENS logo in the header; ÆOn Software Solutions credit at the map's bottom right.

**`954b5a0` — Interface redesign**
- Simple and Advanced views; English and Spanish; Guide tab; bigger type; black / orange / white
  institutional style; more graphics in Simple view. The map itself unchanged.

**`0e0c321` — Learned spread model test**
- Gradient-boosted model with Meteomatics weather: no gain over a ring around the burned area.

**`00bc218` — Analysis engine**
- Faster analyses (vectorised sensor comparison, index steering); seasonal baseline; land cover,
  terrain and Sentinel-2/3/5P context; spread outlook (experimental).

**`6d7c5dc` — Map usability**
- Collapsible side panel; individual fires on zoom; all confidence levels by default.

Earlier: fire events v3 (lineage, history, incremental clustering), zoom pyramid and tiles,
history loading from the archive, alerts, fire-danger forecast, NDVI.

---

## 17. Roadmap

**Next (most viable)**
1. **Timelapse:** play a period day by day, with the growing perimeter.
2. **GOES geostationary fire detections:** a new view every 10 minutes over the Americas.
3. **Trained fire-danger model** once the weather download completes; must beat the heuristic.
4. Finish loading the worldwide archive (September–October 2026) and the seasonal baseline.

**Later**
- Perimeters for every fire on the map (not only the one opened).
- Landsat active fire (USA / Canada), Sentinel-5P aerosol overlay.
- Alerts by email / Telegram / WhatsApp, user accounts, per-user zones.
- PostgreSQL + PostGIS, migrations tool, job queue: when deploying for many users.

---

## 18. Data sources and credits

NASA FIRMS (MODIS Collection 6.1, VIIRS 375 m) · NASA EOSDIS GIBS imagery · Copernicus Sentinel-2,
Sentinel-3, Sentinel-5P (Copernicus Data Space Ecosystem) · ESA WorldCover 2021 · Copernicus DEM
GLO-90 · Open-Meteo · Meteomatics · Esri World Imagery · OpenStreetMap contributors · CesiumJS.

Developed by ÆOn Software Solutions.
