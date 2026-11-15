"""Training rows for the fire-danger model: one per 0.5 deg cell x target day x lead time.

Question each row answers: will VIIRS NOAA-20 detect fire (nominal/high confidence, not a gas
flare/volcano) in this ~50 km cell on day d, asked L = 1..7 days ahead?

Features use only what is known at the issue day (d - L), except weather on the target day, which at
forecast time comes from a weather *forecast*; in training it comes from the archive ("perfect
prognosis": an optimistic stand-in, see docs). Activity features: NOAA-20 + MODIS detections.

Years: Y1 = 2023-11-15..2024-11-14, Y2 = 2024-11-15..2025-11-14 (training), Y3 = 2025-11-15..2026-11-14
(held out). The burnable domain (cells that had any fire) is defined from the training years only, and
cell climatology for a training row comes from the *other* training year, so nothing leaks from the
row's own label or from the held-out year.
"""

import json
import math
import sqlite3
from datetime import date, timedelta
from pathlib import Path

import numpy as np

from app.services.risk import WEIGHTS, vpd_hpa
from ml.download_firms import END, REGION, START, training_db
from ml.download_weather import CELL_DEG as WX_CELL, weather_dir

CELL = 0.5
MAX_LEAD = 7
HISTORY_DAYS = 30
YEAR_STARTS = [date(2023, 11, 15), date(2024, 11, 15), date(2025, 11, 15)]
TEST_YEAR = 2  # index into YEAR_STARTS
FEATURES = [
    "lead_days", "fire_issue_day", "fire_3d", "fire_7d", "fire_30d", "neighbours_7d", "days_since_fire",
    "climatology", "tmax_c", "rhmin_pct", "wind_kmh", "precip_mm", "vpd_max_kpa", "et0_mm",
    "precip_7d", "precip_30d", "dry_days", "vpd_7d", "doy_sin", "doy_cos", "lat", "lon",
]
BASELINE = "baseline_score"  # the app's current heuristic (baseline-hdw-v2, weather + activity)


def _days() -> list[date]:
    n = (END - START).days + 1
    return [START + timedelta(days=i) for i in range(n)]


def _year_of(days: list[date]) -> np.ndarray:
    out = np.zeros(len(days), dtype=np.int8)
    for k, ys in enumerate(YEAR_STARTS):
        out[[i for i, d in enumerate(days) if d >= ys]] = k
    return out


def _grid(region: str):
    """Dense lat x lon grid (0.5 deg) covering the region's detections."""
    c = sqlite3.connect(f"file:{training_db(region).as_posix()}?mode=ro", uri=True)
    lat0, lat1, lon0, lon1 = c.execute("SELECT min(latitude), max(latitude), min(longitude), max(longitude) FROM detections").fetchone()
    gy0, gy1 = math.floor((lat0 + 90) / CELL), math.floor((lat1 + 90) / CELL)
    gx0, gx1 = math.floor((lon0 + 180) / CELL), math.floor((lon1 + 180) / CELL)
    return c, gy0, gx0, gy1 - gy0 + 1, gx1 - gx0 + 1


def fire_counts(region: str = REGION):
    """(label NOAA-20 counts, activity NOAA-20+MODIS counts) as [ny, nx, days] arrays."""
    days = _days()
    c, gy0, gx0, ny, nx = _grid(region)
    label = np.zeros((ny, nx, len(days)), dtype=np.int32)
    act = np.zeros_like(label)
    rows = c.execute(
        "SELECT CAST((latitude + 90) / ? AS INTEGER), CAST((longitude + 180) / ? AS INTEGER), "
        "CAST(julianday(date(acquired_at)) - julianday(?) AS INTEGER), satellite, count(*) FROM detections "
        "WHERE static_kind IS NULL AND confidence_level IN ('nominal', 'high') GROUP BY 1, 2, 3, 4",
        (CELL, CELL, START.isoformat()))
    for gy, gx, di, sat, n in rows:
        if 0 <= di < len(days):
            act[gy - gy0, gx - gx0, di] += n
            if sat == "NOAA-20":
                label[gy - gy0, gx - gx0, di] += n
    return label, act, (gy0, gx0, ny, nx), days


