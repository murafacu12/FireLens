# MODIS ↔ VIIRS data harmonization

FireLens uses NASA FIRMS active-fire products as its base data (the FIRMS web map is
our functional reference). FIRMS publishes MODIS and VIIRS as **separate CSVs with
different columns, units and semantics**. This document is the contract for how both
become one canonical `FireDetection` (`backend/app/schemas.py`), implemented in
`backend/app/ingest/normalize.py` and covered by `backend/tests/test_normalize.py`.

## Sensors at a glance

| | MODIS (Terra, Aqua) | VIIRS (Suomi NPP, NOAA-20, NOAA-21) |
|---|---|---|
| Nominal pixel at nadir | 1 km | 375 m |
| Pixel at scan edge | up to ~4.8 × 2 km | up to ~0.8 × 0.78 km |
| Fire channel | band 21/22 (~4 µm) | I4 (3.74 µm), saturates ≈ 367 K |
| Background channel | band 31 (11 µm) | I5 (11.45 µm) |
| Confidence | 0–100 % | class: `l` / `n` / `h` |
| Overpasses (local solar) | ~10:30 / 22:30 (Terra), ~13:30 / 01:30 (Aqua) | ~13:30 / 01:30 |
| FIRMS products used | `MODIS_NRT`, `MODIS_SP` | `VIIRS_SNPP_NRT/SP`, `VIIRS_NOAA20_NRT/SP`, `VIIRS_NOAA21_NRT` |

## Field mapping

| Canonical field | MODIS source | VIIRS source | Rule |
|---|---|---|---|
| `instrument` | `brightness` column present | `bright_ti4` column present | Detected from the columns, and checked against the requested source. |
| `satellite` | `T`→Terra, `A`→Aqua | `N`→Suomi NPP, `1`/`N20`→NOAA-20, `2`/`N21`→NOAA-21 | Falls back to the source's platform. |
| `acquired_at` | `acq_date` + `acq_time` | same | `acq_time` is HHMM UTC with leading zeros dropped (`145` = 01:45), padded and stored as UTC. |
| `brightness_k` | `brightness` | `bright_ti4` | Kept for display only. **Different bands, so not comparable across sensors.** |
| `brightness_secondary_k` | `bright_t31` | `bright_ti5` | Same caveat. |
| `brightness_saturated` | always `false` | `bright_ti4 ≥ 367 K` | Flags I4 saturation, where the temperature is not reliable. |
| `frp_mw` | `frp` | `frp` | Fire Radiative Power in MW. This is the main **cross-sensor** intensity metric. Negative values become null. |
| `scan_km`, `track_km` | `scan`, `track` | same | Default to the nominal pixel size if missing. |
| `pixel_area_km2` | `scan × track` | same | The real footprint grows with view angle. |
| `nominal_resolution_m` | 1000 | 375 | |
| `confidence_pct` | `confidence` | `l`=20, `n`=55, `h`=90 | VIIRS classes map to a value inside the equivalent MODIS band (below). |
| `confidence_level` | <30 low, 30–79 nominal, ≥80 high | `l`/`n`/`h` | One filterable scale for both sensors. |
| `processing_level` | `NRT` if `version` contains "NRT", else `SP` | same | |
| `daynight` | `daynight` | same | `D`/`N`. |
| `detection_type` | `type` (archive only) | same | 0 vegetation, 1 volcano, 2 static land source, 3 offshore. |

## Identity and de-duplication

`id = sha1(instrument | satellite | lat(4dp) | lon(4dp) | YYYYMMDDHHMM)`.

* Re-ingesting the same file is idempotent (upsert by id).
* An **SP** (science-quality reprocessing) record replaces its **NRT** twin. An NRT record never overwrites SP.
* MODIS and VIIRS detections of the same fire are **not** merged at the pixel level. Their footprints and times differ, so each is real evidence. They are merged into one **fire event** (next section).

## From pixels to fire events

`backend/app/ingest/clustering.py` groups detections from **any sensor** into events:

* Two detections are linked when their pixel footprints are within `cluster_tolerance_km` (default 1 km) of touching, using the haversine distance between centres minus both footprint half-diagonals. They must also be at most `cluster_max_gap_hours` apart (default 24 h).
* Links are transitive (union-find), so a fire that spreads over days stays one event.
* A space-time grid keeps neighbour search near-linear.

Event metrics:

| Metric | Definition |
|---|---|
| `area_km2` | Union of pixel footprints on a 250 m grid over the event's life. It uses only **VIIRS** pixels when any exist, so coarse MODIS pixels don't inflate it and overlapping sensors aren't double counted. This is the *active-fire footprint*, **not burned area**. |
| `max_frp_mw`, `total_frp_mw` | Max and sum of FRP across all sensors. |
| centroid | FRP-weighted mean position, which sits on the most intense part of the fire. |
| `status` | `active` if last seen within `event_active_hours` (24 h). |
| `modis_count`, `viirs_count` | How much each sensor contributed. |

## Known limitations / next steps

* **Overpass bias:** MODIS sees mid-morning and evening, VIIRS early afternoon. Daily counts mix diurnal phases. A future `diurnal_phase` field could normalise this.
* **FRP across sensors:** VIIRS FRP per pixel is lower because pixels are smaller. Compare *event totals*, not per-pixel values. Inter-sensor FRP calibration (published regressions) is a candidate improvement.
* **Antimeridian:** bbox filters handle it. Clustering and area treat ±180° as an edge (rare for fires).
* **Burned area:** should come from MODIS MCD64A1 or Sentinel-2 dNBR, not from active-fire footprints.
