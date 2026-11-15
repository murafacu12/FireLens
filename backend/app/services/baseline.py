"""Seasonal baseline: is this period's fire activity normal for the time of year?

The current period is compared with the *same calendar days* of earlier years, over the same area.

What makes the comparison fair (and what it does not do):
* **Same satellites every year.** Counts grow when more satellites watch: NOAA-21 joined in 2023,
  so "VIIRS" in 2026 sees more than "VIIRS" in 2020 for the same fires. Each group is compared
  only with itself: VIIRS NOAA-20 (flying since 2018) and MODIS (Terra + Aqua). Caveat: both MODIS
  satellites have been drifting to later overpass times since ~2020-2022, which slowly changes what
  they see; NOAA-20 is the steadier reference and is the headline when it has enough seasons.
* **Same area every year.** The archive is stored per 0.5 deg cell and day, so the area is snapped
  outward to whole cells, and cut to the part every year's archive covers. The current period is
  counted over exactly that area (it is reported: `area`).
* **Whole UTC days only**, and only days that ended at least SETTLE_HOURS ago (late NRT data).
* **Same pixels.** Non-vegetation sources (gas flares, volcanoes, offshore platforms) are left out
  on both sides; every confidence level is counted, as in the rest of the analysis.
* The archive is FIRMS' science-quality product (SP) and recent periods are near-real-time (NRT):
  the two differ by a few percent, far less than the year-to-year swings this is meant to show.

Storage: `baseline_cells` (group x day x 0.5 deg cell counts) in its own SQLite file, never in the
production database. It is filled by `python -m ml.build_baseline` (from a downloaded archive
database) and `python -m ml.download_baseline` (worldwide, from the FIRMS archive API).
`baseline_sources` records which area and days each import covers, so a year whose archive does
not cover the period is left out instead of being read as a quiet year.
"""

import math
import statistics
from collections import Counter, defaultdict
from collections.abc import Iterable
from datetime import UTC, date, datetime, time, timedelta
from functools import lru_cache
from pathlib import Path

import numpy as np
from sqlalchemy import Column, Engine, Float, Integer, MetaData, String, Table, cast, create_engine, func, or_, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.models import DetectionRow
from app.services.coverage import WORLD, BBox, _split, day_status
from app.services.store import SITE_CELL_DEG

CELL_DEG = 0.5
GROUPS = {"viirs_noaa20": "VIIRS NOAA-20", "modis": "MODIS (Terra + Aqua)"}
HEADLINE_ORDER = ("viirs_noaa20", "modis")
MAX_YEARS_BACK = 10
MIN_SEASONS = 2  # fewer past seasons than this: no verdict
MIN_AREA_FRACTION = 0.5  # the archive must cover at least this much of the requested area
SETTLE_HOURS = 6  # NRT detections of a day keep arriving for a few hours after it ends
MIN_TYPICAL = 10  # below this many detections in a typical year, a ratio says nothing
EPOCH = date(1970, 1, 1)

meta = MetaData()
cells_table = Table(
    "baseline_cells", meta,
    Column("grp", String(16), primary_key=True),
    Column("day", Integer, primary_key=True),  # days since 1970-01-01 (UTC)
    Column("cy", Integer, primary_key=True),  # floor((lat + 90) / CELL_DEG)
    Column("cx", Integer, primary_key=True),  # floor((lon + 180) / CELL_DEG)
    Column("n", Integer, nullable=False),  # detections, every confidence level
    Column("n_nh", Integer, nullable=False),  # nominal + high confidence
    Column("frp_sum", Float, nullable=False),
    sqlite_with_rowid=False,
)
sources_table = Table(
    "baseline_sources", meta,
    Column("id", Integer, primary_key=True),
    Column("grp", String(16), nullable=False),
    Column("first_day", Integer, nullable=False),
    Column("last_day", Integer, nullable=False),
    Column("west", Float, nullable=False), Column("south", Float, nullable=False),
    Column("east", Float, nullable=False), Column("north", Float, nullable=False),
    Column("origin", String(64), nullable=False),
)
static_sites_table = Table(  # non-vegetation sites (gas flares, volcanoes, platforms) the archive labelled
    "baseline_static_sites", meta,
    Column("gy", Integer, primary_key=True),  # floor(lat / SITE_CELL_DEG)
    Column("gx", Integer, primary_key=True),
    Column("kind", String(12), nullable=False),
    sqlite_with_rowid=False,
)
chunks_table = Table(  # finished download requests (resumable downloads)
    "baseline_chunks", meta,
    Column("source", String(32), primary_key=True),
    Column("start", String(10), primary_key=True),
    Column("days", Integer, nullable=False),
    Column("detections", Integer, nullable=False),
)


