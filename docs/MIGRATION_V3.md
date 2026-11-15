# Migration v2 → v3 (fire event engine): rehearsal report and runbook

**Status: production (`firelens.db`) migrated on 2026-11-14, with approval, following the runbook
below.**

* Code: commit `3a34281` on branch `events-v3`. `main` is unchanged.
* Backup taken before the migration: `backups/firelens-v2-2026-11-14.db`.
  * 115.1 MB, integrity ok, row counts identical to the source.
  * SHA-256 `d173353599a774971f16d491def570398d1c40c36f25c30e9ad0a5fe089d85b7`, stored next to it.
* A fresh rehearsal on a new copy passed all checks right before the migration.
* Migration: 33.8 s via the guarded startup path, with the permission flag for that run only and
  network ingestion disabled. 43,481 v2 events became 40,267 v3 events (3,214 merged).
* Verification of production against the backup
  (`python -m benchmarks.migration_rehearsal verify ../firelens.db ../backups/firelens-v2-2026-11-14.db ../backups/verify`):
  **18 of 18 checks passed**. Size 115.2 MB → 169.2 MB.
* The API was restarted **without** the flag; the guard found nothing pending.
* In the app: the event panel, per-pass chart and history work, and old v2 ids resolve.

Upgrading a database that holds data always needs explicit permission. Without
`FIRELENS_ALLOW_SCHEMA_UPGRADE=true`, the API refuses to start and lists what would change. The guard
runs before any table is created (`test_database_with_data_is_never_upgraded_without_explicit_permission`
checks that not a single byte of the database file changes).

## What the migration changes

**Additive:**
* 3 new tables: `event_passes`, `event_lineage`, `event_history`;
* 17 new columns on `fire_events`;
* a one-time copy of the v2 events into `fire_events_legacy_v2`.

**Rebuilt (derived data):**
* `fire_events`, with the v3 rules;
* `detections.event_id`;
* the map pyramid (`fire_cells`), recomputed with the same values.

**Never modified:**
* every detection column except `event_id`;
* `coverage`, `ingest_runs`, `alert_rules`, `alerts`.

## Rehearsal on an isolated copy of `firelens.db`

Command (it opens the source read-only and only writes into the work directory):

```bash
cd backend && .venv/Scripts/python -m benchmarks.migration_rehearsal ../firelens.db <work dir>
```

Method:
* SQLite backup API from a `mode=ro` connection: a consistent snapshot, including the WAL. It took 0.3 s.
* Then the **real application startup** on the copy (guard → `create_all` → migrations), with
  network ingestion disabled.
* Then the checks.

| | Before (v2) | After (v3) |
|---|---|---|
| Duration of the upgrade (startup) | – | **20.0 s** |
| Database size (WAL checkpointed) | 115.2 MB | **169.2 MB (+54.0 MB, +47 %)** |
| detections | 189,430 | 189,430 |
| fire_cells | 149,488 | 149,488 |
| coverage / ingest_runs | 164 / 40 | 164 / 40 |
| alert_rules / alerts | 0 / 0 | 0 / 0 |
| fire_events | 43,481 | 40,267 |
| fire_events_legacy_v2 | – | 43,481 (the exact v2 ids) |
| event_lineage | – | 3,214 (all `merged`: 3,214 v2 events joined others) |
| event_history | – | 43,481 rows: 37,748 `updated` (metrics recomputed: spherical centroid, new fields) + 5,733 `merged` |
| event_passes | – | 79,911 |
| v2 ids still live | – | 40,267 of 43,481. The other 3,214 resolve through lineage. |
| Lifecycle | – | 2,634 active · 316 reactivated · 26,470 inactive · 10,847 archived (> 30 days) |
| Evidence | – | 13,914 multi-pass · 6,529 single-pass · 19,824 single detection |

Size growth: event history snapshots (~16 MB), the legacy copy of v2 events (~9 MB), the pass
timeline (80k rows), new columns and their indexes.

Checks (all passed):

| Check | Result |
|---|---|
| Data tables unchanged (row counts) | ✅ |
| Detections unchanged: SHA-256 over every column except `event_id`, before vs after | ✅ identical |
| v2 events preserved in `fire_events_legacy_v2`, exactly the same ids | ✅ |
| Every fire detection belongs to an event; no non-fire detection does | ✅ |
| No detection points to a missing event; no empty event | ✅ |
| `detection_count` = members, for every event | ✅ |
| `pass_count` = rows in `event_passes`, for every event | ✅ |
| Every lineage target exists; every v2 id that is not live has a lineage record | ✅ |
| Alerts still reach a live event (your database has 0 alerts, so this is vacuous here; covered by tests) | ✅ |
| Every live event has history | ✅ |
| All expected indexes and both sensor views exist | ✅ |
| `PRAGMA integrity_check` = ok, `foreign_key_check` empty | ✅ |
| `events_version` = 3 | ✅ |
| API on the migrated copy: `/health`, `/events`, `/tiles/meta` = 200; 200 of 200 v2 ids open through `/events/{id}` | ✅ |
| Source files (`firelens.db`, `-wal`) byte sizes and timestamps unchanged | ✅ |

At worldwide scale (a copy of the 918k-detection benchmark database), the same migration took
**185.5 s** (188,722 → 172,438 events, 16,284 merged). All 2,000 sampled v2 ids resolve.

## Runbook: when you approve migrating production

