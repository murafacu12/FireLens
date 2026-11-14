# Fire event clustering: audit of the current engine

Status: audit of the code as of 2026-11-14 (uncommitted working tree). No behaviour was changed to
write it. Measurements come from a **real worldwide 7-day NASA FIRMS load** (MODIS + VIIRS
NOAA-20/21 NRT, 918,565 detections, 188,722 events), stored in a scratch copy of the database.
**No ground-truth labels exist for this data.** Every number below describes structure (counts,
sizes, gaps); none of them is an accuracy figure.

Code audited: `backend/app/ingest/clustering.py`, `backend/app/services/store.py`,
`backend/app/services/alerts.py`, `backend/app/migrations.py`, plus their consumers (API, globe, calendar).

## 1. How detections are grouped

Fire events are **connected components of a space-time graph**: single-linkage, equivalent to
ST-DBSCAN with `minPts = 1`.

Two detections *i*, *j* are linked when both hold:

* `haversine(i, j) ≤ rᵢ + rⱼ + tolerance_km`. Here `r = ½·√(scan² + track²)` is the half-diagonal
  of the **actual** pixel footprint, and `tolerance_km = 1.0` (`FIRELENS_CLUSTER_TOLERANCE_KM`).
  The distance is geodesic (haversine on a 6,371 km sphere). There is no Euclidean lat/lon distance.
* `|tᵢ − tⱼ| ≤ max_gap_hours = 24` (`FIRELENS_CLUSTER_MAX_GAP_HOURS`).

Membership is transitive (union-find), so a fire that moves or grows stays one event. Neighbour
search uses a lat × lon × time grid. Longitude cells are widened toward the poles, so the cost is
close to linear (central Africa, 148k detections: 14 s).

Sensors are treated alike. MODIS and VIIRS link to each other with the same rule. Static sources
(volcanoes, gas flares, offshore platforms; `static_kind`) are excluded before clustering.

Measured link distances (the actual pixel footprint grows toward the swath edge):

| Sensor | footprint radius p50 | p99 | two-pixel link distance at p99 |
|---|---|---|---|
| VIIRS 375 m | 0.32 km | 0.54 km | 2.1 km |
| MODIS 1 km | 0.82 km | 2.31 km | **5.6 km** |

## 2. When events merge

Any detection linking two components merges them (single linkage). That happens:

* when a new detection falls between two events (tested: `test_new_detection_joining_two_fires_merges_them`);
* when a MODIS swath-edge pixel (up to 5.6 km reach) bridges two VIIRS-separated fires;
* on chaining across days in dense burning areas. Widest events: **115 km × 60 h** (5,640
  detections, Kimberley, Australia), 95 km, 75 km × 156 h (Botswana). These may be real
  landscape-scale grass fires or chains of separate burns. Without perimeters, this can't be decided.

The merged event keeps the id of the event holding the earliest detection. The other event **is
deleted**. Nothing records the merge.

## 3. When detections are separated

* Farther apart than `rᵢ + rⱼ + 1 km` from every member: VIIRS fires ~2.1 km apart stay separate;
  MODIS-only fires up to 5.6 km apart can merge.
* Observed more than 24 h after the previous detection of the component.

The 24 h rule cuts fires that are still burning. Time between consecutive passes inside multi-pass
events (68k gaps):

| p50 | p75 | p90 | p95 | p99 |
|---|---|---|---|---|
| 4 h | 12 h | 23 h | 24 h | 47 h |

The threshold sits at the 95th percentile of normal revisit gaps, and cloud cover adds to them.
Consistent with that, **25.5 % of all events start within 3 km of an event that went quiet 24–96 h
earlier** (29.4 % in central Africa). Some of these are the same fire, re-observed after a miss or
under cloud. Others are new burns next to old ones (prescribed and agricultural burning
re-ignites nearby). There is no label to separate the two.

Isolated detections are not filtered: **53 % of events are a single detection** (10.9 % of
detections). In central Africa, 62 % of events are a single satellite pass.

Parameter sensitivity, central Africa, 7 days (148k detections):

| tolerance / gap | events | single-detection | widest | events > 20 km wide |
|---|---|---|---|---|
| 1 km / 24 h (today) | 23,332 | 10,176 | 54 km | 34 |
| 0.5 km / 24 h | 28,087 | 12,914 | 54 km | 18 |
| 2 km / 24 h | 17,165 | 7,051 | 86 km | 77 |
| 1 km / 12 h | 29,505 | 12,845 | 44 km | 11 |
| 1 km / 48 h | 19,567 | 8,430 | 58 km | 47 |

The event count swings ±25 % with either parameter. The current values are plausible, but nothing
validated them against reference data.

## 4. When an event becomes inactive

`status = "active"` if the last detection is at most `event_active_hours = 24` old, else `"inactive"`.
It is recomputed after every ingest (`refresh_event_status`), so it can lag by up to one ingest
interval (60 min).

* The 24 h threshold equals the p90–p95 revisit gap. A burning fire missed by one or two passes
  shows as "inactive".
* "inactive" is shown in the UI as a bare state. It reads like "extinguished", which absence of
  detections does not prove.