# ---------------------------------------------------------------------------- storage


def default_path() -> Path:
    return Path(__file__).resolve().parents[3] / "data" / "baseline.db"


def open_rw(path: Path) -> Engine:
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{path.as_posix()}")
    meta.create_all(engine)
    return engine


@lru_cache(maxsize=4)
def _open_ro(path: str) -> Engine:
    return create_engine(f"sqlite:///file:{Path(path).as_posix()}?mode=ro&uri=true")


def open_ro(path: Path) -> Engine | None:
    """The baseline database read-only, or None when it has not been built."""
    if not path.is_file():
        return None
    return _open_ro(str(path))


def version(path: Path) -> float:
    """Changes whenever the baseline file does (cache key for results computed from it)."""
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def day_no(d: date) -> int:
    return (d - EPOCH).days


def cell_of(lat: float, lon: float) -> tuple[int, int]:
    return (min(int(math.floor((lat + 90) / CELL_DEG)), int(180 / CELL_DEG) - 1),
            min(int(math.floor((lon + 180) / CELL_DEG)), int(360 / CELL_DEG) - 1))


def group_of(instrument: str, satellite: str) -> str | None:
    if satellite == "NOAA-20":
        return "viirs_noaa20"
    if instrument == "MODIS":
        return "modis"
    return None


def aggregate(rows: Iterable[tuple[str, str, float, float, datetime, float | None, str | None]]) -> dict:
    """(instrument, satellite, lat, lon, acquired_at, frp, confidence_level) of non-static detections
    -> {(grp, day, cy, cx): [n, n_nh, frp_sum]}."""
    out: dict[tuple, list] = defaultdict(lambda: [0, 0, 0.0])
    for ins, sat, lat, lon, when, frp, conf in rows:
        grp = group_of(ins, sat)
        if grp is None:
            continue
        acc = out[(grp, day_no(when.date()), *cell_of(lat, lon))]
        acc[0] += 1
        acc[1] += conf in ("nominal", "high")
        acc[2] += frp or 0.0
    return out


def write_cells(conn, cells: dict) -> int:
    """Store aggregated cells. An import covers whole cell-days, so it replaces what was there
    (re-running an import, or a worldwide import over a regional one, never double counts)."""
    rows = [{"grp": g, "day": d, "cy": cy, "cx": cx, "n": v[0], "n_nh": v[1], "frp_sum": round(v[2], 2)}
            for (g, d, cy, cx), v in cells.items()]
    stmt = sqlite_insert(cells_table)
    stmt = stmt.on_conflict_do_update(
        index_elements=["grp", "day", "cy", "cx"],
        set_={"n": stmt.excluded.n, "n_nh": stmt.excluded.n_nh, "frp_sum": stmt.excluded.frp_sum})
    if rows:
        conn.execute(stmt, rows)  # executemany
    return len(rows)


def write_static_sites(conn, sites: Iterable[tuple[float, float, str]]) -> int:
    """Remember where the archive labelled pixels as non-vegetation (lat, lon, kind)."""
    rows = {(math.floor(lat / SITE_CELL_DEG), math.floor(lon / SITE_CELL_DEG)): kind for lat, lon, kind in sites}
    if rows:
        conn.execute(sqlite_insert(static_sites_table).prefix_with("OR IGNORE"),
                     [{"gy": gy, "gx": gx, "kind": k} for (gy, gx), k in rows.items()])
    return len(rows)