1. **Commit the v3 code on a branch**, so the previous version stays one checkout away.
2. **Stop the API** (the scheduler must not write during the backup).
3. **Back up** with the backup API. This gives a consistent copy that includes the WAL. Don't copy the
   `.db` file alone.
   ```bash
   cd backend && .venv/Scripts/python -c "import sqlite3; s = sqlite3.connect('file:../firelens.db?mode=ro', uri=True); d = sqlite3.connect('../backups/firelens-v2-2026-11-14.db'); s.backup(d); d.close(); print(sqlite3.connect('../backups/firelens-v2-2026-11-14.db').execute('pragma integrity_check').fetchone())"
   ```
   Keep the backup until v3 has run for a while.
4. **Rehearse once more** on a fresh copy, because data changed since this report. All checks must pass:
   ```bash
   cd backend && .venv/Scripts/python -m benchmarks.migration_rehearsal ../firelens.db ../backups/rehearsal
   ```
5. **Migrate:** start the API once with `FIRELENS_ALLOW_SCHEMA_UPGRADE=true`. The log lists the
   upgrade steps. Expect about 20 s at the current size.
6. **Remove** `FIRELENS_ALLOW_SCHEMA_UPGRADE` again, so later upgrades also need approval.
7. **Verify** in the app: event panel, map tiles, calendar, alerts.

## Rollback (tested 2026-11-15)

### Versions

| | Rollback target (v2) | Current (v3) |
|---|---|---|
| Database file | `backups/firelens-v2-2026-11-14.db` (115.1 MB), SHA-256 `d173353599a774971f16d491def570398d1c40c36f25c30e9ad0a5fe089d85b7` | `firelens.db` |
| Database schema / data versions (`app_meta`) | `events_version` 2, `cells_version` 1, `data_version` 1 | `events_version` 3, `cells_version` 1 |
| Database contents | 189,430 detections, 43,481 events (as of 2026-11-14 23:36 UTC) | grows with ingestion |
| Code | **No commit matches the code that wrote this backup** (the pre-v3 work was never committed). Tested substitute: **commit `728f83b`** on `main` | commit `3a34281`+ on branch `events-v3` |

What `728f83b` can and cannot do on the restored backup (tested on a copy):
* It starts. `/api/health`, `/api/events`, `/api/events/count`, `/api/detections/compact`,
  `/api/detections/grid` and `/api/calendar` all return 200.
* It ingests: 60 real FIRMS fixture detections were stored, events rebuilt, integrity ok.
* It lacks the later features: map tiles and the worldwide zoom pyramid, the gas-flare exclusion and
  the data-quality fixes. Its frontend loads all detections in view (the old, slow path). It doesn't
  fill `acq_hour` or classify non-fire sources on the rows it stores.
* An event rebuild costs it far more: 60 new detections re-clustered 32,187 events.

### Backup location

**Not confirmed off the production machine.** The backup, and its `.sha256` file, exist only in
`backups/` in the project folder (git-ignored), on the same machine as `firelens.db`. Copying it to
external storage is your step:
* copy both files to your external drive or cloud storage;
* verify the copy there with `sha256sum firelens-v2-2026-11-14.db`;
* compare the result with the `.sha256` file.

### Tested restoration procedure

Every step was run on scratch copies. Production was not touched.

1. **Stop the API.**
2. **Verify the backup:** its SHA-256 must equal the recorded one. *Tested: equal.*
3. **Set the current database aside.** Rename (don't delete) `firelens.db`, `firelens.db-wal` and
   `firelens.db-shm`, for example to `firelens-v3-<date>.db*`.
4. **Restore:** copy the backup to `firelens.db`, with no `-wal` or `-shm` next to it.
   *Tested: the restored file is byte-identical to the backup (SHA-256); `integrity_check` ok;
   189,430 detections, 43,481 events, `events_version` 2. Took 2.7 s.*
5. **Run the rollback code:** `git checkout 728f83b` (or a worktree) and start the API.
   *Tested: starts, serves and ingests (see above).*
6. **Data ingested after the migration** is not in the backup. The public FIRMS files hold the last
   7 days, and the history backfill re-fetches older days, so it can be re-ingested.

**Rolling forward again** after a rollback: start v3 once with `FIRELENS_ALLOW_SCHEMA_UPGRADE=true`,
after a rehearsal. *Tested end to end: restored v2 → `728f83b` ingest → v3 upgrade.* Rehearsal checks:
**24 of 24 passed** in 22.9 s. Along the way:
* the guard asked first, and listed "fill acq_hour for 60 detections (stored by older code)";
* the rows the old code stored without `acq_hour` were filled and are on the map again (pyramid
  total = all 189,490 detections);
* 7 of them were classified as gas flares;
* the 58 events the old code had built only from flare pixels were recorded as `removed`.

This test found the missing-`acq_hour` case. The v3 upgrade now always fills missing hours, and the
guard reports them (`test_roll_forward_after_rollback_fills_hours_older_code_left_empty`).

Partial rollback without restoring the file is **not** supported. The v2 events are in
`fire_events_legacy_v2` for comparison and audit, not as a live fallback.

## Tools

```bash
cd backend
.venv/Scripts/python -m benchmarks.migration_rehearsal <db> <work dir>                    # rehearse on an isolated copy
.venv/Scripts/python -m benchmarks.migration_rehearsal verify <migrated.db> <backup> <dir>  # right after a migration
.venv/Scripts/python -m benchmarks.migration_rehearsal health <db> <dir>                   # any time: invariants, read-only
.venv/Scripts/python -m benchmarks.post_migration_check <db> <dir>                         # alerts/ingest/tiles/filters, on a copy
```

`verify` compares against the backup, so it applies only right after a migration (it passed 18/18).
Once production ingests new data, use `health`: on 2026-11-15, **19/19 passed**, with 345,217
detections after the first post-migration ingests.
