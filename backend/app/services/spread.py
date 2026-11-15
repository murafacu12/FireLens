"""Where a burning fire could spread in the next hours: a minimum-travel-time model on the ground.

The fire's latest front (its detections of the last 12 h) spreads over a ~150 m grid. Each step
between neighbouring cells takes distance / rate of spread, where the rate depends on:

* fuel: the WorldCover class (grass and crops spread fastest, forest slower; water, bare ground,
  snow and towns stop or nearly stop a fire) - BASE_ROS, m/min with no wind on flat ground;
* wind: Open-Meteo's hourly forecast (10 m). The head rate grows with wind speed, and the shape
  around it is the standard elliptical fire (length-to-breadth ratio of Alexander 1985, rate in
  any direction from the ellipse with the ignition at its rear focus);
* slope: uphill faster, about doubling every 10 deg (McArthur), downhill slower;
* dryness: low relative humidity speeds it, rain stops most of it.

The 24 h are run in 6 h steps, each with its own wind and humidity. The result is the hour at
which each cell could be reached (or never), from which the 6/12/24 h zones are drawn.

What it is NOT: an operational fire-behaviour simulation. Fuel comes from a 2021 land-cover map
(not fuel load or curing), wind from a forecast at 10 m (not the fire's own winds), and the
spread rates are generic. `SCALE` is calibrated on past FireLens fires, and the model is only
shown because, in the backtest (ml/backtest_spread.py, docs/FIRE_SPREAD.md), its zones capture
more of the next day's new detections than a zone of the same area that ignores wind, terrain
and fuel. Read it as "where to look", with the measured hit rate attached.
"""

import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx
import numpy as np
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import dijkstra

from app.services import geodata

# Rate of spread with no wind, flat ground, ~30 % relative humidity (m/min), per WorldCover class.
# Generic values in the range of the classic fuel models (grass ~1, shrub ~0.6, timber litter
# ~0.3); only their ratios matter after calibration (SCALE).
BASE_ROS = {
    "grassland": 1.2, "cropland": 0.9, "shrubland": 0.6, "tree_cover": 0.3, "wetland": 0.3, "mangroves": 0.05,
    "moss_lichen": 0.2, "built_up": 0.05, "bare": 0.02, "snow_ice": 0.0, "water": 0.0, "no_data": 0.0,
}
SCALE = 1.0  # set from the backtest (see docs/FIRE_SPREAD.md)
# The backtest (Nov 2026, 300 fires) found these zones no better than a circle of the same area.
# Enabled at the user's request as EXPERIMENTAL: every outlook carries these numbers and the label.
ENABLED = True
EXPERIMENTAL = True
# the backtest's measured hit rates (ml/backtest_spread.py, 15 Nov 2026), shown next to every outlook:
# share of the next day's new detections inside the zone, and for a circle of the same area
BACKTEST: dict | None = {"fires": 300, "capture_24h": 0.414, "circle_capture_24h": 0.445, "median_area_km2": 12.8,
                         "passed": False}
STEP_H = 6
HOURS = 24
HALF_SIZE_KM = 20.0
FRONT_HOURS = 12
NOT_REACHED = 255

# 16 neighbours: the 8 around plus the 8 "knight moves", so spread is not forced onto 8 directions
_MOVES = [(dy, dx) for dy in range(-2, 3) for dx in range(-2, 3)
          if (dy, dx) != (0, 0) and (max(abs(dy), abs(dx)) == 1 or abs(dy) + abs(dx) == 3)]


@dataclass
class Weather:
    """One step: wind speed (km/h at 10 m), the direction it blows TOWARD (deg from north),
    minimum relative humidity (%) and rain (mm)."""
    wind_kmh: float
    toward_deg: float
    rh_min: float
    rain_mm: float


