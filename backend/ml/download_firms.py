"""Download the NASA FIRMS archive for a region into a separate training database.

    python -m ml.download_firms            (Argentina, 2023-11-15 .. 2026-11-14, defaults below)

* Production (`firelens.db`) is never touched: rows go to data/training/<region>.db.
* Satellites: VIIRS NOAA-20 and MODIS (Terra + Aqua). These fly through the whole period. NOAA-21
  (from 2024) is left out on purpose: adding a satellite part-way would make later years look
  fierier only because more of them are watching.
* For each satellite and day, the science-quality archive (SP) is used where FIRMS has it, else NRT.
* Resumable: every finished request is recorded in `coverage`; a rerun skips those.
"""

import asyncio
import logging
import sys
import time
from datetime import date, datetime, timedelta, UTC
from pathlib import Path

import httpx

from app import models  # noqa: F401  (registers tables)
from app.config import Settings
from app.db import Base, make_engine, make_session_factory
from app.ingest.firms_client import FirmsClient, FirmsError, FirmsRateLimited
from app.ingest.normalize import normalize_csv
from app.ingest.sources import get_source
from app.models import CoverageRow
from app.services.coverage import days_between, record_coverage
from app.services.history import COUNTRY_BBOX, plan_backfill
from app.services.store import flag_static_sources, upsert_detections

log = logging.getLogger("ml.download_firms")

REGION = "ARG"
START, END = date(2023, 11, 15), date(2026, 11, 14)
PLATFORMS = ["NOAA20", "MODIS"]
DATA_DIR = Path(__file__).resolve().parents[2] / "data" / "training"


def training_db(region: str = REGION) -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR / f"{region.lower()}.db"


async def main(region: str = REGION, start: date = START, end: date = END) -> None:
    settings = Settings()  # reads the FIRMS key from .env; never printed
    if not settings.firms_map_key:
        sys.exit("FIRELENS_FIRMS_MAP_KEY is required to download the archive")
    db = training_db(region)
    engine = make_engine(f"sqlite:///{db.as_posix()}")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    bbox = COUNTRY_BBOX[region][1]
    box = tuple(float(x) for x in bbox.split(","))

    async with httpx.AsyncClient(headers={"User-Agent": "FireLens/0.2"}) as http:
        client = FirmsClient(http, settings.firms_map_key, settings.firms_base_url, settings.firms_max_day_range)
        availability = await client.data_availability()
        chunks, notes = plan_backfill(start, end, availability, PLATFORMS, settings.firms_max_day_range)
        for n in notes:
            log.warning(n)
        with factory() as s:
            done = {(r.source, r.day) for r in s.query(CoverageRow.source, CoverageRow.day)}
        todo = [c for c in chunks if not all((c.source, d) in done for d in days_between(c.start, c.start + timedelta(days=c.days - 1)))]
        log.info("%s: %d requests planned, %d already done, %d to go -> %s", region, len(chunks), len(chunks) - len(todo), len(todo), db)
        t0 = time.time()
        for i, c in enumerate(todo, 1):
            for attempt in range(4):
                try:
                    text = await client.fetch_area_csv(c.source, bbox, c.days, c.start)
                    break
                except FirmsRateLimited:
                    log.warning("rate limited; waiting 120 s")
                    await asyncio.sleep(120)
                except (FirmsError, httpx.HTTPError) as exc:
                    log.warning("%s %s failed (%s), retry %d", c.source, c.start, str(exc)[:120], attempt + 1)
                    await asyncio.sleep(10 * (attempt + 1))
            else:
                log.error("giving up on %s %s", c.source, c.start)
                continue
            result = normalize_csv(text, get_source(c.source))
            with factory() as s:
                upsert_detections(s, result.detections)
                record_coverage(s, days_between(c.start, c.start + timedelta(days=c.days - 1)), box, c.source)
                s.commit()
            if i % 20 == 0 or i == len(todo):
                log.info("%d/%d requests, last %s %s (+%d rows), %.0f s elapsed", i, len(todo), c.source, c.start,
                         len(result.detections), time.time() - t0)
            await asyncio.sleep(settings.firms_request_pause_s)
        with factory() as s:  # gas flares etc. learned from the archive's own labels
            flagged = flag_static_sources(s)
            s.commit()
        log.info("done; %d NRT detections flagged as non-fire sources; finished %s", len(flagged), datetime.now(UTC).isoformat())


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s")
    # httpx logs each request URL at INFO, and FIRMS puts the MAP_KEY in the URL: never log it.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(main())
