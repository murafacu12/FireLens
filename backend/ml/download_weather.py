"""Historical daily weather from Open-Meteo for the training cells, paced to the free API limits.

    python -m ml.download_weather            (cells = 1 deg cells of the training DB that had fire)

* Only grid-cell coordinates are sent; nothing about the user.
* Open-Meteo weights a request by its size: 3 years of daily data for one location count as
  ~79 calls (more than 2 weeks counts as several). Free limits: 600 calls/min, 5,000/hour,
  10,000/day, so about 126 locations a day. The pacer stays under all three.
* Resumable: each location is cached as JSON in data/training/weather/; reruns skip them.
"""

import json
import logging
import math
import sqlite3
import sys
import time
from collections import deque
from datetime import date
from pathlib import Path

import httpx

from ml.download_firms import END, REGION, START, training_db

log = logging.getLogger("ml.download_weather")

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
DAILY = ["temperature_2m_max", "relative_humidity_2m_min", "wind_speed_10m_max", "precipitation_sum",
         "vapour_pressure_deficit_max", "et0_fao_evapotranspiration"]
CELL_DEG = 1.0
LIMITS = [(60, 600), (3600, 5000), (86400, 10000)]  # (window seconds, weighted calls)
SAFETY = 0.9  # stay 10 % under each limit


def weather_dir(region: str = REGION) -> Path:
    d = training_db(region).parent / "weather" / region.lower()
    d.mkdir(parents=True, exist_ok=True)
    return d


def call_weight(start: date, end: date, n_vars: int = len(DAILY)) -> int:
    days = (end - start).days + 1
    return math.ceil(days / 14) * math.ceil(n_vars / 10)


TRAINING_END = "2025-11-15"  # held-out year starts here: it must not influence which cells get weather


def fire_cells(region: str = REGION, min_detections: int = 10) -> list[tuple[float, float]]:
    """Centres of the 1 deg cells with at least `min_detections` NOAA-20 fire detections in the *training*
    years (99.97 % of Argentina's fire is in such cells). Counting the held-out year here would leak."""
    c = sqlite3.connect(f"file:{training_db(region).as_posix()}?mode=ro", uri=True)
    rows = c.execute(
        "SELECT CAST((latitude + 90) / ? AS INTEGER), CAST((longitude + 180) / ? AS INTEGER), count(*) FROM detections "
        "WHERE static_kind IS NULL AND satellite = 'NOAA-20' AND confidence_level IN ('nominal', 'high') "
        "AND acquired_at < ? GROUP BY 1, 2 HAVING count(*) >= ?", (CELL_DEG, CELL_DEG, TRAINING_END, min_detections)).fetchall()
    return sorted(((gy + 0.5) * CELL_DEG - 90, (gx + 0.5) * CELL_DEG - 180) for gy, gx, _ in rows)


class Pacer:
    def __init__(self) -> None:
        self.sent: deque[tuple[float, int]] = deque()

    def wait(self, weight: int) -> None:
        while True:
            now = time.time()
            while self.sent and now - self.sent[0][0] > LIMITS[-1][0]:
                self.sent.popleft()
            delay = 0.0
            for window, limit in LIMITS:
                used = sum(w for t, w in self.sent if now - t < window)
                if used + weight > limit * SAFETY:
                    oldest = next(t for t, w in self.sent if now - t < window)
                    delay = max(delay, oldest + window - now + 1)
            if delay <= 0:
                self.sent.append((now, weight))
                return
            log.info("pacing: waiting %.0f s for the Open-Meteo quota", delay)
            time.sleep(min(delay, 900))


def main(region: str = REGION, start: date = START, end: date = END) -> None:
    cells = fire_cells(region)
    out = weather_dir(region)
    todo = [(lat, lon) for lat, lon in cells if not (out / f"{lat:.1f}_{lon:.1f}.json").exists()]
    weight = call_weight(start, end)
    log.info("%d cells with fire, %d cached, %d to fetch; %d weighted calls each (~%.1f days at the free daily limit)",
             len(cells), len(cells) - len(todo), len(todo), weight, len(todo) * weight / (10000 * SAFETY))
    pacer = Pacer()
    with httpx.Client(headers={"User-Agent": "FireLens/0.2"}, timeout=120) as http:
        for i, (lat, lon) in enumerate(todo, 1):
            params = {"latitude": lat, "longitude": lon, "start_date": start.isoformat(), "end_date": end.isoformat(),
                      "daily": ",".join(DAILY), "timezone": "UTC"}
            for attempt in range(6):
                pacer.wait(weight)
                try:
                    r = http.get(ARCHIVE_URL, params=params)
                except httpx.HTTPError as exc:
                    log.warning("network error (%s), retry %d", exc, attempt + 1)
                    time.sleep(30 * (attempt + 1))
                    continue
                if r.status_code == 429:  # over a limit anyway: back off hard
                    log.warning("429 from Open-Meteo: %s; waiting 10 min", r.text[:160])
                    time.sleep(600)
                    continue
                if r.status_code != 200:
                    log.warning("HTTP %d for %.1f,%.1f: %s", r.status_code, lat, lon, r.text[:160])
                    time.sleep(30)
                    continue
                body = r.json()
                (out / f"{lat:.1f}_{lon:.1f}.json").write_text(json.dumps({"lat": lat, "lon": lon, "daily": body["daily"]}))
                break
            else:
                log.error("giving up on %.1f,%.1f", lat, lon)
            if i % 10 == 0 or i == len(todo):
                log.info("%d/%d locations", i, len(todo))
    log.info("done")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # keep request URLs out of logs
    main()
    sys.exit(0)