def inner_cells(bbox: BBox) -> BBox:
    """The whole cells inside a box: an import of that box covers these completely."""
    w, s, e, n = bbox
    c = CELL_DEG
    return (math.ceil(w / c) * c, math.ceil(s / c) * c, math.floor(e / c) * c, math.floor(n / c) * c)


def add_source(conn, grp: str, first: date, last: date, bbox: BBox, origin: str) -> None:
    w, s, e, n = inner_cells(bbox)
    conn.execute(sources_table.insert().values(grp=grp, first_day=day_no(first), last_day=day_no(last),
                                               west=w, south=s, east=e, north=n, origin=origin))


# ---------------------------------------------------------------------------- comparison


def _shift_years(d: date, k: int) -> date:
    try:
        return d.replace(year=d.year - k)
    except ValueError:  # 29 February
        return d.replace(year=d.year - k, day=28)


def _snap_out(b: BBox) -> BBox:
    c = CELL_DEG
    return (math.floor(b[0] / c) * c, math.floor(b[1] / c) * c, math.ceil(b[2] / c) * c, math.ceil(b[3] / c) * c)


def _intersect(boxes: list[BBox], box: BBox) -> list[BBox]:
    out = []
    for b in boxes:
        x = (max(b[0], box[0]), max(b[1], box[1]), min(b[2], box[2]), min(b[3], box[3]))
        if x[0] < x[2] and x[1] < x[3]:
            out.append(x)
    return out


def _area(boxes: list[BBox]) -> float:
    return sum((b[2] - b[0]) * (b[3] - b[1]) for b in boxes)


def full_days(start: datetime, end: datetime, now: datetime) -> tuple[date, date] | None:
    """Whole UTC days inside [start, end) that ended at least SETTLE_HOURS before `now`."""
    start, end, now = (x.astimezone(UTC) if x.tzinfo else x.replace(tzinfo=UTC) for x in (start, end, now))
    first = start.date() if start.time() == time(0) else start.date() + timedelta(days=1)
    # the last day whose end (next midnight) is not after the limit
    last = min(end, now - timedelta(hours=SETTLE_HOURS)).date() - timedelta(days=1)
    return (first, last) if last >= first else None


def _box_filter(lat_col, lon_col, boxes: list[BBox]):
    return or_(*[(lat_col >= b[1]) & (lat_col < b[3]) & (lon_col >= b[0]) & (lon_col < b[2]) for b in boxes])


def _past_daily(base: Engine, grp: str, first: date, n_days: int, boxes: list[BBox]) -> list[int]:
    c = CELL_DEG
    cell_boxes = [(round((b[0] + 180) / c), round((b[1] + 90) / c), round((b[2] + 180) / c), round((b[3] + 90) / c)) for b in boxes]
    d0 = day_no(first)
    where = or_(*[(cells_table.c.cy >= y0) & (cells_table.c.cy < y1) & (cells_table.c.cx >= x0) & (cells_table.c.cx < x1)
                  for x0, y0, x1, y1 in cell_boxes])
    out = [0] * n_days
    with base.connect() as conn:
        for day, n in conn.execute(select(cells_table.c.day, func.sum(cells_table.c.n)).where(
                cells_table.c.grp == grp, cells_table.c.day >= d0, cells_table.c.day < d0 + n_days, where).group_by(cells_table.c.day)):
            out[day - d0] = int(n)
    return out


def _static_sites(base: Engine, boxes: list[BBox]) -> np.ndarray:
    """Encoded keys of the archive's static sites in the area (sorted, for np.isin)."""
    c = SITE_CELL_DEG
    t = static_sites_table.c
    where = or_(*[(t.gy >= math.floor(b[1] / c) - 1) & (t.gy <= math.floor(b[3] / c) + 1)
                  & (t.gx >= math.floor(b[0] / c) - 1) & (t.gx <= math.floor(b[2] / c) + 1) for b in boxes])
    with base.connect() as conn:
        keys = [gy * _SITE_STRIDE + gx for gy, gx in conn.execute(select(t.gy, t.gx).where(where))]
    return np.unique(np.asarray(keys, dtype=np.int64))


