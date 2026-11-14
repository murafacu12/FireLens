"""Data-quality rules: angle-independent event area, non-vegetation sources, schema migration."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, inspect, select, text

from app.db import Base, make_engine, make_session_factory
from app.ingest.clustering import build_event, estimate_area_km2
from app.migrations import EVENTS_VERSION, upgrade
from app.models import AppMetaRow, DetectionRow, EventRow
from app.schemas import FireDetection
from app.services import queries
from app.services.store import Seed, flag_static_sources, rebuild_events, upsert_detections

T0 = datetime(2026, 7, 28, 4, 0, tzinfo=UTC)


def det(i, lat, lon, instrument="VIIRS", scan=None, track=None, hours=0.0, dtype=None, kind=None, level="NRT"):
    nominal = 0.375 if instrument == "VIIRS" else 1.0
    s, t = scan or nominal, track or nominal
    return FireDetection(
        id=f"d{i}", source="x", instrument=instrument, satellite="NOAA-20", processing_level=level,
        latitude=lat, longitude=lon, acquired_at=T0 + timedelta(hours=hours), frp_mw=10,
        scan_km=s, track_km=t, pixel_area_km2=s * t, nominal_resolution_m=int(nominal * 1000),
        confidence_raw="n", confidence_pct=55, confidence_level="nominal", detection_type=dtype, static_kind=kind,
    )


# --------------------------------------------------------------------- area

def test_area_does_not_depend_on_viewing_angle():
    nadir = estimate_area_km2([det(1, -31, -64, scan=0.39, track=0.36)], -31)
    edge = estimate_area_km2([det(1, -31, -64, scan=0.80, track=0.78)], -31)  # same fire, swath edge
    assert nadir == edge


def test_modis_only_area_is_one_km_per_pixel_and_labelled_coarse():
    # Regression: 5 off-nadir MODIS pixels (up to 9.6 km2 each) made a 28.7 km2 "fire".
    pixels = [det(i, -36.3 + i * 0.05, -65.4, instrument="MODIS", scan=4.6, track=2.0) for i in range(5)]
    area, basis = estimate_area_km2(pixels, -36.3)
    assert basis == "modis_1km"
    assert 4.5 <= area <= 5.5  # five separate ~1 km2 pixels, not 5 x 9.2 km2


def test_mixed_event_uses_viirs_basis():
    ev = build_event([det(1, -31, -64), det(2, -31.001, -64, instrument="MODIS")], now=T0)
    assert ev.area_basis == "viirs_375m" and ev.area_km2 < 0.3


# --------------------------------------------------------------------- static sources

def make_db(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'q.db'}")
    Base.metadata.create_all(engine)
    return engine, make_session_factory(engine)


def test_nrt_detections_on_archive_flagged_sites_are_not_fires(tmp_path):
    _, factory = make_db(tmp_path)
    flare = (-38.45, -68.55)  # a gas flare the archive labelled "other static land source"
    with factory() as s:
        upsert_detections(s, [
            det(1, *flare, dtype=2, kind="industrial", level="SP"),  # archive verdict
            det(2, flare[0] + 0.002, flare[1], hours=48),  # NRT, same flare two days later
            det(3, flare[0] + 0.003, flare[1] + 0.002, hours=72, dtype=0, level="SP"),  # archive says vegetation
            det(4, flare[0] + 0.3, flare[1], hours=48),  # NRT, a real fire 33 km away
        ])
        seeds = upsert_detections(s, [])  # nothing new
        changed = flag_static_sources(s)
        assert len(changed) == 1  # only the NRT flare detection changed
        kinds = dict(s.execute(select(DetectionRow.id, DetectionRow.static_kind)).all())
        assert kinds == {"d1": "industrial", "d2": "industrial", "d3": None, "d4": None}
        all_rows = [Seed(r.latitude, r.longitude, r.acquired_at, r.id) for r in s.scalars(select(DetectionRow))]
        rebuild_events(s, all_rows + seeds, 1.0, 24, 24)
        member_ids = {r.id for r in s.scalars(select(DetectionRow).where(DetectionRow.event_id.is_not(None)))}
        assert member_ids == {"d3", "d4"}  # flares never become fire events
        visible = queries.query_detections(s, queries.DetectionFilter(limit=100))
        assert {d.id for d in visible} == {"d3", "d4"}
        everything = queries.query_detections(s, queries.DetectionFilter(limit=100, include_static=True))
        assert len(everything) == 4


# --------------------------------------------------------------------- migration

def test_upgrade_adds_columns_and_rebuilds_events_from_old_database(tmp_path):
    engine, factory = make_db(tmp_path)
    with factory() as s:
        upsert_detections(s, [det(1, -31, -64), det(2, -31.001, -64, hours=1), det(3, -40, -70, dtype=3, kind="offshore", level="SP")])
        s.commit()
    with engine.begin() as conn:  # simulate a database created before these columns existed
        conn.execute(text("ALTER TABLE detections DROP COLUMN static_kind"))
        conn.execute(text("ALTER TABLE fire_events DROP COLUMN area_basis"))

    class S:  # the settings upgrade() reads
        cluster_tolerance_km, cluster_max_gap_hours, event_active_hours = 1.0, 24.0, 24.0

    upgrade(engine, factory, S())
    cols = {c["name"] for c in inspect(engine).get_columns("detections")}
    assert "static_kind" in cols
    with factory() as s:
        assert s.get(AppMetaRow, "events_version").value == EVENTS_VERSION
        assert s.scalar(select(DetectionRow.static_kind).where(DetectionRow.id == "d3")) == "offshore"
        events = s.scalars(select(EventRow)).all()
        assert len(events) == 1 and events[0].detection_count == 2 and events[0].area_basis == "viirs_375m"
    upgrade(engine, factory, S())  # idempotent: nothing to do the second time


# --------------------------------------------------------------------- incremental updates (worldwide scale)

def test_reingesting_the_same_data_changes_nothing(tmp_path):
    _, factory = make_db(tmp_path)
    batch = [det(1, -31, -64), det(2, 45, 10, hours=2)]
    with factory() as s:
        assert len(upsert_detections(s, batch)) == 2
        assert upsert_detections(s, batch) == []  # hourly refresh repeating stored rows: no work
        # the archive (SP) version of a stored NRT pixel is a real change; its NRT copy is not
        sp = det(1, -31, -64, level="SP")
        assert [round(x.lat) for x in upsert_detections(s, [sp])] == [-31]
        assert upsert_detections(s, [det(1, -31, -64)]) == []  # NRT never overwrites SP
        assert s.scalar(select(DetectionRow.processing_level).where(DetectionRow.id == "d1")) == "SP"


def test_rebuild_only_touches_the_region_that_changed(tmp_path):
    _, factory = make_db(tmp_path)
    with factory() as s:
        seeds = upsert_detections(s, [det(1, -31, -64), det(2, 45, 10)])  # Argentina and Italy
        assert len(rebuild_events(s, seeds, 1.0, 24, 24)) == 2
        italy_before = s.scalar(select(EventRow.id).where(EventRow.latitude > 0))
        # a new detection next to the Argentine fire, 2 h later
        new = upsert_detections(s, [det(3, -31.002, -64, hours=2)])
        rebuilt = rebuild_events(s, new, 1.0, 24, 24)
        assert [e.detection_count for e in rebuilt] == [2]  # only the Argentine event was rebuilt
        assert s.scalar(select(EventRow.id).where(EventRow.latitude > 0)) == italy_before
        assert s.scalar(select(func.count()).select_from(EventRow)) == 2


def test_new_detection_joining_two_fires_merges_them(tmp_path):
    _, factory = make_db(tmp_path)
    with factory() as s:
        # two fires 3 km apart (too far to link), then a detection between them, 6 h later
        rebuild_events(s, upsert_detections(s, [det(1, -31.0, -64.0), det(2, -31.027, -64.0)]), 1.0, 24, 24)
        assert s.scalar(select(func.count()).select_from(EventRow)) == 2
        rebuilt = rebuild_events(s, upsert_detections(s, [det(3, -31.0135, -64.0, hours=6)]), 1.0, 24, 24)
        assert [e.detection_count for e in rebuilt] == [3]
        assert s.scalar(select(func.count()).select_from(EventRow)) == 1


def test_event_crossing_a_tile_edge_is_rebuilt_whole(tmp_path):
    _, factory = make_db(tmp_path)
    with factory() as s:
        # a fire front crossing the -31 degree line (two 1-degree tiles)
        chain = [det(i, -30.99 - i * 0.004, -64, hours=i) for i in range(6)]
        rebuild_events(s, upsert_detections(s, chain), 1.0, 24, 24)
        assert s.scalar(select(func.count()).select_from(EventRow)) == 1
        # new detection at the southern end only
        new = upsert_detections(s, [det(9, -31.02, -64, hours=7)])
        rebuilt = rebuild_events(s, new, 1.0, 24, 24)
        assert [e.detection_count for e in rebuilt] == [7]  # whole fire, not just the tile that changed
        assert s.scalar(select(func.count()).select_from(EventRow)) == 1
