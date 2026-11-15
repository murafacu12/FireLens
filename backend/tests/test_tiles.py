"""Zoom pyramid, map tiles, per-sensor analysis views and tile-set event rebuilds."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, inspect, select, text

from app.config import Settings
from app.db import Base, make_engine, make_session_factory
from app.main import create_app
from app.migrations import create_views, upgrade
from app.models import DetectionRow, EventRow, FireCellRow
from app.schemas import FireDetection
from app.services import cells, queries
from app.services.store import rebuild_events, upsert_detections

T0 = datetime(2026, 7, 28, 4, 0, tzinfo=UTC)


def det(i, lat, lon, instrument="VIIRS", hours=0.0, frp=10.0, conf="nominal", kind=None):
    nominal = 0.375 if instrument == "VIIRS" else 1.0
    pct = {"low": 20, "nominal": 55, "high": 90}[conf]
    return FireDetection(
        id=f"d{i}", source="x", instrument=instrument, satellite="NOAA-20" if instrument == "VIIRS" else "Aqua",
        processing_level="NRT", latitude=lat, longitude=lon, acquired_at=T0 + timedelta(hours=hours), frp_mw=frp,
        scan_km=nominal, track_km=nominal, pixel_area_km2=nominal**2, nominal_resolution_m=int(nominal * 1000),
        confidence_raw=conf[0], confidence_pct=pct, confidence_level=conf, static_kind=kind,
        brightness_k=330.0, brightness_secondary_k=290.0,
    )


@pytest.fixture
def factory(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 't.db'}")
    Base.metadata.create_all(engine)
    create_views(engine)
    return make_session_factory(engine)


def sample():
    return [
        det(1, -31.20, -64.40, hours=0, frp=5),
        det(2, -31.21, -64.41, hours=0.2, frp=50, conf="high"),
        det(3, -31.22, -64.40, "MODIS", hours=1, frp=80),
        det(4, -31.90, -63.10, hours=5, conf="low"),
        det(5, 45.10, 10.20, hours=2, frp=12),
        det(6, -38.45, -68.55, hours=3, kind="industrial"),  # a gas flare
        det(7, 0.0, 179.99, hours=1),  # next to the antimeridian
    ]


# --------------------------------------------------------------------- pyramid


def test_every_level_adds_up_to_the_detections(factory):
    with factory() as s:
        seeds = upsert_detections(s, sample())
        cells.refresh_cells(s, {cells.epoch_hour(x.t) for x in seeds})
        for level in range(cells.MIN_LEVEL, cells.MAX_STORED_LEVEL + 1):
            n = s.scalar(select(func.sum(FireCellRow.n)).where(FireCellRow.level == level))
            assert n == 7, level
        # a parent cell is the sum of its children (cx >> 1)
        frp = dict(s.execute(select(FireCellRow.level, func.sum(FireCellRow.frp_sum)).group_by(FireCellRow.level)).all())
        assert len({round(v, 6) for v in frp.values()}) == 1


def test_refresh_recomputes_only_changed_hours_exactly(factory):
    with factory() as s:
        seeds = upsert_detections(s, sample())
        cells.refresh_cells(s, {cells.epoch_hour(x.t) for x in seeds})
        before = s.scalar(select(func.count()).select_from(FireCellRow))
        new = upsert_detections(s, [det(8, -31.205, -64.405, hours=0.5, frp=200)])  # same hour as d1/d2
        cells.refresh_cells(s, {cells.epoch_hour(x.t) for x in new})
        total = s.scalar(select(func.sum(FireCellRow.n)).where(FireCellRow.level == 9))
        top = s.scalar(select(func.max(FireCellRow.frp_max)))
        assert total == 8 and top == 200
        assert s.scalar(select(func.count()).select_from(FireCellRow)) >= before


def _tile_of(lat, lon, z):
    size = cells.tile_deg(z)
    return int((lon + 180) // size), int((lat + 90) // size)


def test_stored_tile_matches_tile_summed_from_raw_detections(factory):
    with factory() as s:
        seeds = upsert_detections(s, sample())
        cells.refresh_cells(s, {cells.epoch_hour(x.t) for x in seeds})
        h0, h1 = cells.epoch_hour(T0 - timedelta(hours=1)), cells.epoch_hour(T0 + timedelta(hours=10))
        start, end = cells.hour_start(h0), cells.hour_start(h1 + 1)
        for z in range(0, 6):
            x, y = _tile_of(-31.2, -64.4, z)
            stored = cells.stored_cell_tile(s, z, x, y, h0, h1, None, False)
            raw = cells.raw_cell_tile(s, z, x, y, queries.DetectionFilter(start=start, end=end))
            assert stored["cells"]["c"] == raw["cells"]["c"], z
            assert stored["cells"]["vn"] == raw["cells"]["vn"] and stored["cells"]["mn"] == raw["cells"]["mn"]
            assert stored["cells"]["vlat"] == raw["cells"]["vlat"]
        z = 5
        x, y = _tile_of(-31.2, -64.4, z)
        t = cells.stored_cell_tile(s, z, x, y, h0, h1, None, False)
        assert (t["viirs_total"], t["modis_total"]) == (3, 1)  # sensors reported separately
        high = cells.stored_cell_tile(s, z, x, y, h0, h1, "high", False)
        assert high["viirs_total"] + high["modis_total"] == 1
        x, y = _tile_of(-38.45, -68.55, z)
        assert cells.stored_cell_tile(s, z, x, y, h0, h1, None, False)["viirs_total"] == 0  # gas flare hidden
        assert cells.stored_cell_tile(s, z, x, y, h0, h1, None, True)["viirs_total"] == 1


def test_point_tile_holds_detections_and_events_once(factory):
    with factory() as s:
        seeds = upsert_detections(s, sample())
        rebuild_events(s, seeds, 1.0, 24, 24, now=T0 + timedelta(hours=6))
        x, y = _tile_of(-31.2, -64.4, cells.POINT_ZOOM)
        t = cells.point_tile(s, x, y, queries.DetectionFilter(), queries.EventFilter())
        assert t["points"]["count"] == 3 and sorted(t["points"]["viirs"]) == [0, 1, 1]
        assert len(t["events"]) == 1 and t["events"][0]["detection_count"] == 3
        # a point exactly on a tile edge is drawn by one tile only
        upsert_detections(s, [det(9, -30.9375, -64.6875, hours=1)])
        size = cells.tile_deg(cells.POINT_ZOOM)
        xe, ye = int((-64.6875 + 180) / size), int((-30.9375 + 90) / size)
        counts = [cells.point_tile(s, xx, yy, queries.DetectionFilter(), queries.EventFilter())["points"]["id"].count("d9")
                  for xx in (xe - 1, xe) for yy in (ye - 1, ye)]
        assert sum(counts) == 1


def test_resolve_hours_snaps_rolling_windows():
    now = datetime(2026, 11, 14, 10, 37, tzinfo=UTC)
    h0, h1 = cells.resolve_hours(24, None, None, now)
    assert h1 - h0 == 24 and cells.hour_start(h1) == datetime(2026, 11, 14, 10)
    assert cells.resolve_hours(24, None, None, now + timedelta(minutes=20)) == (h0, h1)  # same tiles all hour


# --------------------------------------------------------------------- event rebuild regions


def test_far_apart_fires_do_not_rebuild_the_ground_between_them(factory):
    with factory() as s:
        # three fires on a line, 10 degrees apart; the middle one is not touched by the update
        first = upsert_detections(s, [det(1, -20, -60), det(2, -20, -50), det(3, -20, -40)])
        rebuild_events(s, first, 1.0, 24, 24, now=T0)
        middle = s.scalar(select(EventRow.id).where(EventRow.longitude > -55, EventRow.longitude < -45))
        new = upsert_detections(s, [det(4, -20.001, -60, hours=1), det(5, -20.001, -40, hours=1)])
        rebuilt = rebuild_events(s, new, 1.0, 24, 24, now=T0)
        assert sorted(e.detection_count for e in rebuilt) == [2, 2]  # the middle fire was left alone
        assert s.scalar(select(EventRow.id).where(EventRow.longitude > -55, EventRow.longitude < -45)) == middle


# --------------------------------------------------------------------- analysis views + migration


def test_sensor_views_use_native_field_names(factory):
    with factory() as s:
        upsert_detections(s, sample())
        s.commit()
        modis = s.execute(text("SELECT * FROM modis_detections")).mappings().all()
        viirs = s.execute(text("SELECT * FROM viirs_detections")).mappings().all()
        assert len(modis) == 1 and len(viirs) == 6
        assert {"brightness_t21_k", "bright_t31_k", "confidence_pct"} <= set(modis[0])
        assert {"bright_ti4_k", "bright_ti5_k", "confidence_class", "ti4_saturated"} <= set(viirs[0])
        assert {r["confidence_class"] for r in viirs} == {"n", "h", "l"}


def test_upgrade_adds_hour_column_and_builds_pyramid(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'old.db'}")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    with factory() as s:
        upsert_detections(s, sample())
        s.commit()
    with engine.begin() as conn:  # a database from before the pyramid
        conn.execute(text("DROP INDEX ix_detections_acq_hour"))
        conn.execute(text("DROP INDEX ix_detections_instrument_hour"))
        conn.execute(text("ALTER TABLE detections DROP COLUMN acq_hour"))
        conn.execute(text("DROP TABLE fire_cells"))
    Base.metadata.create_all(engine)

    class S:
        cluster_tolerance_km, cluster_max_gap_hours, event_active_hours = 1.0, 24.0, 24.0

    upgrade(engine, factory, S())
    assert "ix_detections_instrument_hour" in {i["name"] for i in inspect(engine).get_indexes("detections")}
    with factory() as s:
        hours = dict(s.execute(select(DetectionRow.id, DetectionRow.acq_hour)).all())
        assert hours["d1"] == cells.epoch_hour(T0)
        assert s.scalar(select(func.sum(FireCellRow.n)).where(FireCellRow.level == 4)) == 7
        assert cells.data_version(s) >= 1


# --------------------------------------------------------------------- API


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    db = tmp_path_factory.mktemp("tiles") / "t.db"
    settings = Settings(_env_file=None, database_url=f"sqlite:///{db}", demo_mode=True, demo_days=10)
    with TestClient(create_app(settings)) as c:
        yield c


def test_world_tiles_count_every_detection(client):
    meta = client.get("/api/tiles/meta").json()
    assert meta["data_version"] >= 1 and meta["point_zoom"] == 7
    total = client.get("/api/detections/grid", params={"cell_deg": 10}).json()["total"]
    tiles = [client.get(f"/api/tiles/cells/0/{x}/0", params={"hours": 24 * 400}).json() for x in (0, 1)]
    assert sum(t["viirs_total"] + t["modis_total"] for t in tiles) == total
    assert all(t["level"] == 4 and len(t["cells"]["c"]) == len(t["cells"]["vn"]) for t in tiles)


def test_tiles_revalidate_with_etag_and_follow_filters(client):
    url = "/api/tiles/cells/3/5/2"  # 22.5 deg tile over central Argentina
    r = client.get(url, params={"hours": 24 * 400})
    assert r.status_code == 200 and r.headers["etag"] and r.headers["cache-control"] == "no-cache"
    again = client.get(url, params={"hours": 24 * 400}, headers={"If-None-Match": r.headers["etag"]})
    assert again.status_code == 304 and not again.content
    body = r.json()
    assert body["viirs_total"] > 0 and body["modis_total"] > 0
    raw = client.get(url, params={"hours": 24 * 400, "min_frp": 0}).json()  # forces the raw path
    assert raw["viirs_total"] == body["viirs_total"] and raw["cells"]["c"] == body["cells"]["c"]
    ev = client.get("/api/events", params={"sort": "area", "limit": 1, "bbox": "-67.5,-45,-45,-22.5"}).json()[0]
    total = body["viirs_total"] + body["modis_total"]
    near = client.get(url, params={"hours": 24 * 400, "lat": ev["latitude"], "lon": ev["longitude"], "radius_km": 5}).json()
    assert 0 < near["viirs_total"] + near["modis_total"] < total  # a small radius keeps only the fire's surroundings
    empty = client.get(url, params={"hours": 24 * 400, "lat": -44.5, "lon": -67.0, "radius_km": 5}).json()  # Patagonian steppe
    assert empty["viirs_total"] + empty["modis_total"] == 0


def test_point_tiles_and_bad_tiles(client):
    ev = client.get("/api/events", params={"sort": "area", "limit": 1}).json()[0]
    size = 180 / 2**7
    x, y = int((ev["longitude"] + 180) // size), int((ev["latitude"] + 90) // size)
    t = client.get(f"/api/tiles/points/7/{x}/{y}", params={"hours": 24 * 400}).json()
    assert t["kind"] == "points" and t["points"]["count"] >= ev["detection_count"]
    assert ev["id"] in {e["id"] for e in t["events"]}
    assert all(set(e) >= {"id", "viirs_count", "modis_count"} for e in t["events"])
    assert client.get("/api/tiles/points/5/0/0").status_code == 422
    assert client.get("/api/tiles/cells/9/0/0").status_code == 422
    assert client.get("/api/tiles/cells/2/8/0").status_code == 404


def test_reingest_without_new_data_keeps_tiles_valid(client):
    v = client.get("/api/tiles/meta").json()["data_version"]
    client.post("/api/ingest", json={})  # demo data is idempotent: nothing new
    assert client.get("/api/tiles/meta").json()["data_version"] == v


def test_export_sensor_csv(client):
    r = client.get("/api/export/viirs.csv", params={"start": "2000-01-01T00:00:00Z", "end": "2100-01-01T00:00:00Z"})
    assert r.status_code == 422  # over 400 days
    now = datetime.now(UTC)
    params = {"start": (now - timedelta(days=30)).isoformat(), "end": now.isoformat(), "bbox": "-70,-40,-55,-20"}
    r = client.get("/api/export/modis.csv", params=params)
    lines = r.text.strip().splitlines()
    assert r.headers["content-type"].startswith("text/csv") and len(lines) > 10
    assert "brightness_t21_k" in lines[0] and "bright_ti4_k" not in lines[0]
