# Fire event engine: known limitations

Scope: the event engine v3 described in [CLUSTERING_ALGORITHM.md](CLUSTERING_ALGORITHM.md) and
[FIRE_EVENT_MODEL.md](FIRE_EVENT_MODEL.md). Measured results are in
[CLUSTERING_BENCHMARKS.md](CLUSTERING_BENCHMARKS.md).

## What FireLens events are, and are not

* An event is **a group of satellite thermal detections** that FireLens judges to come from one
  fire. It is not an official incident, a perimeter, or a burned-area measurement.
* **Burned area is not measured.** `burned_area_km2` is always null. `detected_footprint_km2`
  (= `area_km2`) is the union of the nominal sensor pixels that saw fire. It over-states small fires
  (one VIIRS pixel = 0.14 km²) and under-states fast fires that burn between passes.
  `detection_hull_km2` is the hull of detection centres: where detections occurred, not what burned.
* **"No detection" does not mean the fire is out.** Cloud, thick smoke, tree canopy, a missed
  pass or a fire below the detection limit all look the same. Satellites in polar orbit pass over a
  given point 2–8 times a day. The measured gap between consecutive passes over a burning event
  is p50 4 h, p95 24 h, p99 47 h.
* **Footprint growth** (`footprint_growth_km2_per_day ± se`) is the apparent growth of the
  *detected* footprint. It needs at least 3 days with 2 or more passes each. It is not a rate of
  spread, and with 3–4 points the standard error is large.

## Clustering

* **No ground truth was available.** Real-data results are structural (counts, sizes, gaps,
  consistency), not accuracy. Precision and recall figures come only from synthetic scenarios
  built by us, whose "truth" for reactivation and merging is a stated assumption.
* **Thresholds are not validated against fire perimeters.** `tolerance 1 km`, `gap 24 h` and
  `continuity 72 h with overlapping pixels` are justified by sensor geometry and the measured
  revisit statistics. Halving or doubling them moves the event count by about ±25 %. The way to
  validate them is to compare against burned-area products (MODIS MCD64A1, VIIRS VNP64A1) or
  national perimeter datasets. Adding those was out of scope (no new external dependencies).
* **MODIS swath-edge pixels** are up to ~4.8 × 2 km, so two MODIS-only detections up to ~5.6 km
  apart can link. Two separate fires that close are merged (synthetic scenario 2b). This is a
  resolution limit: two adjacent edge pixels can also be one fire. VIIRS, at 375 m, separates
  fires about 2 km apart.
* **Single linkage can chain** separate burns in very dense fire regimes (savanna,
  agricultural burning). E2 (continuity) adds almost no chaining (central Africa: widest event
  54 → 56 km), but E1 behaves as before. The widest real events (up to ~115 km over 2–3 days in
  northern Australia) may be landscape fires or chains of burns. FireLens can't tell which.
* **Restarts beyond 72 h**, or at a distance (spotting, a front that jumps more than ~1 km between
  passes), become new events. Re-ignitions on the same ground within 72 h are joined, even when in
  reality they were a new, separate ignition.
* **Isolated detections are kept** as events with `evidence = single_detection` (about half of
  all events). A real small fire and a false positive look identical. Filter with
  `min_evidence=single_pass` or `multi_pass`.

## Identity and history

* **Ids are deterministic** (hash of the earliest member detection), so incremental updates equal
  a full rebuild. The price is that an id changes when an older detection arrives later (history
  backfill) or when the first pixel is reclassified as a gas flare. Such changes are recorded as
  `renamed`, and the old id keeps resolving (`GET /api/events/{old}`, `resolved_from`).
* **History records what happened in this database's sequence of updates.** A full rebuild records
  how every previous id maps to the new events, but it cannot reconstruct the step-by-step history
  that incremental updates would have produced.
* **Time-driven transitions** (to inactive or archived) are recorded when the hourly ingest runs,
  so their timestamp can lag the threshold by up to one ingest interval.
* **The v2 → v3 migration** rebuilds all events once. The previous events are kept in
  `fire_events_legacy_v2`, and each old id maps to its v3 event through `event_lineage`.

## Intensity (FRP)

* FRP is **instantaneous power**. FireLens does not compute fire radiative energy (FRE). Integrating
  2–8 samples a day would need a diurnal model. `total_frp_mw` is kept only for API
  compatibility. It is a sum of observations and has no physical meaning.
* **MODIS and VIIRS FRP are not cross-calibrated.** They are reported separately (mean pixel FRP
  per sensor) and the intensity trend compares passes of the same sensor only. `peak_pass_frp_mw`
  says which sensor saw it.
* A whole-fire FRP per pass under-counts fires partly hidden by cloud or outside the swath on that
  pass.

## Performance

* The continuity edge widens the time reach of an incremental update from 24 h to 72 h, so an
  hourly update touches more events (see benchmarks).
* The continuity edge makes clustering look 72 h back inside long-lived, dense fires.
  Worldwide, the hourly event update grows from 4.2 s to 12.1 s (12.8 of 17.1 s profiled is
  clustering). See [CLUSTERING_BENCHMARKS.md](CLUSTERING_BENCHMARKS.md).
* A full rebuild holds every non-static detection of the database in memory (Python peak about
  1.4 KB per detection; 211 MB for 148k in central Africa). It runs only when the event rules change
  (`EVENTS_VERSION`), and only with explicit permission on a database that holds data (see
  [MIGRATION_V3.md](MIGRATION_V3.md)).
* Events spanning the antimeridian are handled (centroid, box with west > east, footprint, hull,
  incremental lookup).
* SQLite is kept as the database. Lineage and history grow with every change. There is no
  retention policy for them yet.
