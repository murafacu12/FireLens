"""Fill the seasonal baseline from an archive database downloaded by ml.download_firms.

    python -m ml.build_baseline data/training/arg.db

Reads the source read-only and writes per-cell daily counts to data/baseline.db (see
app/services/baseline.py). Only satellites the baseline compares (VIIRS NOAA-20, MODIS) are kept,
and only for the days and area the source's coverage records say were fully downloaded.
"""

import logging
import sqlite3
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

from app.services import baseline as bl

log = logging.getLogger("ml.build_baseline")

SOURCE_GROUP = {"VIIRS_NOAA20_SP": "viirs_noaa20", "VIIRS_NOAA20_NRT": "viirs_noaa20", "MODIS_SP": "modis", "MODIS_NRT": "modis"}


def covered(src: sqlite3.Connection) -> dict[str, tuple[date, date, tuple[float, float, float, float]]]:
    """Per group: the contiguous days covered (SP and NRT together) and the box common to all of them."""
    days: dict[str, set[date]] = defaultdict(set)
    box: dict[str, list[float]] = {}
    for source, day, w, s, e, n, created in src.execute("select source, day, west, south, east, north, created_at from coverage"):
        grp = SOURCE_GROUP.get(source)
        d = date.fromisoformat(day[:10])
        # a day downloaded before it was over (plus late NRT arrivals) is not complete
        if not grp or datetime.fromisoformat(created) < datetime.combine(d + timedelta(days=1), datetime.min.time()) + timedelta(hours=bl.SETTLE_HOURS):
            continue
        days[grp].add(d)
        b = box.setdefault(grp, [w, s, e, n])
        box[grp] = [max(b[0], w), max(b[1], s), min(b[2], e), min(b[3], n)]
    out = {}
    for grp, ds in days.items():
        first, last = min(ds), max(ds)
        gaps = (last - first).days + 1 - len(ds)
        if gaps:  # keep the longest contiguous run ending at `last`
            d = last
            while d - timedelta(days=1) in ds:
                d -= timedelta(days=1)
            log.warning("%s: %d day(s) missing; using %s..%s only", grp, gaps, d, last)
            first = d
        out[grp] = (first, last, tuple(box[grp]))
    return out


def main(src_path: str) -> None:
    src = sqlite3.connect(f"file:{Path(src_path).resolve().as_posix()}?mode=ro", uri=True)
    spans = covered(src)
    if not spans:
        sys.exit("the source has no coverage records for VIIRS NOAA-20 or MODIS")
    c = bl.CELL_DEG
    sql = f"""
        select case when satellite = 'NOAA-20' then 'viirs_noaa20' when instrument = 'MODIS' then 'modis' end as grp,
               cast(julianday(date(acquired_at)) - 2440587.5 as integer) as day,
               min(cast((latitude + 90) / {c} as integer), {int(180 / c) - 1}) as cy,
               min(cast((longitude + 180) / {c} as integer), {int(360 / c) - 1}) as cx,
               count(*), sum(confidence_level in ('nominal', 'high')), coalesce(sum(frp_mw), 0)
        from detections
        where static_kind is null and (satellite = 'NOAA-20' or instrument = 'MODIS')
        group by 1, 2, 3, 4"""
    cells = {}
    for grp, day, cy, cx, n, n_nh, frp in src.execute(sql):
        first, last, _ = spans[grp]
        if bl.day_no(first) <= day <= bl.day_no(last):
            cells[(grp, day, cy, cx)] = [n, n_nh, frp]
    engine = bl.open_rw(bl.default_path())
    origin = Path(src_path).name
    with engine.begin() as conn:
        conn.execute(bl.sources_table.delete().where(bl.sources_table.c.origin == origin))  # re-run: replace
        written = bl.write_cells(conn, cells)
        sites = bl.write_static_sites(conn, src.execute(
            "select latitude, longitude, static_kind from detections where detection_type in (1, 2, 3) and static_kind is not null"))
        log.info("%d static (non-vegetation) sites from the archive labels", sites)
        for grp, (first, last, box) in spans.items():
            bl.add_source(conn, grp, first, last, box, origin)
            log.info("%s: %s..%s over %s", grp, first, last, ",".join(f"{x:g}" for x in bl.inner_cells(box)))
    log.info("%d cell-days written to %s", written, bl.default_path())


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s")
    main(sys.argv[1] if len(sys.argv) > 1 else str(Path(__file__).resolve().parents[2] / "data" / "training" / "arg.db"))
