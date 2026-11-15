"""Backtest the 24 h fire-spread zones on past FireLens fires.

    python -m ml.backtest_spread                (300 fires from the last ~80 days)

For each sampled fire, at one moment T in its life:
* front   = its VIIRS detections in the 12 h before T;
* targets = its VIIRS detections in the 24 h after T on NEW ground (> 0.75 km from every detection
            up to T), i.e. where the fire actually spread;
* weather = Open-Meteo hourly for T..T+24 h at the fire (cached in data/training/spread_weather/).

Three rankings of the grid cells are compared AT EQUAL AREA (the area the full model reaches in
24 h at each SCALE), so a bigger zone can never win by being bigger:
* full      fuel (land cover) + wind + slope + humidity (app/services/spread.py)
* wind      the same wind and humidity, uniform fuel, flat ground
* circle    plain distance from the front (no wind, no terrain, no fuel)
Score: share of the target detections inside the zone ("capture"), per fire, then averaged; paired
bootstrap over fires for full - circle and full - wind. Reads production read-only.
"""

import argparse
import json
import logging
import math
import random
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import numpy as np
from scipy.ndimage import distance_transform_edt

from app.config import Settings
from app.services import spread as sp

log = logging.getLogger("ml.backtest_spread")
ROOT = Path(__file__).resolve().parents[2]
WEATHER_DIR = ROOT / "data" / "training" / "spread_weather"
NEW_GROUND_KM = 0.75
SCALES = [0.1, 0.25, 0.5, 1.0, 2.0]
HALF_KM = 15.0
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"


def km(lat1, lon1, lat2, lon2):
    """Equirectangular distance matrix (km), fine at fire scales."""
    k = 111.2
    dy = (lat1[:, None] - lat2[None, :]) * k
    dx = (lon1[:, None] - lon2[None, :]) * k * np.cos(np.radians(lat1[:, None]))
    return np.hypot(dx, dy)


def pick_cases(db: sqlite3.Connection, n: int, seed: int, now: datetime):
    since = (now - timedelta(days=80)).strftime("%Y-%m-%d")
    ids = [r[0] for r in db.execute(
        "select id from fire_events where start_at >= ? and observed_hours >= 30 and viirs_count >= 15 and pass_count >= 4",
        (since,))]
    random.Random(seed).shuffle(ids)
    cases = []
    for eid in ids:
        rows = db.execute("select latitude, longitude, acquired_at from detections where event_id = ? and instrument = 'VIIRS' "
                          "and static_kind is null order by acquired_at", (eid,)).fetchall()
        if len(rows) < 10:
            continue
        lat = np.array([r[0] for r in rows])
        lon = np.array([r[1] for r in rows])
        t = np.array([datetime.fromisoformat(r[2]).replace(tzinfo=UTC).timestamp() for r in rows])
        for T in sorted(set(t.tolist()))[1:]:
            if T + 24 * 3600 > now.timestamp() - 6 * 3600:
                break
            front = (t > T - sp.FRONT_HOURS * 3600) & (t <= T)
            nxt = (t > T) & (t <= T + 24 * 3600)
            if front.sum() < 1 or nxt.sum() < 3:
                continue
            prior = t <= T
            d = km(lat[nxt], lon[nxt], lat[prior], lon[prior]).min(axis=1)
            new = np.flatnonzero(nxt)[d > NEW_GROUND_KM]
            if len(new) >= 3:
                cases.append({"event_id": eid, "T": T, "front": np.flatnonzero(front), "targets": new, "lat": lat, "lon": lon})
                break
        if len(cases) >= n:
            break
    return cases


def weather(http: httpx.Client, case) -> dict | None:
    WEATHER_DIR.mkdir(parents=True, exist_ok=True)
    p = WEATHER_DIR / f"{case['event_id']}_{int(case['T'])}.json"
    if p.is_file():
        return json.loads(p.read_text())
    fi = case["front"]
    lat, lon = float(case["lat"][fi].mean()), float(case["lon"][fi].mean())
    t0 = datetime.fromtimestamp(case["T"], UTC)
    params = {"latitude": round(lat, 3), "longitude": round(lon, 3), "timezone": "UTC",
              "hourly": "wind_speed_10m,wind_direction_10m,relative_humidity_2m,precipitation",
              "start_hour": t0.strftime("%Y-%m-%dT%H:00"), "end_hour": (t0 + timedelta(hours=25)).strftime("%Y-%m-%dT%H:00")}
    for attempt in range(3):
        r = http.get(FORECAST_URL, params=params)
        if r.status_code == 429:
            time.sleep(60)
            continue
        if r.status_code != 200:
            log.warning("weather %s: %s %s", case["event_id"], r.status_code, r.text[:120])
            return None
        body = r.json()["hourly"]
        p.write_text(json.dumps(body))
        time.sleep(0.5)
        return body
    return None


