# FireLens database

The database has four layers. Each layer is derived from the one above it and can be rebuilt from it.

| Layer | Tables / views | Written by | Read by |
|---|---|---|---|
| **Observations** (system of record) | `detections` | ingest (`store.upsert_detections`) | everything below, detail panels, export |
| **Per-sensor views** (analysis) | `modis_detections`, `viirs_detections` | views over `detections` | analysts, `GET /api/export/{modis,viirs}.csv` |
| **Map pyramid** (rendering, calendar) | `fire_cells` | ingest, for the hours that changed (`cells.refresh_cells`) | map tiles, worldwide calendar |
| **Fire events** (interpretation) | `fire_events` + `detections.event_id`, `event_passes`, `event_lineage`, `event_history` | ingest, for the events that changed (`store.rebuild_events`) | events layer, alerts, calendar, event detail |
| Bookkeeping | `coverage`, `ingest_runs`, `app_meta`, `alert_rules`, `alerts` | ingest, history jobs, API | History mode, health, alerts |

## `detections`: one row per satellite fire pixel

MODIS and VIIRS share one harmonized table, because most questions span both sensors ("what
burned here?"). The sensor is a column (`instrument`), with an `(instrument, acq_hour)` index, so
each sensor is still a cheap, separate access path. Every native value is kept.

| Column | MODIS meaning | VIIRS meaning |
|---|---|---|
| `brightness_k` | `brightness` (band 21/22, K) | `bright_ti4` (I4, K; ≥ 367 K is saturated → `brightness_saturated`) |
| `brightness_secondary_k` | `bright_t31` | `bright_ti5` |
| `confidence_raw` | 0–100 % | `l` / `n` / `h` |
| `confidence_pct`, `confidence_level` | native % and its FIRMS class | harmonized class value (20 / 55 / 90) |
| `scan_km`, `track_km` | actual pixel footprint (grows toward the swath edge) | same |
| `detection_type`, `static_kind` | archive `type`: 0 vegetation, 1 volcano, 2 static land source, 3 offshore | same |

The `id` is a hash of sensor, satellite, rounded position and minute. The archive (SP) version of a
pixel therefore replaces its near-real-time (NRT) twin in place. `acq_hour` (whole hours since
1970 UTC) is the time key that the pyramid and the views share.

## `modis_detections` / `viirs_detections`: each sensor in its own vocabulary

These are views with the field names each sensor's documentation uses. For example:
`brightness_t21_k`/`bright_t31_k` for MODIS, and `bright_ti4_k`/`bright_ti5_k`/`confidence_class`
(l/n/h) for VIIRS. They are recreated on every start, and they are what the CSV export streams:

```bash
curl "localhost:8000/api/export/viirs.csv?start=2026-11-01T00:00:00Z&end=2026-11-14T00:00:00Z&bbox=-75,-56,-53,-21" -o viirs.csv
```

On PostgreSQL, `detections` can become `PARTITION BY LIST (instrument)` (one physical table per
sensor) with no change to the views or the application.

## `fire_cells`: the zoom pyramid

The globe never asks for "all fires in view". It asks for fixed tiles, and each tile is read from
detections that were already summed when they were stored. NASA FIRMS's own map works the same way.

* Key: `level, tx, ty, hour, cx, cy, instrument, confidence_level, is_static` (stored without a
  rowid). Values: `n`, `frp_sum`, `frp_max`, `lat_sum`, `lon_sum` (centroid = sum / n).
* Level k cells are `180 / 2^k` degrees, aligned to −180/−90. A cell's parent is `(cx >> 1, cy >> 1)`.
  Stored levels run from 4 (11.25°) to 9 (0.35°).
* A map tile at zoom z is the 16 × 16 cells of level z + 4 with `(tx, ty) = (cx >> 4, cy >> 4)`.
  Reading a tile is one index range scan, whatever the zoom.
* Sums, not averages, are stored. So any hour range, any set of confidence classes and either
  sensor add up exactly, and MODIS and VIIRS remain separable at every zoom.
* Maintenance: an ingest re-sums only the hours that received new or changed detections. That is
  one GROUP BY at level 9, rolled up to the coarser levels in memory.

Measured on a worldwide 7-day load (918k detections): 496k cell rows. A world view (2 tiles) takes
0.02 s from the pyramid against 4.4 s computed from the detections. The hourly refresh re-sums its
hours in 0.1 s.

| Zoom (tile size) | Shows | Source |
|---|---|---|
| 0–5 (180° … 5.6°) | grouped markers, 11.25° … 0.35° cells | `fire_cells` |
| 6 (2.8°) | grouped markers, 0.18° cells | summed from `detections` for that tile |
| 7 (1.4°, ~150 km), from ~300 km camera altitude | individual detections + fire events | `detections`, `fire_events` |

Filters the pyramid doesn't hold (satellite, day/night, FRP range, point + radius) are summed from
`detections` for that tile instead. The answer is the same, only slower.

## `fire_events`: what the detections mean

Events are connected components of detections. Two detections are linked when one of these holds:
* they are close in space (footprints + 1 km) and in time (24 h);
* their pixels overlap and they are up to 72 h apart.

Events are derived data. Each has a deterministic id, a lifecycle state (active / reactivated /
inactive / archived), an evidence level, per-sensor FRP and three distinct area measures. Changes
are never silent:
* `event_lineage` maps every id that stopped being canonical (merged / split / renamed / removed)
  to its successor;
* `event_history` keeps one versioned row per change;
* `event_passes` holds the whole-fire FRP per satellite overpass.

The model is in [FIRE_EVENT_MODEL.md](FIRE_EVENT_MODEL.md) and the algorithm in
[CLUSTERING_ALGORITHM.md](CLUSTERING_ALGORITHM.md). `EVENTS_VERSION` / `CELLS_VERSION` in
`migrations.py` rebuild either layer on start when its logic changes. The v2 → v3 rebuild keeps the
previous events in `fire_events_legacy_v2`.

## Caching and the "only load what changed" rule

* `app_meta.data_version` is incremented whenever an ingest stores new or changed detections.
* `GET /api/tiles/meta` returns it, plus the current UTC hour, because rolling "latest" windows
  snap to whole hours.
* The server keeps built tiles in memory, keyed by version + tile + query. Each tile response
  carries an ETag. The browser revalidates a tile it has seen and gets a 304 if nothing changed.
* The globe keeps the tiles it has drawn (up to 400). Moving or zooming only requests tiles never
  loaded for the current filters. Toggling MODIS/VIIRS or a layer redraws from memory. On a new
  `data_version`, only visible tiles are revalidated, and only tiles whose ETag changed are rebuilt.

## Growth and next steps

About 60 MB per worldwide day (detections + indexes + pyramid). Planned next:

* Retention: keep NRT detections for N days, keep SP and events forever.
* PostgreSQL + PostGIS (GiST on a geometry column), with `detections` partitioned by instrument
  and month.
