"""End-to-end functional check of a migrated database, run on an isolated COPY (the source is only
opened read-only). Exercises what production users touch: alert rules and alerts (incl. dedupe),
an incremental ingest growing an existing event (history, version, timeline), map tiles (ETag),
event detail / history / timeline / lineage, and the API filters the frontend uses.

    python -m benchmarks.post_migration_check <migrated.db> <work dir>
"""

import json
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.models import EventRow
from app.schemas import FireDetection, IngestReport
from app.services.pipeline import persist
from sqlalchemy import select


def main() -> None:
    src, work = Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve()
    work.mkdir(parents=True, exist_ok=True)
    copy = work / "functional_copy.db"
    for f in work.glob("functional_copy.db*"):
        f.unlink()
    with sqlite3.connect(f"file:{src.as_posix()}?mode=ro", uri=True) as s_conn, sqlite3.connect(copy) as c_conn:
        s_conn.backup(c_conn)
    settings = Settings(_env_file=None, database_url=f"sqlite:///{copy.as_posix()}", ingest_interval_minutes=0)
    results: dict[str, object] = {}

    def check(name: str, ok: bool, detail=None) -> None:
        results[name] = {"ok": bool(ok), "detail": detail}
        print(("OK  " if ok else "FAIL"), name, "" if detail is None else detail)

    with TestClient(create_app(settings)) as c:
        check("startup without upgrade flag (nothing pending)", c.get("/api/health").status_code == 200)
        ev = c.get("/api/events", params={"status": "active", "min_evidence": "multi_pass", "sort": "area", "limit": 1}).json()[0]
        pad = 0.05
        # ---- alerts: a rule over an active fire alerts at once, once
        rule = c.post("/api/alerts/rules", json={
            "name": "release check", "west": ev["west"] - pad, "south": ev["south"] - pad, "east": ev["east"] + pad,
            "north": ev["north"] + pad, "min_confidence": "low", "min_frp_mw": 0, "min_area_km2": 0, "min_detections": 1,
            "enabled": True}).json()
        alerts = [a for a in c.get("/api/alerts").json() if a["rule_id"] == rule["id"]]
        check("new rule alerts immediately on the active fire", any(a["event_id"] == ev["id"] for a in alerts), len(alerts))
        # ---- incremental ingest: a new detection next to the fire, 1 h after its last one
        last = datetime.fromisoformat(ev["end_at"].replace("Z", "+00:00"))
        new = FireDetection(
            id="release-check-001", source="VIIRS_NOAA20_NRT", instrument="VIIRS", satellite="NOAA-20", processing_level="NRT",
            latitude=ev["latitude"], longitude=ev["longitude"], acquired_at=last + timedelta(hours=1), frp_mw=42.0,
            scan_km=0.4, track_km=0.4, pixel_area_km2=0.16, nominal_resolution_m=375, confidence_raw="h", confidence_pct=90,
            confidence_level="high")
        version_before = c.get("/api/tiles/meta").json()["data_version"]
        z, size = 5, 180 / 2**5
        tx, ty = int((ev["longitude"] + 180) // size), int((ev["latitude"] + 90) // size)
        tile_url = f"/api/tiles/cells/{z}/{tx}/{ty}"
        etag_before = c.get(tile_url, params={"hours": 24 * 30}).headers["etag"]
        report = IngestReport(demo=False, source_mode="firms_api", sources=[], stored=0, events_rebuilt=0, alerts_triggered=0)
        persist(c.app.state.session_factory, settings, [new], datetime.now(UTC), report)
        with c.app.state.session_factory() as s:
            owner = s.get(__import__("app.models", fromlist=["DetectionRow"]).DetectionRow, new.id).event_id
            grown = s.scalars(select(EventRow).where(EventRow.id == owner)).one()
        check("new detection joined the existing v3 event", owner == ev["id"], owner)
        check("event grew by one detection, version bumped",
              grown.detection_count == ev["detection_count"] + 1 and grown.version > ev["version"],
              (ev["detection_count"], grown.detection_count, ev["version"], grown.version))
        hist = c.get(f"/api/events/{ev['id']}/history").json()
        check("history records the growth", hist[-1]["change"] in ("grown", "merged") and hist[-1]["snapshot"]["detection_count"] == grown.detection_count,
              hist[-1]["change"])
        timeline = c.get(f"/api/events/{ev['id']}/timeline").json()
        check("timeline has the new pass", any(p["frp_sum"] == 42.0 for p in timeline), len(timeline))
        check("no duplicate alert for the same fire", report.alerts_triggered == 0 and
              len([a for a in c.get("/api/alerts").json() if a["rule_id"] == rule["id"] and a["event_id"] == ev["id"]]) == 1)
        check("data_version bumped", c.get("/api/tiles/meta").json()["data_version"] == version_before + 1)
        r = c.get(tile_url, params={"hours": 24 * 30})
        check("tile content changed (new ETag)", r.headers["etag"] != etag_before)
        check("unchanged tile answers 304", c.get(tile_url, params={"hours": 24 * 30}, headers={"If-None-Match": r.headers["etag"]}).status_code == 304)
        pt = c.get(f"/api/tiles/points/7/{int((ev['longitude'] + 180) // (180 / 128))}/{int((ev['latitude'] + 90) // (180 / 128))}",
                   params={"hours": 24 * 30}).json()
        check("point tile contains the event and the new detection", ev["id"] in {e["id"] for e in pt["events"]} and "release-check-001" in pt["points"]["id"])
        # ---- old ids, lineage
        lin = c.get(f"/api/events/{ev['id']}/lineage").json()
        absorbed = [x["old_id"] for x in lin if x["new_id"] == ev["id"]]
        if absorbed:
            body = c.get(f"/api/events/{absorbed[0]}").json()
            check("an absorbed v2 id opens its successor", body["id"] == ev["id"] and body["resolved_from"] == absorbed[0])
        # ---- filters used by the frontend
        n_all = c.get("/api/events/count").json()["count"]
        checks = {
            "min_evidence=multi_pass": c.get("/api/events/count", params={"min_evidence": "multi_pass"}).json()["count"],
            "lifecycle_state=reactivated": c.get("/api/events/count", params={"lifecycle_state": "reactivated"}).json()["count"],
            "status=inactive": c.get("/api/events/count", params={"status": "inactive"}).json()["count"],
            "instrument=MODIS": c.get("/api/events/count", params={"instrument": "MODIS"}).json()["count"],
            "min_area=1": c.get("/api/events/count", params={"min_area": 1}).json()["count"],
        }
        check("event filters narrow the result", all(0 < v < n_all for v in checks.values()), {"all": n_all, **checks})
        world = [c.get(f"/api/tiles/cells/0/{x}/0", params={"hours": 24, "min_confidence": "nominal"}).json() for x in (0, 1)]
        check("world tiles (Latest 24 h) have both sensors",
              sum(t["viirs_total"] for t in world) > 0 and sum(t["modis_total"] for t in world) > 0,
              [(t["viirs_total"], t["modis_total"]) for t in world])
        cal = c.get("/api/calendar").json()
        check("calendar served", len(cal) == 30 and sum(d["detections"] for d in cal) > 0)
        hist_tile = c.get("/api/tiles/cells/3/5/2", params={"start": "2026-08-01T00:00:00Z", "end": "2026-08-31T23:59:59Z"})
        check("history-mode tile served", hist_tile.status_code == 200)
        export = c.get("/api/export/viirs.csv", params={"start": (last - timedelta(days=1)).isoformat(), "end": (last + timedelta(hours=2)).isoformat(),
                                                         "bbox": f"{ev['west']},{ev['south']},{ev['east']},{ev['north']}"})
        check("per-sensor CSV export", export.status_code == 200 and "bright_ti4_k" in export.text.splitlines()[0])
    results["passed"] = all(v["ok"] for v in results.values() if isinstance(v, dict))
    (work / "post_migration_check.json").write_text(json.dumps(results, indent=2, default=str))
    print("passed:", results["passed"])


if __name__ == "__main__":
    main()
