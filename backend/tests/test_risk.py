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


HOT_DRY_WINDY = {
    "time": ["2026-11-14"],
    "temperature_2m_max": [38],
    "relative_humidity_2m_min": [8],
    "wind_speed_10m_max": [10],
    "precipitation_sum": [0],
}


def test_bare_desert_has_no_fire_danger_even_in_extreme_weather():
    # Regression: the Atacama (real Sentinel-2 NDVI 0.05) used to score "very_high".
    desert = compute_risk(HOT_DRY_WINDY, date(2026, 11, 14), nearby_7d=0, ndvi=0.05)[0]
    assert desert.score == 0 and desert.level == "low"
    assert desert.components.fuel_load == 0


def test_cured_grass_scores_above_lush_forest_in_same_weather():
    grass = compute_risk(HOT_DRY_WINDY, date(2026, 11, 14), nearby_7d=0, ndvi=0.30)[0]
    forest = compute_risk(HOT_DRY_WINDY, date(2026, 11, 14), nearby_7d=0, ndvi=0.85)[0]
    assert grass.components.fuel_load == forest.components.fuel_load == 1
    assert grass.score > forest.score


def test_unknown_ndvi_falls_back_to_weather_only():
    day = compute_risk(HOT_DRY_WINDY, date(2026, 11, 14), nearby_7d=0, ndvi=None)[0]
    assert day.components.fuel is None and day.components.fuel_load is None and day.score > 0


def test_ndvi_evalscript_masks_water_and_clouds():
    from app.services.ndvi import EVALSCRIPT

    assert "[0, 1, 3, 6, 8, 9, 10, 11]" in EVALSCRIPT
