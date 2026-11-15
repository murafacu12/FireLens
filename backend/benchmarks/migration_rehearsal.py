"""Rehearse the v2 -> v3 migration on an isolated copy of a database. The source is only ever opened
read-only (SQLite backup API from a `mode=ro` connection) and is never written, moved or deleted.

    python -m benchmarks.migration_rehearsal <source.db> <work dir>
    python -m benchmarks.migration_rehearsal verify <migrated.db> <v2 backup.db> <work dir>
    python -m benchmarks.migration_rehearsal health <db> <work dir>

Rehearsal: snapshot copy -> pre-checks -> real application startup with the upgrade explicitly
allowed (guard -> create_all -> migrations) -> post-checks -> JSON report in <work dir>.
Verify: the same post-checks on an already migrated database, against its v2 backup (both read-only).
"""

import hashlib
from collections import defaultdict
import json
import sqlite3
import sys
import time
from pathlib import Path

from fastapi.testclient import TestClient

from app import models  # noqa: F401
from app.config import Settings
from app.db import Base
from app.main import create_app
from app.migrations import ADDED_INDEXES, LEGACY_EVENTS_TABLE, pending_upgrade
from app.db import make_engine

DATA_TABLES = ["detections", "coverage", "ingest_runs", "alert_rules", "alerts"]  # primary data (fire_cells is derived)


def ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)


def detections_digest(conn) -> str:
    """Hash of every detection column except the three the upgrade may legitimately write, each
    checked separately: event_id (event rebuild), acq_hour (filled when older code left it empty),
    static_kind (non-fire flag added to rows older code never classified; existing flags must stay)."""
    cols = [r[1] for r in conn.execute("PRAGMA table_info(detections)") if r[1] not in ("event_id", "acq_hour", "static_kind")]
    h = hashlib.sha256()
    for row in conn.execute(f"SELECT {', '.join(cols)} FROM detections ORDER BY id"):
        h.update(repr(row).encode())
    return h.hexdigest()


def table_counts(conn) -> dict:
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    return {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in sorted(names)}


def files_size(path: Path) -> int:
    return sum(p.stat().st_size for p in path.parent.glob(path.name + "*") if p.is_file())


def _hours_kept(c, pre) -> bool:
    """When older code left some hours empty, the filled ones change the digest: recompute it over
    the rows that already had an hour before (their values must be untouched)."""
    return pre.get("acq_hour_rows") is not None and hashlib.sha256(repr(c.execute(
        "SELECT id, acq_hour FROM detections WHERE id IN (SELECT value FROM json_each(?)) ORDER BY id",
        (json.dumps(pre["acq_hour_rows"]),)).fetchall()).encode()).hexdigest() == pre["acq_hour_digest"]


def _resolves(event_id: str, live: set, lineage: list, hops: int = 50) -> bool:
    nxt = defaultdict(list)
    for o, n, _ in lineage:
        nxt[o].append(n)
    seen = set()
    stack = [event_id]
    while stack and hops:
        i = stack.pop()
        if i in live:
            return True
        if i in seen:
            continue
        seen.add(i)
        stack.extend(n for n in nxt.get(i, ()) if n)
        hops -= 1
    return False


def pre_metrics(path: Path) -> dict:
    with ro(path) as c:
        return {
            "integrity_check": c.execute("PRAGMA integrity_check").fetchone()[0],
            "counts": table_counts(c),
            "detections_digest": detections_digest(c),
            "events_version": dict(c.execute("SELECT key, value FROM app_meta").fetchall()).get("events_version"),
            "v2_event_ids": sorted(r[0] for r in c.execute("SELECT id FROM fire_events")),
            "acq_hour_digest": hashlib.sha256(repr(c.execute(
                "SELECT id, acq_hour FROM detections WHERE acq_hour IS NOT NULL ORDER BY id").fetchall()).encode()).hexdigest(),
            "alert_event_ids": sorted({r[0] for r in c.execute("SELECT event_id FROM alerts")}),
            "acq_hour_rows": [r[0] for r in c.execute("SELECT id FROM detections WHERE acq_hour IS NOT NULL ORDER BY id")],
            "static_flags": dict(c.execute("SELECT id, static_kind FROM detections WHERE static_kind IS NOT NULL").fetchall()),
        }


