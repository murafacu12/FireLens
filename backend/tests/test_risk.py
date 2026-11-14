from datetime import date

import pytest

from app.services.risk import activity_trend, compute_risk, risk_level, vpd_hpa


def test_vpd():
    assert vpd_hpa(30, 100) == pytest.approx(0)
    assert vpd_hpa(30, 20) == pytest.approx(42.43 * 0.8, rel=0.01)


def test_hot_dry_windy_day_scores_higher_than_mild_wet_day():
    daily = {
        "time": ["2026-11-12", "2026-11-13", "2026-11-14", "2026-11-15"],
        "temperature_2m_max": [25, 25, 38, 18],
        "relative_humidity_2m_min": [40, 40, 12, 80],
        "wind_speed_10m_max": [3, 3, 9, 2],
        "precipitation_sum": [0, 0, 0, 12],
    }
    days = compute_risk(daily, date(2026, 11, 14), nearby_7d=10, ndvi=None)
    assert [d.date for d in days] == ["2026-11-14", "2026-11-15"]  # past days only feed dryness
    hot, wet = days
    assert hot.dry_days == 3 and wet.dry_days == 0
    assert hot.score > wet.score
    assert hot.components.fuel is None


def test_levels_and_trend():
    assert risk_level(5) == "low" and risk_level(99) == "extreme"
    assert activity_trend(30, 5) == "increasing"
    assert activity_trend(5, 30) == "decreasing"
    assert activity_trend(0, 0) == "none"
