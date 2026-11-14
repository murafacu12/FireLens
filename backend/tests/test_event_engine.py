"""Fire event engine v3: synthetic ground-truth scenarios, identity/lineage, lifecycle, metrics,
and incremental == full reconstruction. Scenario metrics are synthetic, not real-fire accuracy."""

import json
import random
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update

from app.config import Settings
from app.db import Base, make_engine, make_session_factory
from app.ingest.clustering import cluster_detections
from app.main import create_app
from app.models import DetectionRow, EventHistoryRow, EventLineageRow, EventPassRow, EventRow
from app.services import queries
from app.services.alerts import evaluate_rules
from app.services.store import Seed, rebuild_all_events, rebuild_events, refresh_event_status, upsert_detections
from benchmarks import scenarios as sc

P = dict(tolerance_km=1.0, max_gap_hours=24, active_hours=24, continuity_gap_hours=72, continuity_tolerance_km=0.0,
         archive_days=30)


@pytest.fixture
def factory(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'e.db'}")
    Base.metadata.create_all(engine)
    return make_session_factory(engine)


def ingest(s, dets, hours_after=1.0, **over):
    """Store detections and rebuild like an ingest would, with `now` just after the newest one."""
    now = max(d.acquired_at for d in dets) + timedelta(hours=hours_after)
    seeds = upsert_detections(s, dets)
    out = rebuild_events(s, seeds, now=now, **{**P, **over})
    s.flush()
    return out


def partition(s) -> set[frozenset]:
    groups: dict[str, set] = {}
    for did, eid in s.execute(select(DetectionRow.id, DetectionRow.event_id).where(DetectionRow.event_id.is_not(None))):
        groups.setdefault(eid, set()).add(did)
    return {frozenset(g) for g in groups.values()}


# --------------------------------------------------------------------- synthetic scenarios


@pytest.mark.parametrize("scenario", sc.all_scenarios(), ids=lambda x: x.name)
def test_scenarios_have_no_false_splits_and_no_new_false_merges(scenario):
    dets = [d for d in scenario.detections if d.id not in scenario.static_ids]
    truth = {d.id: scenario.truth[d.id] for d in dets}
    groups = cluster_detections(dets, P["tolerance_km"], P["max_gap_hours"], P["continuity_gap_hours"], P["continuity_tolerance_km"])
    m = sc.pairwise_scores(truth, {d.id: str(k) for k, g in enumerate(groups) for d in g})
    assert m["false_splits"] == 0, m
    # Known, documented limitation: adjacent MODIS swath-edge pixels (4 km) can be one fire or two.
    assert m["false_merges"] == (1 if scenario.name.startswith("2b_") else 0), m


def test_continuity_edge_is_what_fixes_intermittent_and_reactivated_fires():
    for scenario in (sc.intermittent(), sc.reactivation()):
        dets = scenario.detections
        assert len(cluster_detections(dets, 1.0, 24)) > 1  # previous rule: split
        assert len(cluster_detections(dets, 1.0, 24, 72, 0.0)) == 1
    # ...without gluing a new burn 1.5 km from an old scar onto it
    assert len(cluster_detections(sc.new_burn_beside_old_scar().detections, 1.0, 24, 72, 0.0)) == 2


def test_distance_scale_is_geodesic_at_any_latitude():
    equator = sc.expanding(0.0, 20.0, name="eq")
    north = sc.expanding(65.0, 20.0, name="65n")
    for scen in (equator, north):
        assert len(cluster_detections(scen.detections, **{k: P[k] for k in ("tolerance_km", "max_gap_hours")})) == 1
    # two fires 3 km apart stay apart at 65 N too (1 deg of longitude is only 47 km there)
    assert len(cluster_detections(sc.close_fires_viirs(65.0, 20.0).detections, 1.0, 24, 72, 0.0)) == 2


# --------------------------------------------------------------------- identity, lineage, history


def test_merge_keeps_oldest_id_and_old_id_still_resolves(factory):
    scen = sc.merging_fronts()
    by_time = sorted(scen.detections, key=lambda d: d.acquired_at)
    with factory() as s:
        ingest(s, [d for d in by_time if d.acquired_at < sc.T0 + timedelta(hours=13)])
        before = {e for (e,) in s.execute(select(EventRow.id))}
        assert len(before) == 2  # two separate fronts so far
        ingest(s, [d for d in by_time if d.acquired_at >= sc.T0 + timedelta(hours=13)])
        after = {e for (e,) in s.execute(select(EventRow.id))}
        assert len(after) == 1 and after < before  # the surviving id is one of the two originals
        (gone,) = before - after
        (kept,) = after
        lin = s.execute(select(EventLineageRow).where(EventLineageRow.old_id == gone)).scalars().one()
        assert (lin.kind, lin.new_id, lin.reason) == ("merged", kept, "new_detections")
        assert queries.resolve_event_id(s, gone) == kept
        changes = [h.change for h in s.scalars(select(EventHistoryRow).where(EventHistoryRow.event_id == kept))]
        assert changes[0] == "created" and "merged" in changes


