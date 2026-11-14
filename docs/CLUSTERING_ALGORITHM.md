# Fire event clustering algorithm

**Status: implemented (events v3).** Based on [FIRE_CLUSTERING_AUDIT.md](FIRE_CLUSTERING_AUDIT.md). Results: [CLUSTERING_BENCHMARKS.md](CLUSTERING_BENCHMARKS.md). Limitations: [CLUSTERING_LIMITATIONS.md](CLUSTERING_LIMITATIONS.md).
Event fields, identity and lifecycle are in [FIRE_EVENT_MODEL.md](FIRE_EVENT_MODEL.md).

## Choice of method

| Method | Fit for FireLens |
|---|---|
| DBSCAN / ST-DBSCAN with `minPts > 1` | Border points depend on processing order, so incremental updates would no longer equal a full rebuild without extra machinery. Noise points would have to be dropped or handled separately. Needs a density parameter that varies by biome and sensor. **Rejected.** |
| Incremental clustering with centroids or radii (e.g. assign to the nearest active event) | Order-dependent, so its results can't be reproduced by a rebuild. A growing fire can outrun a fixed radius. **Rejected.** |
| **Connected components of a space-time graph** (what exists today, ST-DBSCAN with `minPts = 1`) | Deterministic and order-independent. Exact incremental scope ("only a changed detection can join two components"). Linear with a grid index. A fire can expand without limit as long as it stays contiguous. **Kept**, with the edge rule refined. |

One algorithm only. Density and evidence are reported as attributes of each event; they don't
change which detections belong together. This keeps clustering reproducible and keeps "is this a
real fire?" a separate, explainable question.

## Edge rule

Detections *i* and *j* (non-static) are linked when one of these holds:

