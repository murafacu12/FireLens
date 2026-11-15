"""Download the land-cover tiles under every fire detection in the database (busiest tiles first).

    python -m ml.prefetch_geodata              (ESA WorldCover; ~1,200 tiles, ~1 GB, ~2 h)
    python -m ml.prefetch_geodata --dem        (also the Copernicus DEM; ~5,500 tiles, usually not needed)

The API downloads missing tiles on first use too; this only spares users the wait. Resumable:
tiles already in data/geo are skipped. The production database is only read.
"""

import argparse
import logging
import sqlite3
import time
from pathlib import Path

from app.config import Settings
from app.services import geodata

log = logging.getLogger("ml.prefetch_geodata")


def db_path(settings: Settings) -> Path:
    url = settings.database_url_effective
    path = url.split("///", 1)[1]
    p = Path(path)
    return p if p.is_absolute() else Path(__file__).resolve().parents[2] / p


def main(with_dem: bool) -> None:
    db = sqlite3.connect(f"file:{db_path(Settings()).as_posix()}?mode=ro", uri=True)
    sources = [geodata.worldcover()] + ([geodata.dem()] if with_dem else [])
    for src in sources:
        d = src.tile_deg
        keys = [(int(y) * d, int(x) * d) for y, x, _ in db.execute(
            f"select cast(floor(latitude / {d}) as int), cast(floor(longitude / {d}) as int), count(*) from detections "
            "where static_kind is null group by 1, 2 order by 3 desc")]
        todo = [k for k in keys if src.cached(k) is False]
        log.info("%s: %d tiles under detections, %d to download", src.name, len(keys), len(todo))
        t0 = time.time()
        for i, k in enumerate(todo, 1):
            try:
                src.fetch(k)
            except Exception as exc:  # retried on the next run
                log.warning("%s %s failed: %s", src.name, k, exc)
            if i % 25 == 0 or i == len(todo):
                el = time.time() - t0
                log.info("%s: %d/%d, %.0f min elapsed, ~%.0f min left", src.name, i, len(todo), el / 60, el / i * (len(todo) - i) / 60)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("--dem", action="store_true")
    main(p.parse_args().dem)
