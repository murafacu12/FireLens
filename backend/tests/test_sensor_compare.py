"""MODIS vs VIIRS comparison on coincident observations (synthetic ground truth)."""

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import Base, make_engine, make_session_factory
from app.main import create_app
from app.services import sensor_compare
from app.services.store import rebuild_events, upsert_detections
from benchmarks import scenarios as sc

P = dict(tolerance_km=1.0, max_gap_hours=24, active_hours=24, continuity_gap_hours=72)
LAT, LON = -10.0, -55.0


def build():
    b = sc._Builder("cmp", 11)
    # fire A: VIIRS NOAA-20 at 13:30, NOAA-21 50 min later, Aqua 100 min after NOAA-20 (drifted orbit)
    b.front("A", LAT, LON, 0.0, "NOAA-20", 5, frp=30)
    b.front("A", LAT, LON, 50 / 60, "NOAA-21", 5, frp=30)
    b.front("A", LAT, LON, 100 / 60, "Aqua", 2, frp=60)
    # fire B: small, 10 km away, only VIIRS sees it (inside the scene both sensors imaged)
    b.front("B", LAT, sc.km_east(LAT, LON, 10), 0.0, "NOAA-20", 1, frp=2)
    # fire C: 200 km away, VIIRS only, no MODIS anywhere near: not in a coincident scene
    b.front("C", LAT, sc.km_east(LAT, LON, 200), 0.0, "NOAA-20", 3, frp=10)
    # Terra in the morning over fire A: never coincident with VIIRS, excluded
    b.front("A", LAT, LON, -5.0, "Terra", 1, frp=20)
    return b.dets


@pytest.fixture
def session(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 's.db'}")
    Base.metadata.create_all(engine)
    with make_session_factory(engine)() as s:
        dets = build()
        rebuild_events(s, upsert_detections(s, dets), now=sc.T0 + timedelta(days=1), **P)
        s.info["dets"] = dets
        yield s


def test_coincident_scene_matching(session):
    r = sensor_compare.analyze(session, (LON - 3, LAT - 3, LON + 3, LAT + 3), sc.T0 - timedelta(hours=6), sc.T0 + timedelta(hours=6))
    m = r["modis_vs_viirs"]
    assert r["terra_detections_excluded"] == 1
    assert m["a"]["detections"] == 2 and m["a"]["in_scenes"] == 2 and m["a"]["matched"] == 2  # Aqua over fire A
    # VIIRS: fire A (10 px) and fire B (1 px) are in the scene, fire C (3 px) is not
    assert m["b"]["detections"] == 14 and m["b"]["in_scenes"] == 11
    assert m["b"]["matched"] == 10 and m["b"]["matched_pct"] == pytest.approx(90.9, abs=0.1)
    assert m["b"]["median_frp_unmatched_mw"] < m["b"]["median_frp_matched_mw"]  # the missed one is the small fire


def test_frp_compared_per_pass_and_cluster(session):
    dets = session.info["dets"]
    r = sensor_compare.analyze(session, None, sc.T0 - timedelta(hours=6), sc.T0 + timedelta(hours=6))
    cl = r["modis_vs_viirs"]["clusters"]
    aqua = sum(d.frp_mw for d in dets if d.satellite == "Aqua")
    by_sat = {p["sat_b"]: p for p in cl["points"]}
    assert set(by_sat) == {"NOAA-20", "NOAA-21"} and cl["count"] == 2  # one cluster per VIIRS pass, never summed over passes
    for sat in ("NOAA-20", "NOAA-21"):
        v = sum(d.frp_mw for d in dets if d.satellite == sat and sc_truth(d) == "A")
        assert by_sat[sat]["frp_a"] == pytest.approx(aqua, abs=0.1) and by_sat[sat]["frp_b"] == pytest.approx(v, abs=0.1)
    assert by_sat["NOAA-20"]["minutes_apart"] == 100 and by_sat["NOAA-21"]["minutes_apart"] == 50
    base = r["baseline_viirs_vs_viirs"]
    assert base["a"]["label"] == "VIIRS NOAA-20" and base["a"]["matched"] == 5 and base["b"]["matched_pct"] == 100.0


def sc_truth(d):
    return "A" if abs(d.longitude - LON) < 0.05 else "other"


def test_window_and_events(session):
    r = sensor_compare.analyze(session, None, sc.T0 - timedelta(hours=6), sc.T0 + timedelta(hours=6), window_min=60)
    pairs = {(p["a"], p["b"]) for p in r["modis_vs_viirs"]["satellite_pairs"]}
    assert pairs == {("Aqua", "NOAA-21")}  # NOAA-20 is 100 min before Aqua: outside a 60 min window
    ev = r["events"]
    assert ev["both"] == 1 and ev["viirs_only"] == 2 and ev["modis_only"] == 0
    assert ev["first_seen_by"] == {"modis": 1}  # Terra saw fire A in the morning, before VIIRS


def test_period_edges_use_partners_outside(session):
    # period ends between NOAA-20 and Aqua: the Aqua detections are outside it but still count as partners
    r = sensor_compare.analyze(session, None, sc.T0 - timedelta(hours=1), sc.T0 + timedelta(minutes=30), window_min=120)
    m = r["modis_vs_viirs"]
    assert m["a"]["detections"] == 0 and m["b"]["matched"] == 5  # NOAA-20 pixels of fire A matched by Aqua after the period


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    db = tmp_path_factory.mktemp("sc") / "s.db"
    with TestClient(create_app(Settings(_env_file=None, database_url=f"sqlite:///{db}", demo_mode=True, demo_days=10))) as c:
        yield c


def test_api(client):
    r = client.get("/api/analysis/sensors", params={"hours": 24 * 10}).json()
    assert r["modis_vs_viirs"]["a"]["label"] == "MODIS (Aqua)" and r["window_minutes"] == 120
    assert client.get("/api/analysis/sensors", params={"hours": 24, "window_minutes": 5}).status_code == 422
    assert client.get("/api/analysis/sensors").status_code == 422