_SITE_STRIDE = 1 << 20  # > 360 / SITE_CELL_DEG


def _current_daily(live: Session, grp: str, first: date, n_days: int, boxes: list[BBox], sites: np.ndarray) -> list[int]:
    """Detections per day in the live database. Besides the live database's own static flags,
    detections on a site the archive labelled non-vegetation (its 3x3 neighbourhood of
    SITE_CELL_DEG cells, as the app does) are left out: the live database learns those sites from
    far fewer labelled rows, and the past seasons exclude them by label."""
    t0 = datetime.combine(first, time(0))
    t1 = t0 + timedelta(days=n_days)
    which = DetectionRow.satellite == "NOAA-20" if grp == "viirs_noaa20" else DetectionRow.instrument == "MODIS"
    day = cast(func.julianday(func.date(DetectionRow.acquired_at)) - 2440587.5, Integer)
    where = (DetectionRow.acquired_at >= t0, DetectionRow.acquired_at < t1, DetectionRow.static_kind.is_(None), which,
             _box_filter(DetectionRow.latitude, DetectionRow.longitude, boxes))
    out = [0] * n_days
    d0 = day_no(first)
    if not len(sites):
        for d, n in live.execute(select(day, func.count()).where(*where).group_by(day)):
            out[d - d0] = int(n)
        return out
    rows = live.connection().execute(select(day, DetectionRow.latitude, DetectionRow.longitude).where(*where)).all()
    if not rows:
        return out
    days, lat, lon = (np.asarray(x) for x in zip(*rows))
    gy = np.floor(lat.astype(float) / SITE_CELL_DEG).astype(np.int64)
    gx = np.floor(lon.astype(float) / SITE_CELL_DEG).astype(np.int64)
    on_site = np.zeros(len(rows), dtype=bool)
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            on_site |= np.isin((gy + dy) * _SITE_STRIDE + gx + dx, sites)
    counts = np.bincount(days[~on_site].astype(np.int64) - d0, minlength=n_days)
    return [int(x) for x in counts[:n_days]]


def verdict(current: int, past: list[int]) -> str:
    """within_range / above / well_above / below / well_below, or why there is none.

    With a handful of past seasons the year-to-year spread is the yardstick, not a ratio to the
    median: a period is only "above" (or "below") normal when it is outside the range of every past
    season by more than counting noise (2 x sqrt(n) of that bound). "well_" adds a 1.5x margin over
    the median (or under, 0.67x)."""
    if len(past) < MIN_SEASONS:
        return "insufficient_seasons"
    med, hi, lo = statistics.median(past), max(past), min(past)
    if med < MIN_TYPICAL and current < MIN_TYPICAL:
        return "too_few_fires"
    if current > hi + 2 * math.sqrt(max(hi, 1.0)):
        return "well_above" if current >= 1.5 * med else "above"
    if current < lo - 2 * math.sqrt(max(lo, 1.0)):
        return "well_below" if current <= 0.67 * med else "below"
    return "within_range"