def weather_grid(geom, days, region: str = REGION) -> dict[str, np.ndarray]:
    """Each 0.5 deg cell takes the daily weather of the 1 deg cell containing it (NaN if none)."""
    gy0, gx0, ny, nx = geom
    keys = ["temperature_2m_max", "relative_humidity_2m_min", "wind_speed_10m_max", "precipitation_sum",
            "vapour_pressure_deficit_max", "et0_fao_evapotranspiration"]
    out = {k: np.full((ny, nx, len(days)), np.nan, dtype=np.float32) for k in keys}
    index = {d.isoformat(): i for i, d in enumerate(days)}
    for f in weather_dir(region).glob("*.json"):
        w = json.loads(f.read_text())
        rows = [index.get(t) for t in w["daily"]["time"]]
        lat, lon = w["lat"], w["lon"]
        # 0.5 deg cells whose centre falls in this 1 deg cell
        for iy in range(ny):
            clat = (gy0 + iy + 0.5) * CELL - 90
            if abs(clat - lat) > WX_CELL / 2:
                continue
            for ix in range(nx):
                clon = (gx0 + ix + 0.5) * CELL - 180
                if abs(clon - lon) > WX_CELL / 2:
                    continue
                for k in keys:
                    vals = w["daily"][k]
                    for r, v in zip(rows, vals):
                        if r is not None and v is not None:
                            out[k][iy, ix, r] = v
    return out


def _window_sum(a: np.ndarray, end_idx: np.ndarray, w: int) -> np.ndarray:
    """Sum over the w days ending at end_idx (inclusive), along the last axis, for every cell."""
    cum = np.concatenate([np.zeros(a.shape[:-1] + (1,), a.dtype), np.cumsum(a, axis=-1)], axis=-1)
    hi = end_idx + 1
    lo = np.maximum(hi - w, 0)
    return np.take(cum, hi, axis=-1) - np.take(cum, lo, axis=-1)


