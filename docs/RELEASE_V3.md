# Release readiness: fire event engine v3 (+ zoom-tile rendering, worldwide scale)

Date: 2026-11-15. Branch `events-v3` (not merged, not pushed). Production database migrated on
2026-11-14 23:36 UTC with approval.

## Verdict

**Ready to merge, with two open items for you:** the backup is not yet copied off the machine, and
there is no exact code rollback point (see "Rollback"). Data safety checks all pass. Performance has
one justified regression (hourly event update), described below.

## Evidence

| Area | Result |
|---|---|
| Tests | 176 passing (backend), frontend type-check clean |
| Engine quality, synthetic ground truth | false splits 2 → 0, false merges 1 → 1 (MODIS swath-edge resolution limit, under both rules) |
| Chord vs haversine | identical event membership on all 14 datasets (11 synthetic, Africa 148k, world 918k), both rules; 74 edge-case tests (±1 m, antimeridian, poles) |
| Incremental vs full rebuild | same partition **and** same ids: Africa and world, both rules; random batch orders in tests |
| Migration rehearsal (copy of production) | 18/18 checks |
| Production migration | 33.8 s, then verified against the backup: 18/18 |
| Production health after normal ingestion (345,217 detections) | 19/19 invariants |
| Post-migration ingestion | 2 manual runs from the app (23:41, 23:53 UTC): 155,787 new detections, **all** in valid v3 events. Scheduled run 00:37 UTC: no errors, 0 new (NASA had published nothing newer than 21:50 UTC), 105 recorded lifecycle transitions |
| Alerts, event growth, history, timeline, tiles, filters, calendar, export | 17/17 functional checks on an isolated copy of production |
| Frontend | event panel (new fields, per-pass chart, history), Latest/History, filters, sensor toggles; no console errors |
| Restoration | tested: backup checksum, byte-identical restore, integrity, counts |
| Rollback code `728f83b` on restored data | starts, serves, ingests (degraded feature set) |
| Roll-forward after rollback | 24/24 checks. Found and fixed a missing-`acq_hour` case in the upgrade |

## Performance (old rule → new rule, same code)

| | Africa (148k) | World (918k) |
|---|---|---|
| Hourly event update | 4.2 → 9.4 s | 4.2 → 12.1 s |
| Events touched per hourly update | 3,160 → 5,601 | 774 → 1,069 |
| Full rebuild | 10.5 → 15.2 s | 114.5 → 154.6 s |
| Peak memory, hourly | 56 → 93 MB | 33 → 40 MB |
| Peak memory, full rebuild | 222 → 211 MB | 1,557 → 1,480 MB |

Before this work, the hourly update took 10.5 s worldwide (old rule, old code).

## Remaining performance issues

1. **Hourly event update is about 3× slower worldwide (12.1 s).** The continuity rule makes clustering
   look 72 h back inside dense, long-lived fires. It is well within the 60-min ingest cycle.
   Profiled: 75 % of the time is clustering. Next step: time-ordered processing with per-cell pruning.
2. **Full rebuild memory scales with the database:** about 1.5 GB of Python memory for one worldwide
   week. It only runs when the event rules change, and only with explicit permission.
3. **The first worldwide pull** into the previously South-America-only database took 89 s (41,560
   events built). This happens once.
4. **Database growth:** about 60 MB per worldwide day plus event history. Lineage and history have
   no retention policy yet.

## Known limitations

See [CLUSTERING_LIMITATIONS.md](CLUSTERING_LIMITATIONS.md). In short:
* **No validation:** no ground-truth fire perimeters, so thresholds are not validated against them.
* **No burned area:** FireLens doesn't measure it (`burned_area_km2` is always null).
* **"Inactive" means no detection,** not that the fire is out.
* **MODIS swath edge:** fires about 5 km apart can merge.
* **FRP:** not cross-calibrated between sensors; no FRE.
* **NASA S-NPP feed:** currently returns 0 rows. This is upstream, and the other three satellites
  are fine.

## Rollback

* **Database:** `backups/firelens-v2-2026-11-14.db`, SHA-256 `d1733535…d85b7`. Restore tested.
  **Stored only on this machine.** Copy it, with its `.sha256` file, to external storage.
* **Code:** there is no exact pre-v3 commit. The tested substitute, `728f83b`, works with degraded
  features. Procedure: [MIGRATION_V3.md](MIGRATION_V3.md#rollback-tested-2026-11-15).

## Needs your decision

1. Copy the backup off the machine.
2. Review, then approve merging `events-v3` into `main` and pushing. Neither has been done.
