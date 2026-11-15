# Fire event engine: benchmark report (v2 "old" vs v3 "new")

Run on 2026-11-14 on one development laptop (Windows 11, Python 3.14, SQLite, single process).
Reproduce it with `python -m benchmarks.clustering_benchmark <FIRMS CSV dir> <scratch dir> --world --migrate <v2 db>`.
The raw output is `clustering_benchmark.json` in the scratch dir.

* **old** = v2 rule: E1 only (≤ 1 km between footprints, ≤ 24 h).
* **new** = v3 rule: E1, plus E2 (pixels overlap, ≤ 72 h).

Both rules run through the same v3 code (store, metrics, lineage), so differences come from the rule.

Real data: NASA FIRMS NRT, MODIS + VIIRS NOAA-20 + NOAA-21, worldwide, 7 days (918,565
detections). Central Africa = 15–30° E, 0–12° N (148k detections), the densest fire region in the set.

> **Section 1 is the only one with ground truth, and that truth is synthetic.** Sections 2–5 are
> structural measurements of real data. No labelled fire perimeters were available.

## 1. Synthetic scenarios (ground truth by construction)

| Scenario | true fires | old: events · P · R · FM · FS | new: events · P · R · FM · FS |
|---|---|---|---|
| 1 one fire, five satellites | 1 | 1 · 1.00 · 1.00 · 0 · 0 | 1 · 1.00 · 1.00 · 0 · 0 |
| 2 two fires 3 km apart (VIIRS) | 2 | 2 · 1.00 · 1.00 · 0 · 0 | 2 · 1.00 · 1.00 · 0 · 0 |
| 2b two fires 4 km apart, MODIS swath edge | 2 | 1 · 0.33 · 1.00 · **1** · 0 | 1 · 0.33 · 1.00 · **1** · 0 |
| 2c new burn 1.5 km from an old scar, 48 h later | 2 | 2 · 1.00 · 1.00 · 0 · 0 | 2 · 1.00 · 1.00 · 0 · 0 |
| 3 expanding fire (2.4 km/day, 4 days) | 1 | 1 · 1.00 · 1.00 · 0 · 0 | 1 · 1.00 · 1.00 · 0 · 0 |
| 4 intermittent (unseen 30 h, then 40 h)* | 1 | 3 · 1.00 · **0.32** · 0 · **1** | 1 · 1.00 · 1.00 · 0 · 0 |
| 5 reactivation after 60 h quiet* | 1 | 2 · 1.00 · **0.53** · 0 · **1** | 1 · 1.00 · 1.00 · 0 · 0 |
| 6 15 isolated detections | 15 | 15 · 1.00 · 1.00 · 0 · 0 | 15 · 1.00 · 1.00 · 0 · 0 |
| 8 two fronts burning into each other* | 1 | 1 · 1.00 · 1.00 · 0 · 0 | 1 · 1.00 · 1.00 · 0 · 0 |
| 9 two fires bridged by a gas-flare pixel | 2 | 2 · 1.00 · 1.00 · 0 · 0 | 2 · 1.00 · 1.00 · 0 · 0 |
| 10 expanding fire at 65° N | 1 | 1 · 1.00 · 1.00 · 0 · 0 | 1 · 1.00 · 1.00 · 0 · 0 |
| 11 expanding fire across 180° | 1 | 1 · 1.00 · 1.00 · 0 · 0 | 1 · 1.00 · 1.00 · 0 · 0 |
| **Total** | | **FM 1 · FS 2** | **FM 1 · FS 0** |

P / R = pairwise precision / recall. FM = false merges (events holding more than one true fire). FS =
false splits (true fires spread over several events). \* The truth is an assumption stated in the
scenario: the same pixels within 72 h are the same fire.

* The new rule removes both false splits and adds no false merge. That includes 2c, a new burn next
  to an old scar, which is the trap a longer time gap would fall into.
* 2b (two MODIS swath-edge pixels 4 km apart) is merged by both rules. It is a sensor-resolution
  limit, documented in [CLUSTERING_LIMITATIONS.md](CLUSTERING_LIMITATIONS.md).
* Scenario 7 (duplicate NRT then SP observations) is an ingestion property. It is covered by tests:
  re-ingesting changes nothing, and an SP re-delivery writes no new version.

## 2. Structure on real data (no labels)

| | Africa old | Africa new | World old | World new |
|---|---|---|---|---|
| Events | 23,332 | 21,002 (−10.0 %) | 188,721 | 172,438 (−8.6 %) |
| `single_detection` | 10,176 | 9,217 | 99,797 | 90,295 |
| `single_pass` | 4,354 | 3,711 | 31,084 | 26,810 |
| `multi_pass` | 8,802 | 8,074 | 57,840 | 55,333 |
| Widest event | 54.4 km | 56.4 km | 115.1 km | 115.1 km |
| Events > 20 km wide | 34 | 39 | 137 | 152 |
| Events starting < 3 km from one quiet 24–96 h | 29.2 % | 23.4 % | 23.2 % | 16.5 % |
| Lifecycle (new) | – | 288 reactivated | – | 2,131 reactivated |

The new rule joins about 9 % of events into longer-lived fires. Chaining barely changes: the widest
event stays the same, and events wider than 20 km go from 137 to 152 worldwide (+0.01 % of events).

## 3. Incremental processing vs full reconstruction

