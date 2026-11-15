"""Features of the learned fire-spread model (shared by training in ml/train_spread.py and the API).

For one fire at one moment, every cell of a ~150 m grid around its front gets features that say
how likely new fire is to show up there in the next 24 h:

* distance: to the nearest front pixel, and to the nearest pixel the fire already burned;
* direction: how well the direction from the nearest front pixel lines up with the wind (scaled
  by its speed), with the fire's own recent movement (front centroid minus its centroid 12-36 h
  earlier) and with the uphill direction (scaled by slope);
* ground: land-cover class and the burnable share around the cell, slope;
* history: other fires' detections around the cell in the previous 30 days (where people burn);
* the day: wind, gusts, driest air, rain, temperature, the Fosberg fire-weather index computed
  from them, and the size of the front.

Only basic weather variables go in, so the same features come from Meteomatics (training) or
Open-Meteo (live forecasts).
"""

import math
from dataclasses import dataclass

import numpy as np
from scipy.ndimage import distance_transform_edt, uniform_filter

from app.services import geodata
from app.services.spread import Grid, make_grid

HALF_KM = 10.0  # 90 % of next-day new detections are within ~6 km of the front
LAND = ["tree_cover", "shrubland", "grassland", "cropland", "built_up", "bare", "water", "wetland"]
BURNABLE_CODES = [10, 20, 30, 40, 90, 95, 100]
FEATURES = (["d_front_km", "d_prior_km", "wind_align", "move_align", "uphill", "slope", "burnable_450m", "recurrence"]
            + [f"lc_{c}" for c in LAND]
            + ["wind_kmh", "gust_kmh", "rh_min", "rain_mm", "t_max", "fosberg_max", "front_n", "move_km"])


def fosberg(t_c: float, rh: float, wind_kmh: float) -> float:
    """Fosberg fire weather index (0-100) from temperature, relative humidity and wind."""
    t_f = t_c * 9 / 5 + 32
    h = min(max(rh, 0.0), 100.0)
    if h < 10:
        m = 0.03229 + 0.281073 * h - 0.000578 * h * t_f
    elif h < 50:
        m = 2.22749 + 0.160107 * h - 0.01478 * t_f
    else:
        m = 21.0606 + 0.005565 * h * h - 0.00035 * h * t_f - 0.483199 * h
    x = m / 30
    eta = 1 - 2 * x + 1.5 * x * x - 0.5 * x ** 3
    u_mph = wind_kmh / 1.609
    return float(min(max(eta * math.sqrt(1 + u_mph * u_mph) / 0.3002, 0.0), 100.0))


@dataclass
class Day:
    wind_kmh: float
    toward_deg: float
    gust_kmh: float
    rh_min: float
    rain_mm: float
    t_max: float
    fosberg_max: float


def day_weather(hourly: dict, t0_ts: float, hours: int = 24) -> Day:
    """Summarise the 24 h after t0 from an hourly block with Open-Meteo names."""
    from datetime import UTC, datetime

    times = [datetime.fromisoformat(t).replace(tzinfo=UTC).timestamp() for t in hourly["time"]]
    idx = [i for i, t in enumerate(times) if t0_ts <= t < t0_ts + hours * 3600] or list(range(min(hours, len(times))))
    g = lambda k, d=0.0: [hourly.get(k, [d] * len(times))[i] if hourly.get(k, [None] * len(times))[i] is not None else d for i in idx]  # noqa: E731
    spd, dirs, rh, tc = g("wind_speed_10m"), g("wind_direction_10m"), g("relative_humidity_2m", 50.0), g("temperature_2m", 20.0)
    u = sum(s * math.sin(math.radians((d + 180) % 360)) for s, d in zip(spd, dirs))
    v = sum(s * math.cos(math.radians((d + 180) % 360)) for s, d in zip(spd, dirs))
    gusts = g("wind_gusts_10m") if "wind_gusts_10m" in hourly else spd
    return Day(wind_kmh=float(np.mean(spd)), toward_deg=(math.degrees(math.atan2(u, v)) + 360) % 360, gust_kmh=float(max(gusts)),
               rh_min=float(min(rh)), rain_mm=float(sum(g("precipitation"))), t_max=float(max(tc)),
               fosberg_max=max(fosberg(t, h, w) for t, h, w in zip(tc, rh, spd)))