def build(region: str = REGION, seed: int = 0) -> dict:
    label, act, geom, days = fire_counts(region)
    gy0, gx0, ny, nx = geom
    wx = weather_grid(geom, days, region)
    years = _year_of(days)
    y_day = label > 0
    train_days = years < TEST_YEAR
    # burnable domain: any NOAA-20 fire in the training years (never looks at the held-out year)
    domain = y_day[:, :, train_days].any(axis=-1)
    has_wx = ~np.isnan(wx["temperature_2m_max"]).all(axis=-1)
    domain &= has_wx

    # cumulative helpers along days
    a = act.astype(np.float32)
    nb = np.zeros_like(a)  # 3x3 neighbours excluding the cell itself
    pad = np.pad(a, ((1, 1), (1, 1), (0, 0)))
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            if dy or dx:
                nb += pad[1 + dy:1 + dy + ny, 1 + dx:1 + dx + nx, :]
    cum_a = np.concatenate([np.zeros((ny, nx, 1), np.float32), np.cumsum(a, axis=-1)], axis=-1)
    cum_nb = np.concatenate([np.zeros((ny, nx, 1), np.float32), np.cumsum(nb, axis=-1)], axis=-1)
    precip = np.nan_to_num(wx["precipitation_sum"], nan=0.0)
    cum_p = np.concatenate([np.zeros((ny, nx, 1), np.float32), np.cumsum(precip, axis=-1)], axis=-1)
    vpd = np.nan_to_num(wx["vapour_pressure_deficit_max"], nan=0.0)
    cum_v = np.concatenate([np.zeros((ny, nx, 1), np.float32), np.cumsum(vpd, axis=-1)], axis=-1)
    # days since rain >= 1 mm, counted up to and including each day (as the app's score does)
    dry = np.zeros((ny, nx, len(days)), np.float32)
    run = np.zeros((ny, nx), np.float32)
    since = np.full((ny, nx, len(days)), 365, np.float32)
    last = np.full((ny, nx), -10_000, np.int64)
    for i in range(len(days)):
        run = np.where(precip[:, :, i] >= 1.0, 0, run + 1)
        dry[:, :, i] = run
        last = np.where(a[:, :, i] > 0, i, last)
        since[:, :, i] = np.minimum(i - last, 365)

    # cell climatology by calendar month and year, smoothed toward the region's monthly rate
    months = np.array([d.month for d in days])
    clim = np.zeros((len(YEAR_STARTS), ny, nx, 13), np.float32)
    for k in range(len(YEAR_STARTS)):
        for m in range(1, 13):
            sel = (years == k) & (months == m)
            if sel.any():
                cnt = y_day[:, :, sel].sum(axis=-1)
                n = sel.sum()
                region_rate = y_day[:, :, sel][domain].mean() if domain.any() else 0.0
                clim[k, :, :, m] = (cnt + 10 * region_rate) / (n + 10)

    rng = np.random.default_rng(seed)
    cells = np.argwhere(domain)
    first = HISTORY_DAYS + MAX_LEAD
    X, Y, meta = [], [], []
    w_base = {k: v for k, v in WEIGHTS.items() if k != "fuel"}
    for di in range(first, len(days)):
        lead = rng.integers(1, MAX_LEAD + 1, size=len(cells))
        issue = di - lead
        k = years[di]
        iy, ix = cells[:, 0], cells[:, 1]
        hi = issue + 1
        f1 = cum_a[iy, ix, hi] - cum_a[iy, ix, hi - 1]
        f3 = cum_a[iy, ix, hi] - cum_a[iy, ix, hi - 3]
        f7 = cum_a[iy, ix, hi] - cum_a[iy, ix, hi - 7]
        f30 = cum_a[iy, ix, hi] - cum_a[iy, ix, hi - 30]
        n7 = cum_nb[iy, ix, hi] - cum_nb[iy, ix, hi - 7]
        if k == TEST_YEAR:
            cl = clim[:TEST_YEAR, iy, ix, months[di]].mean(axis=0)
        else:
            cl = clim[1 - k, iy, ix, months[di]]  # the other training year
        t = wx["temperature_2m_max"][iy, ix, di]
        rh = wx["relative_humidity_2m_min"][iy, ix, di]
        wind = wx["wind_speed_10m_max"][iy, ix, di]
        p7 = cum_p[iy, ix, di] - cum_p[iy, ix, di - 7]
        p30 = cum_p[iy, ix, di] - cum_p[iy, ix, di - 30]
        v7 = (cum_v[iy, ix, di] - cum_v[iy, ix, di - 7]) / 7
        doy = days[di].timetuple().tm_yday
        lat = (gy0 + iy + 0.5) * CELL - 90
        lon = (gx0 + ix + 0.5) * CELL - 180
        # the app's current heuristic, computed the way /api/risk does (no NDVI): HDW, dryness, activity
        hdw = np.array([vpd_hpa(float(a_), float(b_)) * (float(c_) / 3.6) if not (np.isnan(a_) or np.isnan(b_) or np.isnan(c_)) else 0.0
                        for a_, b_, c_ in zip(t, rh, wind)], np.float32)
        nearby = f7 + n7
        base = 100 * (w_base["hdw"] * np.clip(hdw / 250, 0, 1) + w_base["dryness"] * np.clip(dry[iy, ix, di] / 14, 0, 1)
                      + w_base["activity"] * np.clip(nearby / 50, 0, 1)) / sum(w_base.values())
        X.append(np.column_stack([
            lead, np.log1p(f1), np.log1p(f3), np.log1p(f7), np.log1p(f30), np.log1p(n7), since[iy, ix, issue],
            cl, t, rh, wind, wx["precipitation_sum"][iy, ix, di], wx["vapour_pressure_deficit_max"][iy, ix, di],
            wx["et0_fao_evapotranspiration"][iy, ix, di], p7, p30, dry[iy, ix, di], v7,
            np.full(len(cells), math.sin(2 * math.pi * doy / 365.25)), np.full(len(cells), math.cos(2 * math.pi * doy / 365.25)),
            lat, lon, base,
        ]).astype(np.float32))
        Y.append(y_day[iy, ix, di])
        meta.append(np.column_stack([np.full(len(cells), di), iy, ix, np.full(len(cells), k)]).astype(np.int32))
    X = np.vstack(X)
    return {
        "X": X[:, :-1], "baseline": X[:, -1], "y": np.concatenate(Y).astype(np.int8), "meta": np.vstack(meta),
        "features": FEATURES, "cells": len(cells), "days": [d.isoformat() for d in days],
        "grid": {"gy0": gy0, "gx0": gx0, "ny": ny, "nx": nx, "cell_deg": CELL},
        "climatology": clim, "domain": domain,
    }


def save(ds: dict, path: Path) -> None:
    np.savez_compressed(path, **{k: v for k, v in ds.items() if isinstance(v, np.ndarray)},
                        meta_json=np.array(json.dumps({k: v for k, v in ds.items() if not isinstance(v, np.ndarray)})))