* **There is no reactivation.** Because `max_gap_hours = active_hours = 24`, a detection arriving
  after an inactive spell always starts a *new* event. The old one stays inactive.

## 5. How event identifiers are preserved

`id = "evt_" + sha1(id of the earliest member detection)[:12]`. The detection id is itself a hash
of sensor, satellite, position (4 decimals) and minute.

Strengths:
* Deterministic and order-independent. A full rebuild and incremental updates produce
  **identical** events. Verified worldwide: 188,722 = 188,722 events, 0 differences.
* An event keeps its id while it grows, because new detections are later than its first one.
* An archive (SP) row replaces its NRT twin under the same detection id, so the event id survives.

Weaknesses:
* **Merge:** the younger event's id disappears. In a replay of central Africa as 6-hourly ingests
  (31 batches), **1,398 event ids vanished through merges**, with no record.
* **Late older data:** a history backfill adding a detection *earlier* than an event's first one
  renames the event.
* **Static flagging:** if the first detection is later flagged as a gas flare, the event is renamed.
* Consequences:
  * alerts point to deleted ids (`GET /api/events/{id}` → 404 when an alert is clicked);
  * the alert dedupe key `(rule, event_id)` can fire twice for one fire after a rename or merge;
  * there is no audit trail.

## 6. Incremental updates

`store.rebuild_events` re-clusters exactly the changed detections (new, SP upgrades, newly flagged
static) plus every member of (a) events containing a changed detection and (b) events within link
reach: `LINK_PAD_KM = 11` > the max link of 5.6 km, and `max_gap` in time. This is exact for
single linkage, because two separate components can only join through a changed detection.

| Worldwide, 7 days | |
|---|---|
| Hourly refresh (1,483 new detections) | 1,234 events rebuilt, 10.5 s |
| Full rebuild | 188,722 events, 275 s |
| Incremental vs full | identical |

Weakness: events are deleted and re-inserted, never updated, so no history is kept.

## 7. FRP

* `max_frp_mw`: maximum single-pixel FRP. Meaningful.
* `total_frp_mw`: **sum of all pixel FRPs over all passes and both sensors.** FRP is instantaneous
  power (MW). Summing observations taken at different times is not energy (that would need
  integration over time, i.e. FRE). When MODIS and VIIRS see the fire within minutes of each other
  (Aqua and S-NPP/NOAA-20 share the 13:30 orbit), the same fire is counted twice. Median
  `total/max = 1.0`, p99 = 13×. The UI shows it as **"Total FRP … MW"**, and the calendar sums FRP
  per day the same way. This should not be presented as a physical quantity.
* The FRP-weighted centroid is reasonable. It mixes sensors, which is acceptable for a position.
* MODIS and VIIRS FRP are not cross-calibrated. VIIRS 375 m detects smaller, cooler fires and
  saturates differently. Nothing reports FRP per sensor.

## 8. Area

`area_km2` is the union of each detection's **nominal** pixel (VIIRS 375 m, or MODIS 1 km when an
event has no VIIRS), rasterised on a 125 m grid in a local equirectangular projection (error < 1 %
at fire scale). `area_basis` says which. Strengths: it doesn't depend on viewing angle, and it
doesn't double count across sensors.

It is the **cumulative active-fire detection footprint at sensor resolution**, not burned area:
* it over-states small fires (one 375 m pixel = 0.14 km² for a fire of a few m²);
* it under-states fast fires that burn between passes;
* it misses smouldering or obscured fronts.

The code documents this. The UI labels it "Area … (VIIRS 375 m)", which a reader can still take as
burned area. Alert severity uses `area_km2 ≥ 10` as "critical".

## 9. Consumers that depend on the event model

* `GET /api/events` (filters: `status`, `min_area`, `instrument` via `modis_count`/`viirs_count`),
  `/events/count`, `/events/{id}`.
* Map tiles: events are embedded in point tiles.
* Calendar: `events_started` per day.
* Alerts: `rule_matches` uses `status`, `area_km2`, `max_frp_mw`, `detection_count`,
  `max_confidence_pct`. Dedupe uses `(rule_id, event_id)`.
* Globe: circle radius from `area_km2`, colour from `status`. Detail panel shows area, max/total FRP.
* (The brief mentions a React frontend; the frontend is Vite + TypeScript + CesiumJS without React.)

## 10. Summary

| | Verdict |
|---|---|
| Geodesic distance, sensor-aware pixel size, near-linear search | keep |
| Deterministic, order-independent, incremental = full | keep (it is the basis for everything else) |
| Exact incremental scope | keep |
| 24 h gap = 24 h activity threshold, no reactivation | **weakness**: splits ongoing fires (p95 revisit = 24 h) |
| Isolated detections become events (53 %) | **weakness**: no evidence level |
| MODIS swath-edge pixels link up to 5.6 km | **weakness**: possible false merges, unmeasurable without labels |
| Merged or renamed ids vanish | **weakness**: dangling alerts, duplicate alerts, no history |
| `total_frp_mw` presented as MW | **incorrect presentation** |
| Area labelled as area | **ambiguous presentation**: it is a detection footprint |
| Status "inactive" | **wording implies certainty** |
| Thresholds never validated against reference data | **open**: needs perimeter data (see limitations) |
