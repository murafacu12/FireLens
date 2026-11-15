"""Hourly weather for every spread case, from Meteomatics, cached locally for good.

    python -m ml.fetch_spread_weather     -> data/training/spread/weather/<event>_<T>.json

Cases on the same UTC day share one request window (that day 00:00 to two days later), with up
to POINTS_PER_REQUEST fires per request, so ~4,000 cases cost ~100 requests. The account limit is
1,000 requests a day; this stays far below it. Cached files use Open-Meteo's names, so the model
code reads either source. Resumable: cached cases are skipped. Credentials come from .env.
"""

import json
import logging
import sys
import time
from collections import defaultdict
from datetime import UTC, datetime, timedelta

import httpx

from app.config import Settings
from app.services.meteomatics import Meteomatics, MeteomaticsError
from ml.spread_cases import OUT

log = logging.getLogger("ml.fetch_spread_weather")
WEATHER = OUT / "weather"
POINTS_PER_REQUEST = 50
PARAMS = {  # Meteomatics name -> the Open-Meteo name the model code uses
    "wind_speed_10m:kmh": "wind_speed_10m", "wind_dir_10m:d": "wind_direction_10m", "wind_gusts_10m_1h:kmh": "wind_gusts_10m",
    "relative_humidity_2m:p": "relative_humidity_2m", "precip_1h:mm": "precipitation", "t_2m:C": "temperature_2m",
}


def case_file(c) -> "Path":  # noqa: F821
    return WEATHER / f"{c['event_id']}_{int(c['T'])}.json"


def main() -> None:
    s = Settings()
    if not s.meteomatics_enabled:
        sys.exit("Meteomatics credentials missing in .env")
    cases = json.loads((OUT / "cases.json").read_text())
    WEATHER.mkdir(parents=True, exist_ok=True)
    todo = [c for c in cases if not case_file(c).is_file()]
    by_day: dict = defaultdict(list)
    for c in todo:
        by_day[datetime.fromtimestamp(c["T"], UTC).date()].append(c)
    n_req = sum((len(v) + POINTS_PER_REQUEST - 1) // POINTS_PER_REQUEST for v in by_day.values())
    log.info("%d cases, %d cached, %d to fetch in %d requests", len(cases), len(cases) - len(todo), len(todo), n_req)
    done = 0
    with httpx.Client(headers={"User-Agent": "FireLens/0.2"}) as http:
        mm = Meteomatics(http, s.meteomatics_username, s.meteomatics_password)
        for day, group in sorted(by_day.items()):
            t0 = datetime(day.year, day.month, day.day, tzinfo=UTC)
            for i in range(0, len(group), POINTS_PER_REQUEST):
                chunk = group[i : i + POINTS_PER_REQUEST]
                pts = []
                for c in chunk:
                    f = c["front"]
                    pts.append((sum(p[0] for p in f) / len(f), sum(p[1] for p in f) / len(f)))
                for attempt in range(4):
                    try:
                        blocks = mm.hourly_sync(t0, t0 + timedelta(hours=49), pts, list(PARAMS))
                        break
                    except (MeteomaticsError, httpx.HTTPError) as exc:
                        log.warning("%s: %s (retry %d)", day, str(exc)[:150], attempt + 1)
                        time.sleep(10 * (attempt + 1))
                else:
                    log.error("giving up on %s for now (a rerun retries it)", day)
                    continue
                for c, b in zip(chunk, blocks):
                    case_file(c).write_text(json.dumps({PARAMS.get(k, k): v for k, v in b.items()}))
                done += 1
                time.sleep(0.5)
            log.info("%s: %d cases (%d requests so far)", day, len(group), done)
    log.info("finished: %d requests", done)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    main()