def test_split_when_bridging_pixel_is_a_gas_flare(factory):
    scen = sc.split_by_flare()
    (flare,) = scen.static_ids
    with factory() as s:
        ingest(s, scen.detections)
        assert s.scalar(select(func.count()).select_from(EventRow)) == 1
        (original,) = s.scalars(select(EventRow.id)).all()
        s.execute(update(DetectionRow).where(DetectionRow.id == flare).values(static_kind="industrial"))
        row = s.get(DetectionRow, flare)
        rebuild_events(s, [Seed(row.latitude, row.longitude, row.acquired_at, row.id)], now=sc.T0 + timedelta(hours=13), **P)
        ids = set(s.scalars(select(EventRow.id)))
        assert len(ids) == 2 and original in ids  # the part holding the earliest pixel keeps the id
        lin = s.scalars(select(EventLineageRow).where(EventLineageRow.old_id == original)).all()
        assert [(r.kind, r.reason) for r in lin] == [("split", "static_reclassified")]
        assert lin[0].new_id in ids
        assert s.get(DetectionRow, flare).event_id is None


def test_late_older_detection_renames_and_old_id_resolves(factory):
    scen = sc.intermittent()
    early, later = scen.detections[:3], scen.detections[3:]
    with factory() as s:
        ingest(s, later)  # the archive for the first pass arrives later (history backfill)
        old_ids = set(s.scalars(select(EventRow.id)))
        ingest(s, early)
        (new_id,) = s.scalars(select(EventRow.id)).all()
        assert new_id not in old_ids
        for old in old_ids:
            assert queries.resolve_event_id(s, old) == new_id


def test_reingesting_identical_data_writes_no_new_version(factory):
    scen = sc.expanding()
    with factory() as s:
        ingest(s, scen.detections)
        n_hist = s.scalar(select(func.count()).select_from(EventHistoryRow))
        assert ingest(s, scen.detections) == []  # nothing new stored, nothing rebuilt
        # an archive (SP) re-delivery of one pixel re-clusters the event but changes nothing tracked
        d = scen.detections[0].model_copy(update={"processing_level": "SP"})
        ingest(s, [d], hours_after=200)
        assert s.scalar(select(func.count()).select_from(EventHistoryRow)) == n_hist + 1  # only the lifecycle moved
        assert s.scalars(select(EventHistoryRow.change).order_by(EventHistoryRow.id.desc())).first() == "state"


def test_alert_is_not_repeated_after_merge(factory):
    from app.models import AlertRow, AlertRuleRow

    scen = sc.merging_fronts()
    by_time = sorted(scen.detections, key=lambda d: d.acquired_at)
    with factory() as s:
        rule = AlertRuleRow(name="all", min_confidence="low", min_frp_mw=0, min_area_km2=0, min_detections=1, enabled=True,
                            created_at=sc.T0.replace(tzinfo=None))
        s.add(rule)
        s.flush()
        first = ingest(s, [d for d in by_time if d.acquired_at < sc.T0 + timedelta(hours=13)])
        assert len(evaluate_rules(s, first)) == 2
        merged = ingest(s, [d for d in by_time if d.acquired_at >= sc.T0 + timedelta(hours=13)])
        assert evaluate_rules(s, merged) == []  # both fronts already alerted; the merged fire is not new
        assert s.scalar(select(func.count()).select_from(AlertRow)) == 2


# --------------------------------------------------------------------- lifecycle


def test_lifecycle_reactivated_inactive_archived(factory):
    scen = sc.reactivation()
    with factory() as s:
        ingest(s, scen.detections, hours_after=2)
        ev = s.scalars(select(EventRow)).one()
        assert ev.lifecycle_state == "reactivated" and ev.status == "active"
        assert ev.run_start_at == (sc.T0 + timedelta(hours=84)).replace(tzinfo=None)
        last = ev.end_at
        assert refresh_event_status(s, 24, now=(last + timedelta(hours=30)).replace(tzinfo=sc.T0.tzinfo)) == 1
        assert (ev := s.get(EventRow, ev.id)).lifecycle_state == "inactive" and ev.status == "inactive"
        assert refresh_event_status(s, 24, now=(last + timedelta(days=31)).replace(tzinfo=sc.T0.tzinfo)) == 1
        s.expire_all()
        assert s.get(EventRow, ev.id).lifecycle_state == "archived"
        states = [(h.from_state, h.to_state) for h in s.scalars(select(EventHistoryRow).where(EventHistoryRow.change == "state"))]
        assert states == [("reactivated", "inactive"), ("inactive", "archived")]


