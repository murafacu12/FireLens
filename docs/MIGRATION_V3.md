# Migration v2 → v3 (fire event engine): rehearsal report and runbook

**Status: rehearsed on an isolated copy. The production database (`firelens.db`) has NOT been
migrated**, and it can't be by accident:
* a database that already holds detections is never upgraded unless the API is started with
  `FIRELENS_ALLOW_SCHEMA_UPGRADE=true`;
* without it, the API refuses to start and lists what would change;
* the guard runs before any table is created (`test_database_with_data_is_never_upgraded_without_explicit_permission`
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

## Rollback

v3 never modifies detections, but the v2 code can't read a v3 database's history. The v3 code also
refuses a v2 database with data unless it is allowed to upgrade. A rollback is therefore code and
data together:

1. Stop the API.
2. Move the migrated `firelens.db`, `firelens.db-wal` and `firelens.db-shm` aside. **Rename them; don't
   delete them.**
3. Copy the backup from step 3 to `firelens.db`.
4. Check out the pre-v3 code (the commit before the v3 branch) and start the API.

Data loss on rollback: only detections ingested after the migration. The public FIRMS files hold the
last 7 days, and the history backfill re-fetches older days, so they can be re-ingested.

Partial rollback without restoring the file is **not** supported. The v2 events are in
`fire_events_legacy_v2` for comparison and audit, not as a live fallback.
