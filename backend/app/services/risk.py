"""Baseline fire-danger forecast for a point.

This is a transparent heuristic, NOT a validated model. It combines:
  * Hot-Dry-Windy Index (HDW = VPD[hPa] x wind[m/s], Srock et al. 2018) from Open-Meteo,
  * antecedent dryness (days since >= 1 mm of rain),
  * recent fire activity nearby (from FireLens' own harmonized detections),
  * optional fuel condition from Sentinel-2 NDVI.
It is meant as the baseline the ML model (roadmap phase 3) has to beat.
"""

import math
from datetime import date

import httpx
from pydantic import BaseModel

WEIGHTS = {"hdw": 0.45, "dryness": 0.30, "activity": 0.15, "fuel": 0.10}
CLASSES = [(20, "low"), (40, "moderate"), (60, "high"), (80, "very_high"), (101, "extreme")]


class RiskComponents(BaseModel):
    hdw: float
    dryness: float
    activity: float
    fuel: float | None  # dryness of the vegetation present (from NDVI)
    fuel_load: float | None = None  # is there vegetation to burn at all; multiplies the score


class RiskDay(BaseModel):
    date: str
    score: float
    level: str
    temperature_max_c: float | None
    humidity_min_pct: float | None
    wind_max_ms: float | None
    precipitation_mm: float | None
    hdw: float | None
    dry_days: int
    components: RiskComponents


class RiskForecast(BaseModel):
    latitude: float
    longitude: float
    model: str = "baseline-hdw-v2"
    disclaimer: str = "Heuristic baseline for exploration; not an official fire-danger rating."
    nearby_detections_7d: int
    nearby_detections_prev_7d: int
    activity_trend: str
    ndvi: float | None
    surface: str | None = None  # land | water | open_ocean | unknown (from Sentinel-2)
    water_fraction: float | None = None
    days: list[RiskDay]


def saturation_vapour_pressure_hpa(t_c: float) -> float:
    return 6.108 * math.exp(17.27 * t_c / (t_c + 237.3))


def vpd_hpa(t_c: float, rh_pct: float) -> float:
    return saturation_vapour_pressure_hpa(t_c) * (1 - max(0.0, min(100.0, rh_pct)) / 100)


def _clamp(x: float) -> float:
    return max(0.0, min(1.0, x))


def fuel_load(ndvi: float) -> float:
    """Is there enough vegetation to carry a fire? 0 for bare ground/rock/sand (NDVI <= 0.10),
    rising to 1 once there is continuous cover (NDVI >= 0.25). Gates the whole score."""
    return _clamp((ndvi - 0.10) / 0.15)


def fuel_dryness(ndvi: float) -> float:
    """How cured is the vegetation that exists? 1 for dry grass/shrub (NDVI <= 0.25),
    falling to 0 for lush green canopy (NDVI >= 0.75)."""
    return _clamp(1 - (ndvi - 0.25) / 0.5)


def risk_level(score: float) -> str:
    return next(name for limit, name in CLASSES if score < limit)


def activity_trend(last: int, prev: int) -> str:
    if last == prev == 0:
        return "none"
    if last > prev * 1.25 + 2:
        return "increasing"
    if last < prev * 0.75 - 2:
        return "decreasing"
    return "stable"


NO_FUEL_SURFACES = ("water", "open_ocean")


def compute_risk(daily: dict, today: date, nearby_7d: int, ndvi: float | None, surface: str | None = None) -> list[RiskDay]:
    """`daily` is Open-Meteo's daily block (includes past days for antecedent dryness)."""
    times = daily.get("time", [])
    tmax = daily.get("temperature_2m_max", [])
    rhmin = daily.get("relative_humidity_2m_min", [])
    wind = daily.get("wind_speed_10m_max", [])
    rain = daily.get("precipitation_sum", [])

    activity = _clamp(nearby_7d / 50)
    fuel = None if ndvi is None else fuel_dryness(ndvi)
    load = 1.0 if ndvi is None else fuel_load(ndvi)  # unknown fuel: don't gate (weather-only estimate)
    if surface in NO_FUEL_SURFACES:  # lakes, rivers, sea: nothing to burn whatever the weather
        fuel, load = None, 0.0
    weights = dict(WEIGHTS)
    if fuel is None:
        weights.pop("fuel")
    total_w = sum(weights.values())

    out: list[RiskDay] = []
    dry_days = 0
    for i, day in enumerate(times):
        p = rain[i] if i < len(rain) else None
        dry_days = 0 if (p or 0) >= 1.0 else dry_days + 1
        if date.fromisoformat(day) < today:
            continue
        t, rh, w = (tmax[i] if i < len(tmax) else None), (rhmin[i] if i < len(rhmin) else None), (wind[i] if i < len(wind) else None)
        hdw = vpd_hpa(t, rh) * w if None not in (t, rh, w) else None
        comps = {
            "hdw": _clamp((hdw or 0) / 250),
            "dryness": _clamp(dry_days / 14),
            "activity": activity,
            "fuel": fuel,
        }
        # No fuel, no fire: a hot, dry, windy desert is not fire danger.
        score = 100 * load * sum(comps[k] * wgt for k, wgt in weights.items()) / total_w
        comps["fuel_load"] = None if (ndvi is None and surface not in NO_FUEL_SURFACES) else load
        out.append(
            RiskDay(
                date=day,
                score=round(score, 1),
                level=risk_level(score),
                temperature_max_c=t,
                humidity_min_pct=rh,
                wind_max_ms=w,
                precipitation_mm=p,
                hdw=round(hdw, 1) if hdw is not None else None,
                dry_days=dry_days,
                components=RiskComponents(**{k: (round(v, 3) if v is not None else None) for k, v in comps.items()}),
            )
        )
    return out


async def fetch_weather(http: httpx.AsyncClient, url: str, lat: float, lon: float) -> dict:
    params = {
        "latitude": lat,
        "longitude": lon,
        "daily": "temperature_2m_max,relative_humidity_2m_min,wind_speed_10m_max,precipitation_sum",
        "wind_speed_unit": "ms",
        "timezone": "UTC",
        "past_days": 14,
        "forecast_days": 7,
    }
    resp = await http.get(url, params=params, timeout=20)
    resp.raise_for_status()
    return resp.json()["daily"]
