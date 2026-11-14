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


def test_ndvi_evalscript_separates_water_from_land_and_masks_clouds():
    from app.services.ndvi import EVALSCRIPT

    assert "[0, 1, 3, 8, 9, 10, 11]" in EVALSCRIPT  # clouds, shadow, snow, bad pixels
    assert "water ? NaN : ndvi" in EVALSCRIPT  # land-only NDVI
    assert 'id: "water"' in EVALSCRIPT  # water share as its own statistic


def test_water_and_open_ocean_have_no_fire_danger():
    for surface in ("water", "open_ocean"):
        day = compute_risk(HOT_DRY_WINDY, date(2026, 11, 14), nearby_7d=0, ndvi=None, surface=surface)[0]
        assert day.score == 0 and day.components.fuel_load == 0
    coast = compute_risk(HOT_DRY_WINDY, date(2026, 11, 14), nearby_7d=0, ndvi=0.3, surface="land")[0]
    assert coast.score > 0


def _stats(mean, samples=100, nodata=0):
    return {"bands": {"B0": {"stats": {"mean": mean, "sampleCount": samples, "noDataCount": nodata}}}}


def _copernicus(stats_body):
    import httpx

    def handler(request):
        if "openid-connect/token" in str(request.url):
            return httpx.Response(200, json={"access_token": "t", "expires_in": 600})
        return httpx.Response(200, json=stats_body)

    return httpx.MockTransport(handler)


def _query(stats_body):
    import asyncio

    import httpx

    from app.services.ndvi import CopernicusNdvi

    async def run():
        async with httpx.AsyncClient(transport=_copernicus(stats_body)) as http:
            return await CopernicusNdvi(http, "id", "secret").mean_ndvi(-35.0, -50.0)

    return asyncio.run(run())


def test_ndvi_surface_classification_from_statistics():
    # open Atlantic: Sentinel-2 never images it -> no intervals at all
    ocean = _query({"data": [], "status": "OK"})
    assert ocean.surface == "open_ocean" and ocean.ndvi_mean is None
    # a lake: every cloud-free pixel is water, land NDVI is NaN
    interval = {"from": "2026-11-09T00:00:00Z", "to": "2026-11-14T00:00:00Z"}
    lake = _query({"data": [{"interval": interval, "outputs": {"ndvi": _stats("NaN"), "water": _stats(0.97)}}]})
    assert lake.surface == "water" and lake.ndvi_mean is None and lake.water_fraction == 0.97
    # riverbank: mostly water but the land part has real vegetation
    bank = _query({"data": [{"interval": interval, "outputs": {"ndvi": _stats(0.556), "water": _stats(0.81)}}]})
    assert bank.surface == "land" and bank.ndvi_mean == 0.556
    # a month of clouds only: unknown, not "water"
    cloudy = _query({"data": [{"interval": interval, "outputs": {"ndvi": _stats(None, nodata=100), "water": _stats(None, nodata=100)}}]})
    assert cloudy.surface == "unknown"
