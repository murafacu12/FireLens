from types import SimpleNamespace

from app.services.perimeter import event_perimeter


def det(lat, lon, instrument="VIIRS"):
    return SimpleNamespace(latitude=lat, longitude=lon, instrument=instrument)


def grid(lat0, lon0, n, step_deg, instrument="VIIRS"):
    return [det(lat0 + i * step_deg, lon0 + j * step_deg, instrument) for i in range(n) for j in range(n)]


def test_too_few_pixels_gives_none():
    assert event_perimeter([det(-30, -60), det(-30.001, -60)], -30, -60) is None


def test_block_of_pixels_outline_and_area():
    # 6 x 6 VIIRS pixels ~375 m apart: a ~2.2 km square, about 5 km2 inside
    step = 0.375 / 111.0
    dets = grid(-30.0, -60.0, 6, step)
    p = event_perimeter(dets, -30.0, -60.0)
    assert p is not None
    assert p.basis == "viirs_375m"
    assert len(p.rings) == 1
    ring = p.rings[0]
    assert ring[0] == ring[-1]
    lons = [x for x, _ in ring]
    lats = [y for _, y in ring]
    # the outline encloses every detection
    assert min(lons) < -60.0 and max(lons) > -60.0 + 5 * step
    assert min(lats) < -30.0 and max(lats) > -30.0 + 5 * step
    assert 3.5 < p.area_km2 < 7.0


def test_small_gaps_close_and_far_parts_stay_apart():
    step = 0.375 / 111.0
    # two blocks one pixel apart merge; a block 20 km away stays a separate ring
    a = grid(-30.0, -60.0, 4, step)
    b = grid(-30.0, -60.0 + 5 * step, 4, step)
    far = grid(-30.0, -59.8, 4, step)
    p = event_perimeter(a + b + far, -30.0, -59.9)
    assert p is not None
    assert len(p.rings) == 2


def test_holes_are_filled():
    step = 0.375 / 111.0
    ring_only = [d for d in grid(-30.0, -60.0, 12, step)
                 if not (3 <= round((d.latitude + 30.0) / step) <= 8 and 3 <= round((d.longitude + 60.0) / step) <= 8)]
    p = event_perimeter(ring_only, -30.0, -60.0)
    assert p is not None
    full = event_perimeter(grid(-30.0, -60.0, 12, step), -30.0, -60.0)
    assert abs(p.area_km2 - full.area_km2) < 0.5


def test_viirs_preferred_over_modis():
    step = 0.375 / 111.0
    dets = grid(-30.0, -60.0, 4, step) + [det(-30.05, -60.05, "MODIS")]
    p = event_perimeter(dets, -30.0, -60.0)
    assert p.basis == "viirs_375m"
    assert min(y for _, y in p.rings[0]) > -30.03  # the lone MODIS pixel is not part of it


def test_antimeridian():
    step = 0.375 / 111.0
    dets = grid(10.0, 179.995, 4, step) + grid(10.0, -179.999, 4, step)
    p = event_perimeter(dets, 10.0, 180.0)
    assert p is not None
    assert all(-180 <= x <= 180 for r in p.rings for x, _ in r)
    assert p.area_km2 < 10