@dataclass
class Layers:
    grid: Grid
    landcover: np.ndarray
    slope: np.ndarray
    downhill: np.ndarray
    coverage: float


def layers_for(lat: float, lon: float, budget_s: float = 60.0) -> Layers:
    g = make_grid(lat, lon, HALF_KM)
    w, s, e, n = g.bbox()
    lc, c1, lats, _ = geodata.worldcover().window(w, s, e, n, g.step, budget_s)
    el, c2, _, _ = geodata.dem_coarse().window(w, s, e, n, g.step, budget_s)
    lc, el = lc[: g.rows, : g.cols], el[: g.rows, : g.cols]
    slope, downhill = geodata.terrain(el, lats[: g.rows], g.step)
    return Layers(g, lc, slope, downhill, min(c1, c2))


def _cells(g: Grid, pts: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    if not len(pts):
        return np.zeros(0, int), np.zeros(0, int)
    r, c = g.cell(pts[:, 0], pts[:, 1])
    ok = (r >= 0) & (r < g.rows) & (c >= 0) & (c < g.cols)
    return r[ok], c[ok]


def features(L: Layers, front: np.ndarray, history: np.ndarray, prior: np.ndarray, others: np.ndarray, day: Day) -> np.ndarray:
    """(rows*cols, len(FEATURES)) feature matrix. Points are (lat, lon) arrays; `others` are other
    fires' detections of the previous 30 days near the grid."""
    g = L.grid
    shape = (g.rows, g.cols)
    sampling = (g.dy_m / 1000, g.dx_m / 1000)
    fr, fc = _cells(g, front)
    mask = np.ones(shape, bool)
    mask[fr, fc] = False
    d_front, (ir, ic) = distance_transform_edt(mask, sampling=sampling, return_indices=True)
    rr, cc = np.indices(shape)
    dy_km = (ir - rr) * sampling[0]  # cell -> nearest front points north when positive (rows run south)
    dx_km = (cc - ic) * sampling[1]
    bearing = np.degrees(np.arctan2(dx_km, dy_km))  # from the front out to the cell, deg from north
    norm = np.hypot(dx_km, dy_km) > 0

    def align(deg: float) -> np.ndarray:
        return np.where(norm, np.cos(np.radians(bearing - deg)), 0.0)

    pr, pc = _cells(g, prior)
    pm = np.ones(shape, bool)
    pm[pr, pc] = False
    d_prior = distance_transform_edt(pm, sampling=sampling) if len(pr) else np.full(shape, 50.0)

    move_km, move_deg = 0.0, 0.0
    if len(history):
        mlat = front[:, 0].mean() - history[:, 0].mean()
        mlon = (front[:, 1].mean() - history[:, 1].mean()) * math.cos(math.radians(front[:, 0].mean()))
        move_km = math.hypot(mlat, mlon) * 111.2
        move_deg = math.degrees(math.atan2(mlon, mlat))
    uphill = -L.slope * np.cos(np.radians(bearing - L.downhill)) * norm  # + where the cell lies uphill
    burnable = np.isin(L.landcover, BURNABLE_CODES).astype(float)
    orr, occ = _cells(g, others)
    hist2d = np.zeros(shape)
    np.add.at(hist2d, (orr, occ), 1.0)
    recur = uniform_filter(hist2d, size=7, mode="constant") * 49  # detections within ~0.5 km
    cols = [d_front, d_prior, align(day.toward_deg) * day.wind_kmh, align(move_deg) * move_km, uphill, L.slope,
            uniform_filter(burnable, size=3, mode="nearest"), recur]
    cols += [(L.landcover == code).astype(float) for code in (10, 20, 30, 40, 50, 60, 80, 90)]
    const = [day.wind_kmh, day.gust_kmh, day.rh_min, day.rain_mm, day.t_max, day.fosberg_max, float(len(front)), move_km]
    cols += [np.full(shape, v) for v in const]
    return np.stack([c.ravel() for c in cols], axis=1).astype(np.float32)


def target_cells(L: Layers, targets: np.ndarray) -> tuple[np.ndarray, float]:
    """Flat cell index of each target inside the grid (one per detection), and the share of
    targets inside the grid."""
    r, c = _cells(L.grid, targets)
    return r * L.grid.cols + c, (len(r) / len(targets)) if len(targets) else 0.0