Initial load = everything except the newest 2 h. The hourly refresh is the real 24 h file, of
which the newest 2 h are new. Then a full rebuild of everything is compared with the incremental
result.

| | Africa old | Africa new | World old | World new |
|---|---|---|---|---|
| Same partition (event membership) | ✅ | ✅ | ✅ | ✅ |
| Same event ids | ✅ | ✅ | ✅ | ✅ |
| Events modified by the hourly refresh | 3,160 | 5,601 | 774 | 1,069 |
| Hourly refresh: store + map pyramid | 0.4 s | 0.4 s | 1.6 s | 2.5 s |
| Hourly refresh: event rebuild | 4.2 s | 9.4 s | 4.2 s | 12.1 s |
| Full rebuild | 10.5 s | 15.2 s | 114.5 s | 154.6 s |
| Initial load (event build) | 23.7 s | 32.7 s | 207.9 s | 429.6 s |
| Peak Python memory, hourly | 56 MB | 93 MB | 33 MB | 40 MB |
| Peak Python memory, full rebuild | 222 MB | 211 MB | 1,557 MB | 1,480 MB |

Tests add a third check: the same detections ingested as shuffled batches (archives and late passes
arriving in any order, an antimeridian fire included) give the same events and ids as a full rebuild
(`test_incremental_equals_full_rebuild_in_any_batch_order`).

**Performance regression, justified and bounded.**
* Worldwide, the hourly event update goes from 4.2 s to 12.1 s (+8 s per hour, against a 60 min
  ingest interval).
* Profile of the worldwide hourly step: 12.8 of 17.1 s is spent clustering the affected events.
  E2 makes each detection look 72 h back instead of 24 h, inside long-lived, dense fires.
* Tried and rejected: skipping already-connected pairs was slower (16.2 s).
* Remaining option, not done: process detections in time order and prune per-cell history.

**The same work also made the old rule faster.** The hourly update was 10.5 s before
(audit baseline, same data). Two exact optimizations made it 4.2 s:
* exact reach pads (5,601 instead of 8,937 events touched in Africa);
* the chord distance test.

Memory is measured with `tracemalloc` (Python allocations) in separate runs, so it never distorts a
timing (worldwide: `--world-memory-only`). Full rebuilds need about 1.5–1.7 KB of Python memory per
detection: 1.5 GB for one worldwide week. Hourly updates stay under 100 MB.

## 4. Chord distance vs haversine: exact membership equivalence

Production tests "distance ≤ reach" as "chord between unit vectors ≤ 2·sin(reach / 2R)". This is
the same great-circle distance and is monotonic, but cheaper. It is compared against the original
haversine predicate:

| Dataset | old rule | new rule |
|---|---|---|
| 11 synthetic scenarios (+ all-pairs brute force) | identical | identical |
| Central Africa, 148k detections | identical (23,332 events, 0 differ) · chord 5.8 s vs haversine 10.1 s | identical (21,002, 0 differ) · 10.5 s vs 20.0 s |
| World, 918k detections | identical (188,721, 0 differ) · 56.3 s vs 104.2 s | identical (172,438, 0 differ) · 96.0 s vs 176.2 s |

Edge cases (`tests/test_distance_equivalence.py`, 74 tests):
* pairs 1 m inside and 1 m outside the link distance, at the equator, 34° S, 65° N, across 180°
  and within 0.05° of both poles, at four bearings, for both edges;
* random clouds near each of those places, against an all-pairs brute force, under three
  parameter sets.

**Bugs these checks found and fixed (present in v2 too):**
* Detections on opposite sides of the antimeridian were never compared (the grid didn't wrap).
* Above about 89.9° latitude the longitude search window was too narrow.
* The window was 0.1 % too narrow everywhere, from mixing the ellipsoid km/° with the sphere radius.

Events spanning 180° now have a wrap-aware centroid, bounding box (west > east), footprint and hull.
293 of the worldwide detections lie within 1° of the antimeridian. Your current database has none.

## 5. Migration v2 → v3

| | Copy of your database (`firelens.db`) | Worldwide-scale copy (benchmark DB) |
|---|---|---|
| Detections | 189,430 | 918,565 |
| Events v2 → v3 | 43,481 → 40,267 | 188,722 → 172,438 |
| v2 ids still live / merged (lineage) | 40,267 / 3,214 | 172,438 / 16,284 |
| Duration (real startup path) | 20.0 s | 185.5 s |
| All checks | passed (18/18) | v2 ids resolve: 2,000 / 2,000 sampled |

Details, checks, size and rollback: [MIGRATION_V3.md](MIGRATION_V3.md).

## 6. Production (after the migration)

| Check | Result |
|---|---|
| Migration of `firelens.db` | 33.8 s. Verified against the backup: 18/18 |
| Post-migration ingestion | 155,787 new detections (worldwide 24 h file), all in valid v3 events. The scheduled run at 00:37 UTC had no errors |
| Health invariants on production after ingestion (345,217 detections) | 19/19 (`migration_rehearsal health`) |
| Alerts, event growth, history, timeline, tiles, filters, calendar, export | 17/17 on an isolated copy of production (`post_migration_check`) |
| Rollback cycle (restore → `728f83b` → roll forward) | restore byte-identical; `728f83b` serves and ingests; roll-forward 24/24 |

Release summary: [RELEASE_V3.md](RELEASE_V3.md).
