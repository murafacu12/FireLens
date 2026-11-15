"""Fire statistics and trends (analysis engine)."""

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import Base, make_engine, make_session_factory
from app.main import create_app
from app.services import analysis, cells
from app.services.store import rebuild_events, upsert_detections
from benchmarks import scenarios as sc

P = dict(tolerance_km=1.0, max_gap_hours=24, active_hours=24, continuity_gap_hours=72)


@pytest.fixture
def session(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'a.db'}")
    Base.metadata.create_all(engine)
    with make_session_factory(engine)() as s:
        dets = sc.multi_satellite(-10.0, -55.0).detections + sc.expanding(-12.0, -50.0).detections
        seeds = upsert_detections(s, dets)
        cells.refresh_cells(s, {cells.epoch_hour(x.t) for x in seeds})
        rebuild_events(s, seeds, now=sc.T0 + timedelta(days=5), **P)
        s.info["dets"] = dets
        yield s


START, END = sc.T0 - timedelta(hours=3), sc.T0 + timedelta(days=5)


def test_small_area_is_exact_and_sensors_stay_separate(session):
    one = [d for d in session.info["dets"] if d.id.startswith("s1-")]
    r = analysis.analyze(session, (-55.5, -10.5, -54.5, -9.5), START, END)
    assert r["method"] == "detections"
    assert r["detections"]["total"] == len(one)
    v, m = r["detections"]["by_sensor"]["viirs"], r["detections"]["by_sensor"]["modis"]
    assert v["detections"] == sum(d.instrument == "VIIRS" for d in one)
    assert m["detections"] == sum(d.instrument == "MODIS" for d in one)
    assert v["max_frp_mw"] == round(max(d.frp_mw for d in one if d.instrument == "VIIRS"), 1)
    assert sum(r["detections"]["by_confidence"].values()) == len(one)
    assert sum(d["detections"] for d in r["daily"]) == len(one)
    assert sum(d["events_started"] for d in r["daily"]) == 1
    assert r["events"]["started"] == 1 and r["events"]["by_evidence"] == {"multi_pass": 1}
    assert sum(h["viirs"] + h["modis"] for h in r["time_of_day"]["hours"]) == len(one)
    assert r["time_of_day"]["basis"] == "local_solar" and r["time_of_day"]["utc_offset_hours"] == -4


def test_wide_area_uses_the_pyramid_and_matches_the_exact_count(session):
    r = analysis.analyze(session, (-80.0, -30.0, -40.0, 10.0), START, END)
    assert r["method"].startswith("cells_")
    assert r["detections"]["total"] == len(session.info["dets"])
    assert r["events"]["started"] == 2 and len(r["events"]["largest_footprint"]) == 2
    assert r["events"]["largest_footprint"][0]["area_km2"] >= r["events"]["largest_footprint"][1]["area_km2"]
    world = analysis.analyze(session, None, START, END)
    assert world["detections"]["total"] == len(session.info["dets"]) and world["time_of_day"]["basis"] == "utc"
    wrap = analysis.analyze(session, (170.0, -30.0, -40.0, 10.0), START, END)  # crosses the antimeridian
    assert wrap["detections"]["total"] == len(session.info["dets"])


def test_trend_previous_period_and_coverage(session):
    box = (-51.0, -13.0, -49.0, -11.0)  # the expanding fire: more pixels every 12 h
    r = analysis.analyze(session, box, sc.T0, sc.T0 + timedelta(days=4), assume_loaded=True)
    assert r["trend"]["direction"] == "rising" and r["trend"]["second_half"] > r["trend"]["first_half"]
    # nothing is recorded as loaded here: trend and comparison are withheld rather than guessed
    r = analysis.analyze(session, box, sc.T0, sc.T0 + timedelta(days=4))
    assert r["trend"]["direction"] == "not_comparable" and r["unusual_days"] == []
    assert r["previous"]["comparable"] is False and r["previous"]["detections_change_pct"] is None
    later = analysis.analyze(session, box, sc.T0 + timedelta(days=2), sc.T0 + timedelta(days=4), assume_loaded=True)
    assert later["previous"]["detections"] > 0 and later["previous"]["detections_change_pct"] > 0
    assert later["previous"]["events_started"] == 1 and later["events"]["started"] == 0


def test_unusual_day_is_flagged(session):
    b = sc._Builder("spike", 3)
    for day in range(8):  # a quiet fire, with one day ten times busier
        b.front("S", 5.0, 20.0, 24 * day + 12, "NOAA-20", 40 if day == 5 else 4)
    seeds = upsert_detections(session, b.dets)
    cells.refresh_cells(session, {cells.epoch_hour(x.t) for x in seeds})
    r = analysis.analyze(session, (19.0, 4.0, 21.0, 6.0), sc.T0, sc.T0 + timedelta(days=8), assume_loaded=True)
    assert [d["detections"] for d in r["unusual_days"]] == [40]


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    db = tmp_path_factory.mktemp("an") / "a.db"
    settings = Settings(_env_file=None, database_url=f"sqlite:///{db}", demo_mode=True, demo_days=10)
    with TestClient(create_app(settings)) as c:
        yield c


def test_api_summary(client):
    r = client.get("/api/analysis/summary", params={"hours": 24 * 7}).json()
    grid = client.get("/api/detections/grid", params={"cell_deg": 10, "start": r["start"], "end": r["end"]}).json()
    assert r["detections"]["total"] == grid["total"] > 0
    assert r["previous"]["comparable"] is True
    assert r["events"]["started"] > 0 and r["events"]["strongest"][0]["peak_pass_frp_mw"] > 0
    assert client.get("/api/analysis/summary").status_code == 422
    bad = {"start": "2026-01-01T00:00:00Z", "end": "2020-01-01T00:00:00Z"}
    assert client.get("/api/analysis/summary", params=bad).status_code == 422
    box = client.get("/api/analysis/summary", params={"hours": 168, "bbox": "-66,-33,-63,-30"}).json()
    assert box["method"] == "detections" and box["detections"]["total"] <= r["detections"]["total"]
