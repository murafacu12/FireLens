"""Rehearse the v2 -> v3 migration on an isolated copy of a database. The source is only ever opened
read-only (SQLite backup API from a `mode=ro` connection) and is never written, moved or deleted.

    python -m benchmarks.migration_rehearsal <source.db> <work dir>

Steps: snapshot copy -> pre-checks -> real application startup with the upgrade explicitly allowed
(guard -> create_all -> migrations) -> post-checks -> JSON report in <work dir>.
"""

import hashlib
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

DATA_TABLES = ["detections", "fire_cells", "coverage", "ingest_runs", "alert_rules", "alerts"]


def ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)


def detections_digest(conn) -> str:
    """Hash of every detection column except event_id (the only column events may change)."""
    cols = [r[1] for r in conn.execute("PRAGMA table_info(detections)") if r[1] != "event_id"]
    h = hashlib.sha256()
    for row in conn.execute(f"SELECT {', '.join(cols)} FROM detections ORDER BY id"):
        h.update(repr(row).encode())
    return h.hexdigest()


def table_counts(conn) -> dict:
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    return {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in sorted(names)}


def files_size(path: Path) -> int:
    return sum(p.stat().st_size for p in path.parent.glob(path.name + "*") if p.is_file())


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
    with ro(copy) as c:
        report["pre"] = {
            "integrity_check": c.execute("PRAGMA integrity_check").fetchone()[0],
            "counts": table_counts(c),
            "detections_digest": detections_digest(c),
            "events_version": dict(c.execute("SELECT key, value FROM app_meta").fetchall()).get("events_version"),
            "v2_event_ids": sorted(r[0] for r in c.execute("SELECT id FROM fire_events")),
            "alert_event_ids": sorted({r[0] for r in c.execute("SELECT event_id FROM alerts")}),
        }
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
    with ro(copy) as c:
        post = {
            "integrity_check": c.execute("PRAGMA integrity_check").fetchone()[0],
            "foreign_key_check": c.execute("PRAGMA foreign_key_check").fetchall(),
            "counts": table_counts(c),
            "detections_digest": detections_digest(c),
            "app_meta": dict(c.execute("SELECT key, value FROM app_meta").fetchall()),
        }
        pre = report["pre"]
        checks = {}
        checks["data tables unchanged"] = all(post["counts"].get(t) == pre["counts"].get(t) for t in DATA_TABLES if t in pre["counts"])
        checks["detections unchanged (all columns except event_id)"] = post["detections_digest"] == pre["detections_digest"]
        checks["v2 events kept in legacy table"] = post["counts"].get(LEGACY_EVENTS_TABLE) == pre["counts"]["fire_events"]
        legacy_ids = sorted(r[0] for r in c.execute(f"SELECT id FROM {LEGACY_EVENTS_TABLE}"))
        checks["legacy table holds exactly the v2 ids"] = legacy_ids == pre["v2_event_ids"]
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
        checks["lineage targets exist"] = all(n is None or n in live for _, n, _ in lineage)
        gone = [i for i in pre["v2_event_ids"] if i not in live]
        mapped = {o for o, _, _ in lineage}
        checks["every v2 id that is not live has a lineage record"] = all(i in mapped for i in gone)
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
    report["post"] = post
    report["checks"] = checks
    report["source_files_after"] = {p.name: p.stat().st_size for p in src.parent.glob(src.name + "*")}
    report["passed"] = all(checks.values())
    for k in ("v2_event_ids", "alert_event_ids"):
        report["pre"][k + "_count"] = len(report["pre"].pop(k))
    (work / "migration_rehearsal.json").write_text(json.dumps(report, indent=2, default=str))
    print(json.dumps({k: v for k, v in report.items() if k not in ("pre", "post")}, indent=2, default=str))
    print("post:", json.dumps({k: post[k] for k in ("counts", "lineage_kinds", "history_changes", "v2_ids_kept", "lifecycle", "evidence", "missing_indexes")}, default=str))
    print("pre counts:", report["pre"]["counts"])


if __name__ == "__main__":
    main()