def capture_at(rank: np.ndarray, n_cells: int, target_cells: np.ndarray) -> float:
    """Share of targets among the n_cells best-ranked cells (rank: lower = earlier)."""
    if n_cells <= 0:
        return 0.0
    order = np.argsort(rank.ravel(), kind="stable")[:n_cells]
    chosen = np.zeros(rank.size, dtype=bool)
    chosen[order] = True
    return float(chosen[target_cells].mean()) if len(target_cells) else float("nan")


def run_case(case, hourly) -> dict | None:
    fi, ti = case["front"], case["targets"]
    lat0, lon0 = float(case["lat"][fi].mean()), float(case["lon"][fi].mean())
    g = sp.make_grid(lat0, lon0, HALF_KM)
    base, slope, downhill, cov = sp.ground_layers(g, budget_s=60)
    if cov < 0.99:
        return None
    steps = sp.steps_from_hourly(hourly, datetime.fromtimestamp(case["T"], UTC))
    fr = g.cell(case["lat"][fi], case["lon"][fi])
    tr, tc = g.cell(case["lat"][ti], case["lon"][ti])
    inside = (tr >= 0) & (tr < g.rows) & (tc >= 0) & (tc < g.cols)
    target_cells = np.where(inside, tr * g.cols + tc, -1)
    # targets outside the grid count as misses for every method
    tcells = target_cells[target_cells >= 0]
    miss_out = 1.0 - inside.mean()
    # circle: distance from the front cells
    mask = np.ones((g.rows, g.cols), dtype=bool)
    ok = (fr[0] >= 0) & (fr[0] < g.rows) & (fr[1] >= 0) & (fr[1] < g.cols)
    mask[fr[0][ok], fr[1][ok]] = False
    circle = distance_transform_edt(mask, sampling=(g.dy_m, g.dx_m))
    flat = np.zeros_like(slope)
    uniform = np.where(base >= 0, 1.0, 0.0)
    out = {"event_id": case["event_id"], "T": case["T"], "targets": int(len(ti)), "front": int(len(fi)),
           "wind_kmh": round(float(np.mean([w.wind_kmh for w in steps])), 1), "scales": {}}
    for s in SCALES:
        t_full = sp.arrival_hours(g, base, slope, downhill, fr, steps, scale=s)
        n = int(np.isfinite(t_full).sum())
        t_wind = sp.arrival_hours(g, uniform * float(np.median(base[base > 0]) if (base > 0).any() else 1.0), flat, flat, fr, steps, scale=s)
        rank_full = np.where(np.isfinite(t_full), t_full, 1e9)
        rank_wind = np.where(np.isfinite(t_wind), t_wind, 1e9) + circle * 1e-9  # ties: nearer first
        rank_full = rank_full + circle * 1e-9
        k = (1 - miss_out)
        out["scales"][s] = {
            "area_km2": round(n * g.dx_m * g.dy_m / 1e6, 2),
            "full": capture_at(rank_full, n, tcells) * k,
            "wind": capture_at(rank_wind, n, tcells) * k,
            "circle": capture_at(circle, n, tcells) * k,
        }
    return out


def bootstrap(diff: np.ndarray, n: int = 2000, seed: int = 1) -> tuple[float, float]:
    rng = np.random.default_rng(seed)
    means = [rng.choice(diff, len(diff)).mean() for _ in range(n)]
    return float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))


def main(n: int, seed: int) -> None:
    s = Settings()
    url = s.database_url_effective.split("///", 1)[1]
    p = Path(url) if Path(url).is_absolute() else ROOT / url
    db = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)
    now = datetime.now(UTC)
    cases = pick_cases(db, n, seed, now)
    log.info("%d cases", len(cases))
    results = []
    with httpx.Client(timeout=30, headers={"User-Agent": "FireLens/0.2"}) as http:
        for i, c in enumerate(cases, 1):
            hourly = weather(http, c)
            if not hourly:
                continue
            try:
                r = run_case(c, hourly)
            except Exception as exc:
                log.warning("case %s failed: %s", c["event_id"], exc)
                continue
            if r:
                results.append(r)
            if i % 20 == 0:
                log.info("%d/%d cases run, %d scored", i, len(cases), len(results))
    out = ROOT / "data" / "training" / "spread_backtest.json"
    out.write_text(json.dumps(results, indent=1))
    print(f"\n{len(results)} fires scored (results in {out})")
    print(f"{'scale':>6} {'area km2 (median)':>18} {'full':>6} {'wind':>6} {'circle':>7}   full-circle (95% CI)        full-wind (95% CI)")
    for s in SCALES:
        rows = [r["scales"][s] for r in results]
        f, w, c = (np.array([x[k] for x in rows]) for k in ("full", "wind", "circle"))
        a = np.median([x["area_km2"] for x in rows])
        lo1, hi1 = bootstrap(f - c)
        lo2, hi2 = bootstrap(f - w)
        print(f"{s:>6} {a:>18.1f} {f.mean():>6.3f} {w.mean():>6.3f} {c.mean():>7.3f}   "
              f"{(f - c).mean():+.3f} ({lo1:+.3f}..{hi1:+.3f})   {(f - w).mean():+.3f} ({lo2:+.3f}..{hi2:+.3f})")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", type=int, default=300)
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()
    main(a.n, a.seed)