| Edge | Time apart | Distance (geodesic, haversine) | Purpose |
|---|---|---|---|
| **E1 contiguity** (today's rule) | `Δt ≤ link_gap_hours` (24) | `d ≤ rᵢ + rⱼ + link_tolerance_km` (1.0) | same fire, consecutive passes |
| **E2 continuity** (new) | `link_gap_hours < Δt ≤ continuity_gap_hours` (72) | `d ≤ rᵢ + rⱼ + continuity_tolerance_km` (0.0): the pixels overlap | same place burning again after missed passes, cloud or smouldering |

`r` is half the diagonal of the actual pixel footprint (scan × track), as today. It reflects
geolocation and pixel extent, so MODIS swath-edge pixels reach further, which is physically right.

Why E2 is stricter: a quiet period is where false merges are most likely (a new burn next to an
old scar), so it only links **overlapping** pixels.

Measured on central Africa, 7 days, 148k detections. These are structural numbers; there are no
labels:

| Rule | events | "restart < 3 km after 24–96 h" | widest | > 20 km wide |
|---|---|---|---|---|
| today (E1 only) | 23,332 | 29.4 % | 54 km | 34 |
| **E1 + E2 to 72 h** | 21,002 (−10 %) | 23.5 % | 56 km | 39 |
| E1 + E2 to 96 h | 20,364 | 21.9 % | 56 km | 43 |
| plain 72 h gap (contrast) | 16,855 | 12.0 % | 59 km | 62 |

E2 absorbs a quarter of the suspected restarts and adds almost no chaining. A plain longer gap
absorbs more, but nearly doubles the very wide events. Neither result proves which is correct:
**the defaults stay configurable, and their validation against burned-area perimeters is listed as
open work.**

Parameters (environment, `FIRELENS_` prefix): `CLUSTER_TOLERANCE_KM` (1.0),
`CLUSTER_MAX_GAP_HOURS` (24, E1), `CLUSTER_CONTINUITY_GAP_HOURS` (72, 0 disables E2),
`CLUSTER_CONTINUITY_TOLERANCE_KM` (0.0), `EVENT_ACTIVE_HOURS` (24), `EVENT_ARCHIVE_DAYS` (30).

## Evidence level (isolated detections)

53 % of events are a single detection. They are **kept**: deleting observations loses history, and
one detection can be a real small fire. Each event gets an `evidence` class:

* `single_detection`: one pixel.
* `single_pass`: several pixels, all from one satellite overpass.
* `multi_pass`: seen on two or more overpasses (any satellites). This is the strongest evidence
  that a fire persists.

APIs and alerts can filter on it (`min_evidence`). Defaults stay as today, so nothing disappears
silently.

## Architecture

```mermaid
flowchart TD
    A[New / upgraded / re-flagged detections<br/>= changed set] --> B[Find affected events<br/>a) containing a changed detection<br/>b) within link reach: 11 km, continuity_gap in time]
    B --> C[Load changed detections + all members<br/>of affected events]
    C --> D[Connected components<br/>grid index lat x lon x time<br/>edges E1 / E2, haversine]
    D --> E[Build events<br/>metrics, evidence, area measures,<br/>pass timeline, lifecycle state]
    E --> F[Diff against the previous assignment<br/>continued / grown / merged / split / renamed / new]
    F --> G[(fire_events: live events<br/>replaced only where changed)]
    F --> H[(event_lineage: old id -> canonical id)]
    F --> I[(event_history: one version per change)]
    F --> J[(event_passes: FRP per overpass)]
    G --> K[Alerts: dedupe across lineage]
    G --> L[API / tiles / calendar]
```

## Incremental processing and consistency

* The scope rule stays the same. The only change is that the time reach becomes
  `continuity_gap_hours`, so affected events must be within 72 h instead of 24 h. Expected cost:
  more events per hourly update. It will be measured, with today's 10.5 s / 1,234 events
  worldwide as the baseline.
* Deterministic membership and id (earliest member) mean an incremental run and a full rebuild
  give the same live events. This stays a test (`incremental == full`) and a benchmark check.
* Lineage and history are a log of what *happened* in a given sequence of updates, so they are not
  part of that equality. A full rebuild also writes lineage: it diffs the previous `event_id` of
  every detection against the new one, so an id that disappears still resolves.

## Where it lives

| File | Role |
|---|---|
| `backend/app/ingest/clustering.py` | `cluster_detections` (E1/E2 edges, grid index), `build_event` (metrics, evidence, area measures, passes, lifecycle) |
| `backend/app/services/store.py` | `rebuild_events` (affected scope, diff → lineage/history, writes), `refresh_event_status` (time-driven transitions), `rebuild_all_events` |
| `backend/app/migrations.py` | `EVENTS_VERSION = 3`, additive columns, `fire_events_legacy_v2` snapshot before the rebuild |
| `backend/benchmarks/scenarios.py` | labelled synthetic scenarios + pairwise metrics |
| `backend/benchmarks/clustering_benchmark.py` | old vs new comparison on synthetic and real data |
| `backend/tests/test_event_engine.py` | scenario, identity, lifecycle, metrics and incremental == full tests |

## Validation plan

1. **Synthetic, labelled** (generated, ground truth by construction; reported separately from real
   data). Scenarios:
   1. one fire seen by several satellites;
   2. two fires 1.5 / 3 / 6 km apart;
   3. progressive expansion;
   4. intermittent detections (12–48 h gaps);
   5. reactivation after 30–70 h;
   6. isolated detections;
   7. duplicate observations (NRT then SP);
   8. two events joined by a bridging detection;
   9. a split caused by a re-flagged gas flare;
   10. equator vs 65° N (longitude scale).

   Metrics: pairwise precision / recall, false merges, false splits; old vs new rule.
2. **Real, unlabelled**: event counts, evidence classes, extents, restart share, events modified
   per hourly update, time, peak memory (tracemalloc on a regional subset), incremental == full.
3. **Randomised ingestion order**: the same detections ingested in different batch orders must
   give identical live events.