def compare(live: Session, base: Engine | None, bbox: BBox | None, start: datetime, end: datetime,
            now: datetime | None = None, assume_loaded: bool = False) -> dict:
    bbox = bbox or WORLD
    now = now or datetime.now(UTC)
    days = full_days(start, end, now)
    out: dict = {"available": False, "reason": None, "cell_deg": CELL_DEG, "groups": {}, "headline": None}
    if base is None:
        out["reason"] = "no_baseline"
        return out
    if days is None:
        out["reason"] = "period_too_short"
        return out
    first, last = days
    n_days = (last - first).days + 1
    out["days"] = {"first": first.isoformat(), "last": last.isoformat(), "count": n_days}
    requested = [_snap_out(b) for b in _split(bbox)]
    req_area = _area(requested)

    with base.connect() as conn:
        sources = conn.execute(select(sources_table)).all()

    any_group = False
    for grp, label in GROUPS.items():
        # for each past year, the source (import) covering all its days with the largest share of the area
        years: list[tuple[int, date, BBox]] = []
        for k in range(1, MAX_YEARS_BACK + 1):
            a = _shift_years(first, k)
            d0, d1 = day_no(a), day_no(a) + n_days - 1
            best, best_area = None, 0.0
            for s in sources:
                if s.grp == grp and s.first_day <= d0 and s.last_day >= d1:
                    box = (s.west, s.south, s.east, s.north)
                    ov = _area(_intersect(requested, box))
                    if ov > best_area:
                        best, best_area = box, ov
            if best is not None:
                years.append((first.year - k, a, best))
        boxes = requested
        for _, _, box in years:
            boxes = _intersect(boxes, box)
        area_fraction = _area(boxes) / req_area if req_area else 0.0
        g: dict = {"label": label, "seasons": 0, "area_fraction": round(area_fraction, 3)}
        out["groups"][grp] = g
        if not years:
            g["verdict"] = "no_archive"
            continue
        if area_fraction < MIN_AREA_FRACTION:
            g["verdict"] = "area_not_covered"
            continue
        past = [(y, a, _past_daily(base, grp, a, n_days, boxes)) for y, a, _ in years]
        # The current period, day by day: from the archive where it covers the day and the whole
        # area (the same product as the past seasons), else from the live database where that day is
        # loaded. Days neither has make the period incomplete (they would read as quiet days).
        area_used = _area(boxes)
        d0 = day_no(first)
        in_archive = [any(s.grp == grp and s.first_day <= d0 + i <= s.last_day
                          and _area(_intersect(boxes, (s.west, s.south, s.east, s.north))) >= area_used - 1e-9 for s in sources)
                      for i in range(n_days)]
        arch = _past_daily(base, grp, first, n_days, boxes) if any(in_archive) else [0] * n_days
        live_daily = (_current_daily(live, grp, first, n_days, boxes, _static_sites(base, boxes))
                      if not all(in_archive) else [0] * n_days)
        loaded = [True] * n_days if assume_loaded else [d["status"] == "loaded" for d in day_status(
            live, first, last, None if bbox == WORLD else bbox if bbox[0] <= bbox[2] else None)]
        cur_daily = [arch[i] if in_archive[i] else live_daily[i] for i in range(n_days)]
        missing = sum(1 for i in range(n_days) if not in_archive[i] and not loaded[i])
        complete = missing == 0
        totals = [sum(p) for _, _, p in past]
        current = sum(cur_daily)
        g.update({
            "area": [list(b) for b in boxes],
            "seasons": len(past),
            "current": current,
            "current_complete": complete,
            "current_days": {"archive": sum(in_archive), "live": sum(1 for i in range(n_days) if not in_archive[i] and loaded[i]),
                             "missing": missing},
            "years": [{"year": y, "first": a.isoformat(), "detections": t} for (y, a, _), t in zip(past, totals)],
            "median": statistics.median(totals),
            "min": min(totals), "max": max(totals),
            "ratio": round(current / statistics.median(totals), 2) if statistics.median(totals) else None,
            "rank": 1 + sum(1 for t in totals if t > current),  # 1 = more than every past season
            "verdict": verdict(current, totals) if complete else "current_incomplete",
            "daily": [{"date": (first + timedelta(days=i)).isoformat(), "current": cur_daily[i],
                       "median": statistics.median(p[i] for _, _, p in past),
                       "min": min(p[i] for _, _, p in past), "max": max(p[i] for _, _, p in past)} for i in range(n_days)],
        })
        any_group = True
    usable = [grp for grp in HEADLINE_ORDER if out["groups"].get(grp, {}).get("seasons", 0) >= MIN_SEASONS
              and out["groups"][grp].get("verdict") not in ("area_not_covered", "no_archive")]
    out["headline"] = usable[0] if usable else None
    out["available"] = any_group
    if not any_group:
        reasons = Counter(g.get("verdict") for g in out["groups"].values())
        out["reason"] = "area_not_covered" if reasons.get("area_not_covered") else "no_archive"
    return out
