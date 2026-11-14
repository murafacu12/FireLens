from datetime import UTC, datetime, timedelta

from app.ingest.clustering import build_event, cluster_detections, estimate_area_km2
from app.schemas import FireDetection

T0 = datetime(2026, 11, 10, 14, 0, tzinfo=UTC)


def det(i, lat, lon, instrument="VIIRS", hours=0.0, frp=10.0, scan=None, track=None):
    km = 0.375 if instrument == "VIIRS" else 1.0
    s, t = scan or km, track or km
    return FireDetection(
        id=f"d{i}", source="x", instrument=instrument, satellite="NOAA-20", processing_level="NRT",
        latitude=lat, longitude=lon, acquired_at=T0 + timedelta(hours=hours), frp_mw=frp,
        scan_km=s, track_km=t, pixel_area_km2=s * t, nominal_resolution_m=int(km * 1000),
        confidence_raw="n", confidence_pct=55, confidence_level="nominal",
    )


def test_adjacent_pixels_form_one_event_far_ones_do_not():
    dets = [det(1, -31.0, -64.0), det(2, -31.004, -64.0), det(3, -32.0, -64.0)]
    clusters = cluster_detections(dets)
    assert sorted(len(c) for c in clusters) == [1, 2]


def test_time_gap_splits_events():
    dets = [det(1, -31.0, -64.0), det(2, -31.0, -64.0, hours=30)]
    assert len(cluster_detections(dets, max_gap_hours=24)) == 2


def test_chain_is_transitive_across_days():
    dets = [det(i, -31.0 + i * 0.006, -64.0, hours=i * 12) for i in range(6)]
    assert len(cluster_detections(dets)) == 1


def test_modis_and_viirs_merge_and_area_uses_viirs():
    viirs = [det(i, -31.0 + i * 0.003, -64.0) for i in range(4)]
    modis = det(9, -31.004, -64.0, instrument="MODIS", scan=2.0, track=1.5, hours=1)
    clusters = cluster_detections([*viirs, modis])
    assert len(clusters) == 1
    ev = build_event(clusters[0], now=T0 + timedelta(hours=2))
    assert (ev.modis_count, ev.viirs_count) == (1, 4)
    # 4 VIIRS pixels ~ 4 * 0.14 km2; the MODIS 3 km2 pixel must not inflate it.
    assert 0.3 <= ev.area_km2 <= 1.0
    assert ev.status == "active"


def test_area_of_overlapping_pixels_not_double_counted():
    same = [det(1, -31.0, -64.0), det(2, -31.0, -64.0, hours=12)]
    single = [det(1, -31.0, -64.0)]
    assert estimate_area_km2(same, -31.0) == estimate_area_km2(single, -31.0)


def test_event_status_inactive_after_window():
    ev = build_event([det(1, 0, 0)], now=T0 + timedelta(hours=48), active_hours=24)
    assert ev.status == "inactive"
