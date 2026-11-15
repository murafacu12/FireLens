"""Learned spread model features, the Fosberg index and the Meteomatics client (offline)."""

import math
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from app.services import meteomatics, spread_ml as sm
from app.services.spread import Grid


def test_fosberg_reference_values():
    assert sm.fosberg(20, 100, 0) == pytest.approx(0, abs=1)  # saturated air, calm: no fire weather
    calm_dry = sm.fosberg(35, 10, 0)
    windy_dry = sm.fosberg(35, 10, 40)
    assert 0 < calm_dry < 5 and windy_dry == pytest.approx(70.6, abs=1.0)  # the index is driven by wind
    assert sm.fosberg(25, 30, 20) > sm.fosberg(25, 70, 20)  # drier air, higher index


def test_day_weather_summary():
    t0 = datetime(2026, 9, 1, tzinfo=UTC)
    hourly = {"time": [(t0 + timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:%S") for h in range(30)],
              "wind_speed_10m": [10.0] * 30, "wind_direction_10m": [0.0] * 30, "wind_gusts_10m": [20.0] * 30,
              "relative_humidity_2m": [60.0] * 12 + [25.0] * 18, "precipitation": [0.0] * 30, "temperature_2m": [30.0] * 30}
    d = sm.day_weather(hourly, t0.timestamp())
    assert d.toward_deg == pytest.approx(180, abs=0.5)  # wind from the north blows toward the south
    assert d.rh_min == 25 and d.gust_kmh == 20 and d.fosberg_max == pytest.approx(sm.fosberg(30, 25, 10))


def _layers(n=81):
    step = 0.0015
    g = Grid(west=-60.06, north=-29.94, step=step, rows=n, cols=n, lat0=-30.0)
    lc = np.full((n, n), 30, np.uint8)
    lc[:, :10] = 80  # a lake along the west edge
    return sm.Layers(g, lc, np.zeros((n, n)), np.zeros((n, n)), 1.0)


def test_features_geometry():
    L = _layers()
    g = L.grid
    centre = np.array([[g.north - 40.5 * g.step, g.west + 40.5 * g.step]])
    day = sm.Day(wind_kmh=20, toward_deg=90, gust_kmh=30, rh_min=30, rain_mm=0, t_max=30, fosberg_max=20)
    history = centre + [0.0, -0.02]  # 12-36 h ago the fire was ~2 km further west: it is moving east
    X = sm.features(L, centre, history, history, np.zeros((0, 2)), day)
    assert X.shape == (g.rows * g.cols, len(sm.FEATURES))
    F = {name: X[:, i].reshape(g.rows, g.cols) for i, name in enumerate(sm.FEATURES)}
    assert F["d_front_km"][40, 40] == 0 and F["d_front_km"][40, 60] == pytest.approx(20 * g.dx_m / 1000, rel=0.01)
    assert F["wind_align"][40, 60] == pytest.approx(20, abs=0.5) and F["wind_align"][40, 20] == pytest.approx(-20, abs=0.5)
    assert F["move_align"][40, 60] > 0 > F["move_align"][40, 20]  # downstream of its own movement
    assert F["lc_water"][40, 5] == 1 and F["lc_grassland"][40, 40] == 1
    cells, inside = sm.target_cells(L, np.vstack([centre, [[-10.0, -10.0]]]))
    assert inside == 0.5 and len(cells) == 1


def test_meteomatics_parse_and_url():
    body = {"data": [
        {"parameter": "wind_speed_10m:kmh", "coordinates": [
            {"lat": 1, "lon": 2, "dates": [{"date": "2026-09-01T00:00:00Z", "value": 5.0}, {"date": "2026-09-01T01:00:00Z", "value": 6.0}]},
            {"lat": 3, "lon": 4, "dates": [{"date": "2026-09-01T00:00:00Z", "value": 7.0}, {"date": "2026-09-01T01:00:00Z", "value": 8.0}]}]},
    ]}
    out = meteomatics.Meteomatics._parse(body, [(1, 2), (3, 4)])
    assert out[1]["wind_speed_10m:kmh"] == [7.0, 8.0] and out[0]["time"] == ["2026-09-01T00:00:00", "2026-09-01T01:00:00"]
    mm = meteomatics.Meteomatics(None, "u", "p")
    url = mm._url(datetime(2026, 9, 1, tzinfo=UTC), datetime(2026, 9, 2, tzinfo=UTC), "PT1H", ["t_2m:C"], [(1.0, 2.0), (3.0, 4.0)])
    assert url.endswith("/2026-09-01T00:00:00Z--2026-09-02T00:00:00Z:PT1H/t_2m:C/1.0000,2.0000+3.0000,4.0000/json")
    assert "u" not in url.split("api.meteomatics.com")[1].split("/")[0]  # credentials never in the URL
