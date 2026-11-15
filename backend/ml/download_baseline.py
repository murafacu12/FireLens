"""Download the worldwide FIRMS archive into the seasonal baseline (aggregated, no raw rows kept).

    python -m ml.download_baseline                 (2021-01-01 .. 2025-12-31, VIIRS NOAA-20 + MODIS)
    python -m ml.download_baseline --limit 2       (smoke test: two requests)

* Writes only to data/baseline.db (see app/services/baseline.py); production is never touched.
* Each FIRMS request (one product, up to FIRMS' day range, the whole world) is normalized,
  aggregated to 0.5 deg cells per day and stored at once; finished requests are recorded in
  `baseline_chunks`, so an interrupted run resumes where it stopped.
* Pixels the archive labels as volcano / static land source / offshore are left out, as in the app,
  and their sites are kept (`baseline_static_sites`) to leave the same sites out of live data.
* `baseline_sources` grows with the contiguous days finished per group, so the API only uses
  years that are completely downloaded.
* The FIRMS key is read from .env and never logged (httpx request logging is silenced).
"""

import argparse
import asyncio
import logging
import sys
import time
from datetime import date, timedelta

import httpx
from sqlalchemy import select

from app.config import Settings
from app.ingest.firms_client import FirmsClient, FirmsError, FirmsRateLimited
from app.ingest.normalize import normalize_csv
from app.ingest.sources import get_source
from app.services import baseline as bl
from app.services.coverage import WORLD
from app.services.history import plan_backfill

log = logging.getLogger("ml.download_baseline")

START, END = date(2021, 1, 1), date(2025, 12, 31)  # 5 past seasons; from 2019 the file would be ~40 % larger
PLATFORMS = ["NOAA20", "MODIS"]
ORIGIN = "firms_world"
GROUP_OF_SOURCE = {"VIIRS_NOAA20_SP": "viirs_noaa20", "VIIRS_NOAA20_NRT": "viirs_noaa20", "MODIS_SP": "modis", "MODIS_NRT": "modis"}


def refresh_sources(conn, start: date) -> dict[str, date | None]:
    """Per group, record the days finished contiguously from `start` as one worldwide source."""
    done: dict[str, set[date]] = {}
    for source, first, days in conn.execute(select(bl.chunks_table.c.source, bl.chunks_table.c.start, bl.chunks_table.c.days)):
        grp = GROUP_OF_SOURCE.get(source)
        if grp:
            d0 = date.fromisoformat(first)
            done.setdefault(grp, set()).update(d0 + timedelta(days=i) for i in range(days))
    out = {}
    conn.execute(bl.sources_table.delete().where(bl.sources_table.c.origin == ORIGIN))
    for grp in dict.fromkeys(GROUP_OF_SOURCE.values()):
        days, d = done.get(grp, set()), start
        while d in days:
            d += timedelta(days=1)
        last = d - timedelta(days=1)
        out[grp] = last if last >= start else None
        if out[grp]:
            bl.add_source(conn, grp, start, last, WORLD, ORIGIN)
    return out


async def main(start: date, end: date, limit: int | None) -> None:
    settings = Settings()  # reads the FIRMS key from .env; never printed
    if not settings.firms_map_key:
        sys.exit("FIRELENS_FIRMS_MAP_KEY is required to download the archive")
    engine = bl.open_rw(bl.default_path())
    async with httpx.AsyncClient(headers={"User-Agent": "FireLens/0.2"}) as http:
        client = FirmsClient(http, settings.firms_map_key, settings.firms_base_url, settings.firms_max_day_range)
        availability = await client.data_availability()
        chunks, notes = plan_backfill(start, end, availability, PLATFORMS, settings.firms_max_day_range)
        for n in notes:
            log.warning(n)
        with engine.connect() as conn:
            done = {(r.source, r.start) for r in conn.execute(select(bl.chunks_table.c.source, bl.chunks_table.c.start))}
        todo = [c for c in chunks if (c.source, c.start.isoformat()) not in done]
        log.info("%d requests planned, %d already done, %d to go -> %s", len(chunks), len(chunks) - len(todo), len(todo), bl.default_path())
        if limit:
            todo = todo[:limit]
        t0 = time.time()
        for i, c in enumerate(todo, 1):
            for attempt in range(6):
                try:
                    text = await client.fetch_area_csv(c.source, "world", c.days, c.start)
                    break
                except FirmsRateLimited:
                    log.warning("rate limited; waiting 120 s")
                    await asyncio.sleep(120)
                except (FirmsError, httpx.HTTPError) as exc:
                    log.warning("%s %s failed (%s), retry %d", c.source, c.start, str(exc)[:120], attempt + 1)
                    await asyncio.sleep(15 * (attempt + 1))
            else:
                log.error("giving up on %s %s for now (a rerun retries it)", c.source, c.start)
                continue
            dets = normalize_csv(text, get_source(c.source)).detections
            cells = bl.aggregate((d.instrument, d.satellite, d.latitude, d.longitude, d.acquired_at, d.frp_mw, d.confidence_level)
                                 for d in dets if d.static_kind is None)
            with engine.begin() as conn:
                bl.write_cells(conn, cells)
                # the archive's own labels: used to leave the same sites out of live (unlabelled) data
                bl.write_static_sites(conn, ((d.latitude, d.longitude, d.static_kind) for d in dets
                                             if d.static_kind is not None and d.detection_type is not None))
                conn.execute(bl.chunks_table.insert().prefix_with("OR REPLACE").values(
                    source=c.source, start=c.start.isoformat(), days=c.days, detections=len(dets)))
                through = refresh_sources(conn, start)
            elapsed = time.time() - t0
            log.info("%d/%d %s %s+%dd: %d detections, %d cell-days; complete through %s; %.0f min elapsed, ~%.0f min left",
                     i, len(todo), c.source, c.start, c.days, len(dets), len(cells),
                     ", ".join(f"{g}={d}" for g, d in through.items()), elapsed / 60, elapsed / i * (len(todo) - i) / 60)
            await asyncio.sleep(settings.firms_request_pause_s)
    log.info("finished")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s")
    # httpx logs each request URL at INFO, and FIRMS puts the MAP_KEY in the URL: never log it.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    p = argparse.ArgumentParser()
    p.add_argument("--start", type=date.fromisoformat, default=START)
    p.add_argument("--end", type=date.fromisoformat, default=END)
    p.add_argument("--limit", type=int, default=None, help="stop after this many requests (smoke test)")
    a = p.parse_args()
    asyncio.run(main(a.start, a.end, a.limit))