# --------------------------------------------------------------------- metrics


def test_metrics_multi_satellite(factory):
    scen = sc.multi_satellite()
    with factory() as s:
        ingest(s, scen.detections)
        ev = s.scalars(select(EventRow)).one()
        counts = json.loads(ev.satellite_counts)
        assert set(counts) == {"NOAA-20", "NOAA-21", "Suomi NPP", "Aqua", "Terra"} and sum(counts.values()) == ev.detection_count
        assert ev.evidence == "multi_pass" and ev.pass_count == s.scalar(select(func.count()).select_from(EventPassRow))
        assert ev.mean_frp_viirs_mw and ev.mean_frp_modis_mw
        passes = s.scalars(select(EventPassRow)).all()
        assert ev.peak_pass_frp_mw == max(p.frp_sum for p in passes)
        assert ev.area_basis == "viirs_375m"  # footprint from the finest sensor, no double counting


def test_metrics_expanding_fire_growth_and_trend(factory):
    scen = sc.expanding()
    with factory() as s:
        ingest(s, scen.detections)
        ev = s.scalars(select(EventRow)).one()
        assert ev.intensity_trend == "stable"  # steady ~30 % per pass is growth, not a significant jump
        assert ev.footprint_growth_km2_per_day and ev.footprint_growth_km2_per_day > 0 and ev.footprint_growth_se is not None
        assert ev.extent_km > 8 and ev.detection_hull_km2 > 0
        assert ev.observed_hours == 84
        # a sudden flare-up: next pass sees ~4x the fire power of the recent passes
        b = sc._Builder("flare", 99)
        b.front("A", -10.0, sc.km_east(-10.0, -55.0, 8.4), 96, "NOAA-20", 10, frp=400)
        ingest(s, b.dets)
        assert s.scalars(select(EventRow)).one().intensity_trend == "rising"


def test_metrics_isolated_detection(factory):
    with factory() as s:
        ingest(s, sc.isolated().detections[:1])
        ev = s.scalars(select(EventRow)).one()
        assert (ev.evidence, ev.intensity_trend, ev.pass_count) == ("single_detection", "insufficient_data", 1)
        assert ev.footprint_growth_km2_per_day is None and ev.detection_hull_km2 == 0


# --------------------------------------------------------------------- incremental == full


