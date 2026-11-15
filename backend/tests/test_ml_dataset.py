"""Fire-danger training rows: no information from after the issue day, no leakage from the held-out year."""

import json
import math
from datetime import timedelta

import numpy as np
import pytest

pytest.importorskip("numpy")

from app.db import Base, make_engine, make_session_factory  # noqa: E402
from app.services.store import upsert_detections  # noqa: E402
from benchmarks import scenarios as sc  # noqa: E402
from ml import dataset, download_firms  # noqa: E402

LAT, LON = -31.2, -64.2
FIRE_DAYS = set(range(40, 1096, 9))  # NOAA-20 fire every 9 days in one cell


@pytest.fixture
def synthetic(tmp_path, monkeypatch):
    monkeypatch.setattr(download_firms, "DATA_DIR", tmp_path)
    engine = make_engine(f"sqlite:///{(tmp_path / 'arg.db').as_posix()}")
    Base.metadata.create_all(engine)
    b = sc._Builder("ml", 1)
    for di in sorted(FIRE_DAYS):
        hours = (download_firms.START - sc.T0.date()).days * 24 + di * 24 + 15
        b.pixel("F", LAT, LON, hours, "NOAA-20")
    for d in b.dets:  # keep every pixel inside the one 0.5 deg cell
        object.__setattr__(d, "latitude", LAT)
        object.__setattr__(d, "longitude", LON)
    with make_session_factory(engine)() as s:
        upsert_detections(s, b.dets)
        s.commit()
    days = dataset._days()
    wx = tmp_path / "weather" / "arg"
    wx.mkdir(parents=True)
    n = len(days)
    (wx / "-31.5_-64.5.json").write_text(json.dumps({"lat": -31.5, "lon": -64.5, "daily": {
        "time": [d.isoformat() for d in days], "temperature_2m_max": [30.0] * n, "relative_humidity_2m_min": [20.0] * n,
        "wind_speed_10m_max": [20.0] * n, "precipitation_sum": [0.0] * n, "vapour_pressure_deficit_max": [3.0] * n,
        "et0_fao_evapotranspiration": [5.0] * n}}))
    return dataset.build()


def test_activity_features_only_see_the_issue_day_and_before(synthetic):
    ds = synthetic
    X, y, meta = ds["X"], ds["y"], ds["meta"]
    f = ds["features"]
    assert ds["cells"] == 1 and len(y) > 900
    for row, (di, _, _, _) in enumerate(meta):
        lead = int(X[row, f.index("lead_days")])
        issue = di - lead
        assert bool(y[row]) == (di in FIRE_DAYS)  # label = fire on the target day
        assert X[row, f.index("fire_issue_day")] == pytest.approx(math.log1p(int(issue in FIRE_DAYS)))
        expected_7d = sum(1 for d in range(issue - 6, issue + 1) if d in FIRE_DAYS)
        assert X[row, f.index("fire_7d")] == pytest.approx(math.log1p(expected_7d))
        last = max(d for d in FIRE_DAYS | {-10_000} if d <= issue)
        assert X[row, f.index("days_since_fire")] == min(issue - last, 365)


def test_climatology_never_uses_the_rows_own_year(synthetic):
    ds = synthetic
    clim, meta = ds["climatology"], ds["meta"]
    X, f = ds["X"], ds["features"]
    months = np.array([int(ds["days"][di][5:7]) for di in meta[:, 0]])
    for row in range(0, len(meta), 37):
        k, m = meta[row, 3], months[row]
        expected = clim[1 - k, 0, 0, m] if k < dataset.TEST_YEAR else clim[:dataset.TEST_YEAR, 0, 0, m].mean()
        assert X[row, f.index("climatology")] == pytest.approx(expected, rel=1e-5)
