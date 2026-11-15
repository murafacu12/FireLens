"""Ground data, land-cover breakdown, the fire report's ground part and the spread model, offline
(fake tiles instead of the public rasters)."""

import math
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from app.services import geodata, sentinel, spread
from app.services.geodata import Tile, TileSource


class FakeSource(TileSource):
    """Tiles made in memory: `value(lat, lon)` gives each pixel's value."""

    def __init__(self, tmp_path, name, tile_deg, px, value, fill=0):
        self.name, self.tile_deg, self.fill = name, tile_deg, fill
        super().__init__(tmp_path)
        self._px, self._value = px, value
        self.pixel_deg = tile_deg / px

    def read_remote(self, key):
        lat0, lon0 = key
        step = self.tile_deg / self._px
        lats = lat0 + self.tile_deg - (np.arange(self._px) + 0.5) * step
        lons = lon0 + (np.arange(self._px) + 0.5) * step
        la, lo = np.meshgrid(lats, lons, indexing="ij")
        return Tile(self._value(la, lo), float(lon0), float(lat0 + self.tile_deg), step, step)


@pytest.fixture
def fake_ground(tmp_path, monkeypatch):
    # land cover: grass west of -60.0, forest east of it, a lake (water) around (-30.05, -59.9)
    def lc(la, lo):
        v = np.where(lo < -60.0, 30, 10).astype(np.uint8)
        v[np.hypot(la + 30.05, lo + 59.9) < 0.01] = 80
        return v
    wc = FakeSource(tmp_path, "worldcover", 3, 2250, lc, fill=80)
    # elevation: a ridge rising to the north (10 m per km)
    dem = FakeSource(tmp_path, "dem90", 1, 1200, lambda la, lo: ((la + 31) * 111.2 * 10).astype(np.int16))
    monkeypatch.setattr(geodata, "worldcover", lambda cache_dir=None: wc)
    monkeypatch.setattr(geodata, "dem", lambda cache_dir=None: dem)
    return wc, dem


def test_tiles_cached_and_sampled(fake_ground, tmp_path):
    wc, _ = fake_ground
    codes, ok = wc.sample(np.array([-30.0, -30.0, -30.05]), np.array([-60.5, -59.5, -59.9]))
    assert ok.all() and codes.tolist() == [30, 10, 80]
    assert {p.name for p in (tmp_path / "worldcover").glob("*.npz")} == {"-30_-63.npz", "-30_-60.npz", "-33_-60.npz"}  # 3 deg tiles, saved once
    wc._mem.clear()
    assert wc.cached((-30, -63)) is not False  # read back from disk after the memory cache is gone


def test_terrain_slope_and_aspect():
    lats = np.linspace(-30.0, -30.1, 50)  # rows north -> south
    elev = np.tile(((lats + 31) * 111.2 * 100)[:, None], (1, 50))  # 100 m per km rising north
    slope, downhill = geodata.terrain(elev, lats, 0.1 / 49)
    assert slope.mean() == pytest.approx(math.degrees(math.atan(0.1)), abs=0.3)
    assert np.median(downhill) == pytest.approx(180, abs=1)  # it faces (falls toward) the south


def test_landcover_shares():
    shares = geodata.landcover_shares(np.array([10, 10, 30, 80]))
    assert list(shares) == ["tree_cover", "grassland", "water"] and shares["tree_cover"] == 0.5


def steady(wind=20.0, toward=90.0, rh=25.0):
    return [spread.Weather(wind, toward, rh, 0.0)] * 4


def test_spread_runs_downwind_and_is_stopped_by_water(fake_ground):
    g = spread.make_grid(-30.0, -59.95, half_km=8)
    base, slope, downhill, cov = spread.ground_layers(g)
    assert cov == 1.0
    flat = np.zeros_like(slope)
    front = g.cell(np.array([-30.0]), np.array([-59.95]))
    t = spread.arrival_hours(g, base, flat, flat, front, steady(toward=90.0), scale=2.0)
    r0, c0 = front[0][0], front[1][0]
    east, west = t[r0, c0 + 8], t[r0, c0 - 8]
    assert np.isfinite(east) and (not np.isfinite(west) or west > 2 * east)  # wind blows toward the east
    lake = g.cell(np.array([-30.05]), np.array([-59.9]))
    assert not np.isfinite(t[lake[0][0], lake[1][0]])  # water never burns
    assert t[r0, c0] == 0


def test_spread_faster_uphill(fake_ground):
    g = spread.make_grid(-30.0, -59.95, half_km=6)
    base, slope, downhill, _ = spread.ground_layers(g)
    front = g.cell(np.array([-30.0]), np.array([-59.95]))
    t = spread.arrival_hours(g, base, slope, downhill, front, steady(wind=0.0), scale=3.0)
    r0, c0 = front[0][0], front[1][0]
    north, south = t[r0 - 4, c0], t[r0 + 4, c0]  # the ground rises to the north
    assert np.isfinite(north) and (not np.isfinite(south) or north < south)


def test_ellipse_and_weather_steps():
    assert spread.length_to_breadth(0) == pytest.approx(1.0)
    assert 2.0 < spread.length_to_breadth(30) < spread.length_to_breadth(60)
    t0 = datetime(2026, 9, 1, tzinfo=UTC)
    hourly = {"time": [(t0 + timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M") for h in range(25)],
              "wind_speed_10m": [10.0] * 25, "wind_direction_10m": [270.0] * 25,  # from the west
              "relative_humidity_2m": [40.0] * 25, "precipitation": [0.0] * 25}
    steps = spread.steps_from_hourly(hourly, t0)
    assert len(steps) == 4 and steps[0].toward_deg == pytest.approx(90, abs=0.5) and steps[0].rh_min == 40


def test_fire_endpoints(fake_ground, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app.config import Settings
    from app.main import create_app
    from app.services import fire_report

    monkeypatch.setattr(fire_report, "default_path", lambda: tmp_path / "enrichment.db")
    settings = Settings(_env_file=None, database_url=f"sqlite:///{tmp_path / 'a.db'}", demo_mode=True, demo_days=3,
                        copernicus_client_id=None, copernicus_client_secret=None)
    with TestClient(create_app(settings)) as c:
        ev = c.get("/api/events", params={"limit": 1}).json()[0]
        g = c.get(f"/api/events/{ev['id']}/ground").json()
        assert g["event_id"] == ev["id"] and g["landcover_coverage"] == 1.0 and g["terrain"]["coverage"] == 1.0
        assert abs(sum(s["share"] for s in g["around"]) - 1) < 1e-3
        assert c.get(f"/api/events/{ev['id']}/satellites").status_code == 503  # Copernicus not configured
        lc = c.get("/api/analysis/landcover", params={"hours": 24 * 3}).json()
        assert lc["coverage"] == 1.0 and sum(x["detections"] for x in lc["classes"]) == lc["detections"]


def test_anomaly_and_box():
    a = sentinel._anomaly([("2026-08-01", 1.0), ("2026-08-02", 3.0), ("2026-08-05", 6.0)], datetime(2026, 8, 5, tzinfo=UTC))
    assert a == {"available": True, "during": 6.0, "before": 2.0, "change": 4.0, "peak": 6.0, "peak_day": "2026-08-05",
                 "days_during": 1, "days_before": 2}
    assert not sentinel._anomaly([("2026-08-01", 1.0)], datetime(2026, 8, 5, tzinfo=UTC))["available"]
    b = sentinel.box_around(-30.0, -60.0, 5.0)
    assert sentinel.area_km2(b) == pytest.approx(100, rel=0.01)
