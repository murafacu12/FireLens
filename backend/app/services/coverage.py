"""Which days have been loaded, for which area.

A day with zero detections may mean "no fires" or "never fetched". Every ingestion records
what it actually fetched (day x area x product); the map asks, per day, how much of the
current view is covered and offers to load the archive for what is missing.
"""

from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.models import CoverageRow

BBox = tuple[float, float, float, float]
WORLD: BBox = (-180.0, -90.0, 180.0, 90.0)
LOADED_FRACTION = 0.8  # a day counts as loaded when this much of the view is covered


def days_between(first: date, last: date) -> list[date]:
    return [first + timedelta(days=i) for i in range((last - first).days + 1)]


def record_coverage(session: Session, days: Iterable[date], bbox: BBox, source: str) -> None:
    rows = [
        {"day": d, "west": bbox[0], "south": bbox[1], "east": bbox[2], "north": bbox[3], "source": source,
         "created_at": datetime.now(UTC).replace(tzinfo=None)}
        for d in days
    ]
    if not rows:
        return
    insert = pg_insert if session.get_bind().dialect.name == "postgresql" else sqlite_insert
    session.execute(insert(CoverageRow).values(rows).on_conflict_do_nothing())


def _split(b: BBox) -> list[BBox]:
    """Boxes crossing the antimeridian (west > east) become two ordinary boxes."""
    w, s, e, n = b
    return [b] if w <= e else [(w, s, 180.0, n), (-180.0, s, e, n)]


def _area(b: BBox) -> float:
    return max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])


def _overlap(a: BBox, b: BBox) -> float:
    return _area((max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])))


def covered_fraction(view: BBox, boxes: list[BBox]) -> float:
    """Share of `view` inside the best single coverage box (a cheap lower bound of the union)."""
    parts = _split(view)
    total = sum(_area(p) for p in parts) or 1.0
    best = 0.0
    for box in boxes:
        for b in _split(box):
            best = max(best, sum(_overlap(p, b) for p in parts) / total)
    return min(1.0, best)


def day_status(session: Session, start: date, end: date, view: BBox | None) -> list[dict]:
    view = view or WORLD
    by_day: dict[date, list[BBox]] = {}
    for r in session.execute(
        select(CoverageRow.day, CoverageRow.west, CoverageRow.south, CoverageRow.east, CoverageRow.north).where(
            CoverageRow.day >= start, CoverageRow.day <= end
        )
    ):
        by_day.setdefault(r.day, []).append((r.west, r.south, r.east, r.north))
    out = []
    for d in days_between(start, end):
        frac = covered_fraction(view, by_day.get(d, []))
        status = "loaded" if frac >= LOADED_FRACTION else "partial" if frac > 0 else "missing"
        out.append({"date": d.isoformat(), "status": status, "covered": round(frac, 3)})
    return out