def post_checks(path: Path, pre: dict) -> tuple[dict, dict]:
    with ro(path) as c:
        post = {
            "integrity_check": c.execute("PRAGMA integrity_check").fetchone()[0],
            "foreign_key_check": c.execute("PRAGMA foreign_key_check").fetchall(),
            "counts": table_counts(c),
            "detections_digest": detections_digest(c),
            "app_meta": dict(c.execute("SELECT key, value FROM app_meta").fetchall()),
        }
        checks = {}
        if pre is None:  # health mode: invariants only, nothing to compare with
            pre = {"counts": {}, "detections_digest": None, "v2_event_ids": [], "alert_event_ids": [], "static_flags": {},
                   "acq_hour_digest": None, "acq_hour_rows": None, "health": True}
        comparative = not pre.get("health")
        checks["data tables unchanged"] = all(post["counts"].get(t) == pre["counts"].get(t) for t in DATA_TABLES if t in pre["counts"])
        checks["detections unchanged (all columns except event_id, acq_hour)"] = post["detections_digest"] == pre["detections_digest"]
        if not comparative:
            for k in ("data tables unchanged", "detections unchanged (all columns except event_id, acq_hour)"):
                checks.pop(k, None)
        post_flags = dict(c.execute("SELECT id, static_kind FROM detections WHERE static_kind IS NOT NULL").fetchall())
        if comparative:
            checks["existing non-fire flags unchanged"] = all(post_flags.get(k) == v for k, v in pre["static_flags"].items())
        post["newly_flagged_non_fire"] = len(set(post_flags) - set(pre["static_flags"]))
        checks["no detection without acq_hour"] = c.execute("SELECT count(*) FROM detections WHERE acq_hour IS NULL").fetchone()[0] == 0
        if comparative:
            checks["existing acq_hour values unchanged"] = hashlib.sha256(repr(c.execute(
                "SELECT id, acq_hour FROM detections WHERE acq_hour IS NOT NULL ORDER BY id").fetchall()).encode()).hexdigest() == pre["acq_hour_digest"]                 or _hours_kept(c, pre)
        checks["acq_hour = hour of acquired_at everywhere"] = c.execute(
            "SELECT count(*) FROM detections WHERE acq_hour != CAST(strftime('%s', acquired_at) AS INTEGER) / 3600").fetchone()[0] == 0
        checks["map pyramid counts every detection"] = (c.execute("SELECT coalesce(sum(n), 0) FROM fire_cells WHERE level = 4").fetchone()[0]
                                                        == c.execute("SELECT count(*) FROM detections").fetchone()[0])
        if comparative:
            checks["v2 events kept in legacy table"] = post["counts"].get(LEGACY_EVENTS_TABLE) == pre["counts"]["fire_events"]
            legacy_ids = sorted(r[0] for r in c.execute(f"SELECT id FROM {LEGACY_EVENTS_TABLE}"))
            checks["legacy table holds exactly the v2 ids"] = legacy_ids == pre["v2_event_ids"]
        else:
            checks["v2 events kept in legacy table"] = post["counts"].get(LEGACY_EVENTS_TABLE, 0) > 0
        # membership
        checks["every fire detection is in an event"] = c.execute(
            "SELECT count(*) FROM detections WHERE static_kind IS NULL AND event_id IS NULL").fetchone()[0] == 0
        checks["no non-fire detection is in an event"] = c.execute(
            "SELECT count(*) FROM detections WHERE static_kind IS NOT NULL AND event_id IS NOT NULL").fetchone()[0] == 0
        checks["no detection points to a missing event"] = c.execute(
            "SELECT count(*) FROM detections d LEFT JOIN fire_events e ON e.id = d.event_id "
            "WHERE d.event_id IS NOT NULL AND e.id IS NULL").fetchone()[0] == 0
        checks["detection_count matches members for every event"] = c.execute(
            "SELECT count(*) FROM fire_events e JOIN (SELECT event_id, count(*) n FROM detections "
            "WHERE event_id IS NOT NULL GROUP BY event_id) m ON m.event_id = e.id WHERE m.n != e.detection_count").fetchone()[0] == 0
        checks["no empty event"] = c.execute(
            "SELECT count(*) FROM fire_events e WHERE NOT EXISTS (SELECT 1 FROM detections d WHERE d.event_id = e.id)").fetchone()[0] == 0
        checks["passes add up to pass_count"] = c.execute(
            "SELECT count(*) FROM fire_events e LEFT JOIN (SELECT event_id, count(*) n FROM event_passes GROUP BY event_id) p "
            "ON p.event_id = e.id WHERE coalesce(p.n, 0) != e.pass_count").fetchone()[0] == 0
        # lineage integrity
        live = {r[0] for r in c.execute("SELECT id FROM fire_events")}
        lineage = c.execute("SELECT old_id, new_id, kind FROM event_lineage").fetchall()
        checks["every lineage target resolves to a live event (through chains)"] = all(
            n is None or _resolves(n, live, lineage) for _, n, _ in lineage)
        gone = [i for i in pre["v2_event_ids"] if i not in live]
        mapped = {o for o, _, _ in lineage}
        if not comparative:
            has_legacy = LEGACY_EVENTS_TABLE in post["counts"]
            legacy = c.execute(f"SELECT id FROM {LEGACY_EVENTS_TABLE}") if has_legacy else []
            gone = [r[0] for r in legacy if r[0] not in live]
        checks["every v2 id that is not live has a lineage record"] = all(i in mapped for i in gone)
        removed = {o for o, _, k in lineage if k == "removed"}  # every detection reclassified as non-fire
        checks["every v2 id resolves to a live event, or was removed as non-fire"] = all(
            _resolves(i, live, lineage) or i in removed for i in gone)
        post["v2_ids_removed_as_non_fire"] = len([i for i in gone if i in removed])
        checks["alerts still reach a live event"] = all(
            a in live or any(o == a and (n in live) for o, n, _ in lineage) for a in pre["alert_event_ids"])
        checks["one history row (created) per live event at least"] = c.execute(
            "SELECT count(*) FROM fire_events e WHERE NOT EXISTS (SELECT 1 FROM event_history h WHERE h.event_id = e.id)").fetchone()[0] == 0
        idx = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        expected = {i.name for t in Base.metadata.tables.values() for i in t.indexes} | {n for n, _, _ in ADDED_INDEXES}
        checks["all expected indexes exist"] = expected <= idx
        post["missing_indexes"] = sorted(expected - idx)
        checks["views exist"] = {"modis_detections", "viirs_detections"} <= {
            r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='view'")}
        checks["integrity_check ok"] = post["integrity_check"] == "ok" and not post["foreign_key_check"]
        checks["events_version is 3"] = post["app_meta"].get("events_version") == "3"
        post["lineage_kinds"] = dict(c.execute("SELECT kind, count(*) FROM event_lineage GROUP BY kind").fetchall())
        post["history_changes"] = dict(c.execute("SELECT change, count(*) FROM event_history GROUP BY change").fetchall())
        post["v2_ids_kept"] = len(set(pre["v2_event_ids"]) & live)
        post["lifecycle"] = dict(c.execute("SELECT lifecycle_state, count(*) FROM fire_events GROUP BY 1").fetchall())
        post["evidence"] = dict(c.execute("SELECT evidence, count(*) FROM fire_events GROUP BY 1").fetchall())
    return post, checks


def health(db: Path, work: Path) -> None:
    """Invariants of a v3 database in normal use (no backup to compare with). Read-only."""
    work.mkdir(parents=True, exist_ok=True)
    post, checks = post_checks(db, None)
    report = {"database": str(db), "checks": checks, "passed": all(checks.values()), "size_bytes": files_size(db),
              "counts": post["counts"], "lifecycle": post["lifecycle"], "evidence": post["evidence"]}
    (work / "migration_health.json").write_text(json.dumps(report, indent=2, default=str))
    print(json.dumps(report, indent=2, default=str))


def verify(migrated: Path, backup: Path, work: Path) -> None:
    work.mkdir(parents=True, exist_ok=True)
    pre = pre_metrics(backup)
    post, checks = post_checks(migrated, pre)
    report = {"migrated": str(migrated), "backup": str(backup), "checks": checks, "passed": all(checks.values()),
              "size_bytes": files_size(migrated), "backup_size_bytes": files_size(backup),
              "post": {k: post[k] for k in ("counts", "lineage_kinds", "history_changes", "v2_ids_kept", "lifecycle", "evidence", "missing_indexes", "integrity_check", "newly_flagged_non_fire")},
              "pre_counts": pre["counts"]}
    (work / "migration_verify.json").write_text(json.dumps(report, indent=2, default=str))
    print(json.dumps(report, indent=2, default=str))


def main() -> None:
    src, work = Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve()
    work.mkdir(parents=True, exist_ok=True)
    copy = work / "rehearsal_copy.db"
    for f in work.glob("rehearsal_copy.db*"):
        f.unlink()
    report: dict = {"source": str(src)}
    report["source_files_before"] = {p.name: p.stat().st_size for p in src.parent.glob(src.name + "*")}

    # 1. consistent snapshot through the backup API, reading the source read-only
    t = time.time()
    with ro(src) as s_conn, sqlite3.connect(copy) as c_conn:
        s_conn.backup(c_conn)
    report["snapshot_s"] = round(time.time() - t, 1)
    with sqlite3.connect(copy) as c:  # the copy stands alone: fold its WAL in, start clean
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    report["size_before_bytes"] = files_size(copy)

    # 2. pre-checks
    report["pre"] = pre_metrics(copy)
    report["pending"] = pending_upgrade(make_engine(f"sqlite:///{copy}"))

    # 3. real startup on the copy, upgrade explicitly allowed, no network ingestion
    settings = Settings(_env_file=None, database_url=f"sqlite:///{copy}", allow_schema_upgrade=True,
                        ingest_interval_minutes=0, firms_map_key=None)
    t = time.time()
    with TestClient(create_app(settings)) as client:
        report["migration_s"] = round(time.time() - t, 1)
        report["api_smoke"] = {
            "health": client.get("/api/health").status_code,
            "events": client.get("/api/events", params={"limit": 5}).status_code,
            "tiles_meta": client.get("/api/tiles/meta").status_code,
            "old_ids_resolve_via_api": sum(
                client.get(f"/api/events/{i}").status_code == 200 for i in report["pre"]["v2_event_ids"][:200]),
        }
    report["size_after_bytes"] = files_size(copy)

    # 4. post-checks
    post, checks = post_checks(copy, report["pre"])
    report["post"] = post
    report["checks"] = checks
    report["source_files_after"] = {p.name: p.stat().st_size for p in src.parent.glob(src.name + "*")}
    report["passed"] = all(checks.values())
    report["pre"].pop("acq_hour_rows", None)
    report["pre"].pop("static_flags", None)
    for k in ("v2_event_ids", "alert_event_ids"):
        report["pre"][k + "_count"] = len(report["pre"].pop(k))
    (work / "migration_rehearsal.json").write_text(json.dumps(report, indent=2, default=str))
    print(json.dumps({k: v for k, v in report.items() if k not in ("pre", "post")}, indent=2, default=str))
    print("post:", json.dumps({k: post[k] for k in ("counts", "lineage_kinds", "history_changes", "v2_ids_kept", "lifecycle", "evidence", "missing_indexes")}, default=str))
    print("pre counts:", report["pre"]["counts"])


if __name__ == "__main__":
    if sys.argv[1] == "health":
        health(Path(sys.argv[2]).resolve(), Path(sys.argv[3]).resolve())
    elif sys.argv[1] == "verify":
        verify(Path(sys.argv[2]).resolve(), Path(sys.argv[3]).resolve(), Path(sys.argv[4]).resolve())
    else:
        main()