def _world_of_scenarios():
    """Every scenario at its own place, so they can share one database."""
    builders = [sc.multi_satellite, sc.close_fires_viirs, sc.close_fires_modis_edge, sc.new_burn_beside_old_scar,
                sc.expanding, sc.intermittent, sc.reactivation, sc.isolated, sc.merging_fronts]
    dets = []
    for k, b in enumerate(builders):
        dets += b(-10.0 + 2 * (k // 5), -60.0 + 2 * (k % 5)).detections
    dets += sc.expanding(-17.0, 179.97, name="11_expanding_across_180").detections  # spans 180 degrees
    return dets


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_incremental_equals_full_rebuild_in_any_batch_order(tmp_path, seed):
    dets = _world_of_scenarios()
    rng = random.Random(seed)
    batches = [dets[i : i + 13] for i in range(0, len(dets), 13)]
    rng.shuffle(batches)  # archives and late passes arrive in any order
    engine = make_engine(f"sqlite:///{tmp_path / 'inc.db'}")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    end = max(d.acquired_at for d in dets) + timedelta(hours=1)
    with factory() as s:
        for b in batches:
            rebuild_events(s, upsert_detections(s, b), now=end, **P)
        incremental = partition(s)
        inc_ids = set(s.scalars(select(EventRow.id)))
        rebuild_all_events(s, **P)
        assert partition(s) == incremental
        assert set(s.scalars(select(EventRow.id))) == inc_ids  # same deterministic ids
        # every id ever seen during the incremental run still resolves to a live event
        for (old,) in s.execute(select(EventLineageRow.old_id).distinct()):
            assert queries.resolve_event_id(s, old) in inc_ids | {None}


# --------------------------------------------------------------------- API


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    db = tmp_path_factory.mktemp("ev") / "e.db"
    settings = Settings(_env_file=None, database_url=f"sqlite:///{db}", demo_mode=True, demo_days=10)
    with TestClient(create_app(settings)) as c:
        yield c


def test_api_event_v3_fields_timeline_and_history(client):
    ev = client.get("/api/events", params={"sort": "area", "limit": 1}).json()[0]
    for k in ("lifecycle_state", "evidence", "pass_count", "peak_pass_frp_mw", "detected_footprint_km2", "detection_hull_km2"):
        assert k in ev
    assert ev["burned_area_km2"] is None and ev["detected_footprint_km2"] == ev["area_km2"]
    timeline = client.get(f"/api/events/{ev['id']}/timeline").json()
    assert len(timeline) == ev["pass_count"] and max(p["frp_sum"] for p in timeline) == ev["peak_pass_frp_mw"]
    history = client.get(f"/api/events/{ev['id']}/history").json()
    assert history[0]["change"] == "created" and history[0]["snapshot"]["detection_count"] >= 1
    all_n = client.get("/api/events/count").json()["count"]
    multi = client.get("/api/events/count", params={"min_evidence": "multi_pass"}).json()["count"]
    assert 0 < multi < all_n


def test_api_old_id_resolves(client):
    ev = client.get("/api/events", params={"limit": 1}).json()[0]
    with client.app.state.session_factory() as s:
        s.add(EventLineageRow(old_id="evt_gone", new_id=ev["id"], kind="merged", reason="new_detections",
                              detections_moved=3, at=sc.T0.replace(tzinfo=None)))
        s.commit()
    body = client.get("/api/events/evt_gone").json()
    assert body["id"] == ev["id"] and body["resolved_from"] == "evt_gone"
    assert client.get("/api/events/evt_never").status_code == 404
    lin = client.get(f"/api/events/{ev['id']}/lineage").json()
    assert any(r["old_id"] == "evt_gone" for r in lin)


# --------------------------------------------------------------------- production safety


def test_database_with_data_is_never_upgraded_without_explicit_permission(tmp_path):
    from sqlalchemy import inspect, text

    from app.migrations import SchemaUpgradeRequired

    db = tmp_path / "prod.db"
    engine = make_engine(f"sqlite:///{db}")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    with factory() as s:
        upsert_detections(s, sc.expanding().detections)
        s.commit()
    with engine.begin() as conn:  # make it look like a v2 database
        conn.execute(text("DROP TABLE event_history"))
        conn.execute(text("ALTER TABLE fire_events DROP COLUMN evidence"))
    engine.dispose()  # checkpoints the setup writes into the main file
    before = db.read_bytes()
    settings = Settings(_env_file=None, database_url=f"sqlite:///{db}", ingest_interval_minutes=0)
    with pytest.raises(SchemaUpgradeRequired, match="FIRELENS_ALLOW_SCHEMA_UPGRADE"):
        with TestClient(create_app(settings)):
            pass
    assert db.read_bytes() == before  # not a single byte changed...
    wal = db.with_name(db.name + "-wal")
    assert not wal.exists() or wal.stat().st_size == 0  # ...and nothing pending in the write-ahead log
    allowed = Settings(_env_file=None, database_url=f"sqlite:///{db}", ingest_interval_minutes=0, allow_schema_upgrade=True)
    with TestClient(create_app(allowed)) as c:
        assert c.get("/api/events").status_code == 200
    eng = make_engine(f"sqlite:///{db}")
    assert "event_history" in inspect(eng).get_table_names()


# --------------------------------------------------------------------- antimeridian


def test_event_across_the_antimeridian_has_sane_geometry(factory):
    scen = sc.expanding(-17.0, 179.97, name="11_expanding_across_180")
    assert min(d.longitude for d in scen.detections) < -179 and max(d.longitude for d in scen.detections) > 179
    with factory() as s:
        ingest(s, scen.detections)
        ev = s.scalars(select(EventRow)).one()
        assert abs(ev.longitude) > 179.9  # centred on the fire, not at 0
        assert ev.west > ev.east  # box crosses 180 (bbox-filter convention)
        assert ev.extent_km < 15 and 0 < ev.detection_hull_km2 < 60 and ev.area_km2 < 20
        # incremental: a new detection on the other side of 180 joins the same event
        b = sc._Builder("x180", 5)
        b.front("A", -17.0, -179.99, 100, "NOAA-20", 3)
        ingest(s, b.dets)
        assert s.scalar(select(func.count()).select_from(EventRow)) == 1