def steps_from_hourly(hourly: dict, t0: datetime, hours: int = HOURS, step_h: int = STEP_H) -> list[Weather]:
    """Open-Meteo hourly block -> one Weather per step, from t0 (wind averaged as vectors)."""
    times = [datetime.fromisoformat(t).replace(tzinfo=UTC) for t in hourly["time"]]
    out = []
    for k in range(hours // step_h):
        a, b = t0 + timedelta(hours=k * step_h), t0 + timedelta(hours=(k + 1) * step_h)
        idx = [i for i, t in enumerate(times) if a <= t < b] or [min(range(len(times)), key=lambda i: abs(times[i] - a))]
        u = v = 0.0
        for i in idx:
            spd = hourly["wind_speed_10m"][i] or 0.0
            toward = math.radians(((hourly["wind_direction_10m"][i] or 0.0) + 180) % 360)  # meteorological "from" -> toward
            u += spd * math.sin(toward)
            v += spd * math.cos(toward)
        n = len(idx)
        speeds = [hourly["wind_speed_10m"][i] or 0.0 for i in idx]
        out.append(Weather(
            wind_kmh=sum(speeds) / n,  # mean speed (vector mean would understate a turning wind)
            toward_deg=(math.degrees(math.atan2(u, v)) + 360) % 360,
            rh_min=min(hourly["relative_humidity_2m"][i] if hourly["relative_humidity_2m"][i] is not None else 50 for i in idx),
            rain_mm=sum(hourly["precipitation"][i] or 0.0 for i in idx),
        ))
    return out


async def fetch_hourly(http: httpx.AsyncClient, url: str, lat: float, lon: float, start: datetime, end: datetime) -> dict:
    params = {"latitude": round(lat, 3), "longitude": round(lon, 3), "timezone": "UTC",
              "hourly": "wind_speed_10m,wind_direction_10m,relative_humidity_2m,precipitation",
              "start_hour": start.strftime("%Y-%m-%dT%H:00"), "end_hour": end.strftime("%Y-%m-%dT%H:00")}
    r = await http.get(url, params=params, timeout=30)
    r.raise_for_status()
    return r.json()["hourly"]


def length_to_breadth(wind_kmh: float) -> float:
    """Alexander (1985): elliptical fire shape from the 10 m wind speed (km/h)."""
    return 1.0 + 8.729 * (1.0 - math.exp(-0.030 * wind_kmh)) ** 2.155


def head_wind_factor(wind_kmh: float) -> float:
    """How much faster the head runs than with no wind (grows a little faster than linearly)."""
    return 1.0 + 0.15 * max(wind_kmh, 0.0) ** 1.2


def dryness_factor(w: Weather) -> float:
    f = math.exp(-0.03 * (w.rh_min - 30.0))
    if w.rain_mm >= 2.0:
        f *= 0.2
    return min(max(f, 0.2), 2.0)


@dataclass
class Grid:
    west: float
    north: float
    step: float  # deg (same both ways; cells are narrower east-west away from the equator)
    rows: int
    cols: int
    lat0: float

    @property
    def dx_m(self) -> float:
        return self.step * 111_320.0 * max(math.cos(math.radians(self.lat0)), 0.05)

    @property
    def dy_m(self) -> float:
        return self.step * 110_574.0

    def cell(self, lat: np.ndarray, lon: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        return ((self.north - lat) / self.step).astype(int), ((lon - self.west) / self.step).astype(int)

    def bbox(self) -> list[float]:
        return [self.west, self.north - self.rows * self.step, self.west + self.cols * self.step, self.north]


def make_grid(lat: float, lon: float, half_km: float = HALF_SIZE_KM, step: float | None = None) -> Grid:
    step = step or geodata.worldcover().pixel_deg
    dlat = half_km / 110.574
    dlon = half_km / (111.320 * max(math.cos(math.radians(lat)), 0.05))
    n = int(math.ceil(2 * dlat / step))
    m = int(math.ceil(2 * dlon / step))
    return Grid(west=lon - dlon, north=lat + dlat, step=step, rows=n, cols=m, lat0=lat)


def ground_layers(g: Grid, budget_s: float = 30.0) -> tuple[np.ndarray, np.ndarray, np.ndarray, float]:
    """Base rate of spread (m/min), slope (deg) and the downhill direction (deg) per cell, and the
    share of the grid whose land-cover and elevation tiles were available."""
    w, s, e, n = g.bbox()
    lc, cov1, lats, _ = geodata.worldcover().window(w, s, e, n, g.step, budget_s)
    elev, cov2, _, _ = geodata.dem().window(w, s, e, n, g.step, budget_s)
    lc, elev = lc[: g.rows, : g.cols], elev[: g.rows, : g.cols]
    base = np.vectorize(lambda c: BASE_ROS.get(geodata.LANDCOVER.get(int(c), "no_data"), 0.0), otypes=[float])(lc)
    slope, downhill = geodata.terrain(elev, lats[: g.rows], g.step)
    return base, slope, downhill, min(cov1, cov2)


def _edges(g: Grid, base: np.ndarray, slope: np.ndarray, downhill: np.ndarray, w: Weather, scale: float):
    """Travel time (minutes) along every move between burnable neighbours, for one step's weather."""
    rows, cols = g.rows, g.cols
    idx = np.arange(rows * cols).reshape(rows, cols)
    lb = length_to_breadth(w.wind_kmh)
    ecc = math.sqrt(max(0.0, 1.0 - 1.0 / (lb * lb)))
    head = head_wind_factor(w.wind_kmh) * dryness_factor(w) * scale
    src, dst, cost = [], [], []
    for dy, dx in _MOVES:
        ys = slice(max(0, -dy), rows - max(0, dy))
        xs = slice(max(0, -dx), cols - max(0, dx))
        yd = slice(max(0, dy), rows - max(0, -dy))
        xd = slice(max(0, dx), cols - max(0, -dx))
        b = 0.5 * (base[ys, xs] + base[yd, xd])
        ok = (base[ys, xs] > 0) & (base[yd, xd] > 0)
        dist = math.hypot(dx * g.dx_m, dy * g.dy_m)
        bearing = math.degrees(math.atan2(dx * g.dx_m, -dy * g.dy_m)) % 360  # rows run south
        # wind: rate in this direction from the ellipse (ignition at the rear focus)
        theta = math.radians(bearing - w.toward_deg)
        wind_f = (1.0 - ecc) / (1.0 - ecc * math.cos(theta)) if ecc > 0 else 1.0
        # slope component along the move: + uphill (the downhill direction points the other way)
        s = 0.5 * (slope[ys, xs] + slope[yd, xd])
        along = -s * np.cos(np.radians(bearing - 0.5 * (downhill[ys, xs] + downhill[yd, xd])))
        slope_f = np.where(along >= 0, np.exp(0.069 * np.minimum(along, 40)), np.maximum(np.exp(0.035 * along), 0.5))
        ros = b * head * wind_f * slope_f  # m/min
        good = ok & (ros > 1e-6)
        src.append(idx[ys, xs][good])
        dst.append(idx[yd, xd][good])
        cost.append(dist / ros[good])
    return np.concatenate(src), np.concatenate(dst), np.concatenate(cost)


def arrival_hours(g: Grid, base, slope, downhill, front: tuple[np.ndarray, np.ndarray], weather: list[Weather],
                  scale: float = SCALE) -> np.ndarray:
    """Hour (0..len(weather)*STEP_H) at which each cell could be reached; inf where never."""
    n = g.rows * g.cols
    t = np.full(n, np.inf)
    fr, fc = front
    inside = (fr >= 0) & (fr < g.rows) & (fc >= 0) & (fc < g.cols)
    t[fr[inside] * g.cols + fc[inside]] = 0.0
    for k, w in enumerate(weather):
        t_end = (k + 1) * STEP_H * 60.0
        s, d, c = _edges(g, base, slope, downhill, w, scale)
        burning = np.flatnonzero(np.isfinite(t))
        if not len(burning):
            break
        # a super-source connected to every reached cell at its arrival time continues the spread
        S = n
        rows_ = np.concatenate([s, np.full(len(burning), S)])
        cols_ = np.concatenate([d, burning])
        vals = np.concatenate([c, t[burning] * 60.0 + 1e-9])
        graph = coo_matrix((vals, (rows_, cols_)), shape=(n + 1, n + 1)).tocsr()
        dist = dijkstra(graph, directed=True, indices=S, limit=t_end)[:n]
        new = np.isfinite(dist) & (dist <= t_end) & ~np.isfinite(t)
        t[new] = dist[new] / 60.0
    return t.reshape(g.rows, g.cols)


def zones(t: np.ndarray) -> np.ndarray:
    """Arrival hours for the map: uint8 per cell, NOT_REACHED where never."""
    return np.where(np.isfinite(t), np.clip(np.ceil(t), 0, 254), NOT_REACHED).astype(np.uint8)


def forecast(g: Grid, base, slope, downhill, front_lat: np.ndarray, front_lon: np.ndarray, hourly: dict,
             t0: datetime, scale: float = SCALE) -> dict:
    """The spread zones of one fire from now, for the map and the details panel."""
    import base64

    steps = steps_from_hourly(hourly, t0)
    fr = g.cell(front_lat, front_lon)
    t = arrival_hours(g, base, slope, downhill, fr, steps, scale)
    cell_km2 = g.dx_m * g.dy_m / 1e6
    reached = np.isfinite(t)
    # where the zone reaches farthest from the front: the direction of the head
    rr, cc = np.nonzero(reached & (t > 0))
    head = None
    if len(rr):
        r0, c0 = float(np.mean(fr[0])), float(np.mean(fr[1]))
        dy, dx = (r0 - rr) * g.dy_m, (cc - c0) * g.dx_m
        far = np.argmax(np.hypot(dx, dy))
        head = {"bearing_deg": round((math.degrees(math.atan2(dx[far], dy[far])) + 360) % 360),
                "distance_km": round(float(np.hypot(dx[far], dy[far])) / 1000, 1)}
    h = zones(t)
    return {
        "t0": t0.isoformat(),
        "grid": {"west": g.west, "north": g.north, "step": g.step, "rows": g.rows, "cols": g.cols,
                 "hours_b64": base64.b64encode(h.tobytes()).decode()},
        "area_km2": {f"{k}h": round(float(((t > 0) & (t <= k)).sum()) * cell_km2, 2) for k in (6, 12, 24)},
        "head": head,
        "weather": [{"from_h": i * STEP_H, "wind_kmh": round(w.wind_kmh), "toward_deg": round(w.toward_deg),
                     "rh_min": round(w.rh_min), "rain_mm": round(w.rain_mm, 1)} for i, w in enumerate(steps)],
        "scale": scale,
    }
