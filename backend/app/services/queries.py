"""Read-side queries with the filters exposed by the API."""

import math
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import Integer, Select, case, cast, func, or_, select
from sqlalchemy.orm import Session

from app.ingest.clustering import haversine_km
from app.models import DetectionRow, EventLineageRow, EventRow, FireCellRow
from app.schemas import CONFIDENCE_ORDER, CalendarDay, FireDetection, FireEvent, to_naive_utc

BBox = tuple[float, float, float, float]  # west, south, east, north


def parse_bbox(value: str | None) -> BBox | None:
    if not value:
        return None
    try:
        w, s, e, n = (float(p) for p in value.split(","))
    except ValueError as exc:
        raise ValueError("bbox must be 'west,south,east,north'") from exc
    if not (-180 <= w <= 180 and -180 <= e <= 180 and -90 <= s <= n <= 90):
        raise ValueError("bbox out of range")
    return w, s, e, n


def radius_to_bbox(lat: float, lon: float, radius_km: float) -> BBox:
    dlat = radius_km / 110.574
    dlon = radius_km / (111.320 * max(math.cos(math.radians(lat)), 1e-3))
    return (max(-180, lon - dlon), max(-90, lat - dlat), min(180, lon + dlon), min(90, lat + dlat))


def _bbox_clause(stmt: Select, lat_col, lon_col, bbox: BBox) -> Select:
    w, s, e, n = bbox
    stmt = stmt.where(lat_col >= s, lat_col <= n)
    if w <= e:
        return stmt.where(lon_col >= w, lon_col <= e)
    return stmt.where(or_(lon_col >= w, lon_col <= e))  # crosses the antimeridian


def _is_whole_world(bbox: BBox) -> bool:
    w, s, e, n = bbox
    return w <= -180 and e >= 180 and s <= -90 and n >= 90


@dataclass
class Area:
    bbox: BBox | None = None
    lat: float | None = None
    lon: float | None = None
    radius_km: float | None = None

    @property
    def has_radius(self) -> bool:
        return None not in (self.lat, self.lon, self.radius_km)

    def apply(self, stmt: Select, lat_col, lon_col) -> Select:
        # A whole-world box (the zoomed-out globe sends one) filters nothing, but it steers SQLite
        # onto the lat/lon index and makes wide queries ~2x slower than a plain scan.
        if self.bbox and not _is_whole_world(self.bbox):
            stmt = _bbox_clause(stmt, lat_col, lon_col, self.bbox)
        if self.has_radius:
            stmt = _bbox_clause(stmt, lat_col, lon_col, radius_to_bbox(self.lat, self.lon, self.radius_km))
        return stmt

    def contains(self, lat: float, lon: float) -> bool:
        return not self.has_radius or haversine_km(self.lat, self.lon, lat, lon) <= self.radius_km


@dataclass
class DetectionFilter:
    start: datetime | None = None
    end: datetime | None = None
    area: Area | None = None
    instruments: list[str] | None = None
    satellites: list[str] | None = None
    min_confidence: str | None = None
    min_frp: float | None = None
    max_frp: float | None = None
    daynight: str | None = None
    event_id: str | None = None
    include_static: bool = False  # volcanoes, gas flares, offshore platforms
    limit: int = 5000
    # "recent" (newest first) or "frp" (strongest first). Map tiles use "frp": the strongest fires are
    # the ones to keep when a tile hits its cap, and it lets SQLite use the lat/lon index instead of
    # walking the time index of the whole world to satisfy ORDER BY acquired_at.
    order: str = "recent"


def _where_detections(stmt: Select, f: DetectionFilter) -> Select:
    if not f.include_static:
        stmt = stmt.where(DetectionRow.static_kind.is_(None))
    if f.start:
        stmt = stmt.where(DetectionRow.acquired_at >= to_naive_utc(f.start))
    if f.end:
        stmt = stmt.where(DetectionRow.acquired_at <= to_naive_utc(f.end))
    if f.area:
        stmt = f.area.apply(stmt, DetectionRow.latitude, DetectionRow.longitude)
    if f.instruments:
        stmt = stmt.where(DetectionRow.instrument.in_([i.upper() for i in f.instruments]))
    if f.satellites:
        stmt = stmt.where(DetectionRow.satellite.in_(f.satellites))
    if f.min_confidence:
        allowed = [k for k, v in CONFIDENCE_ORDER.items() if v >= CONFIDENCE_ORDER[f.min_confidence]]
        stmt = stmt.where(DetectionRow.confidence_level.in_(allowed))
    if f.min_frp is not None:
        stmt = stmt.where(DetectionRow.frp_mw >= f.min_frp)
    if f.max_frp is not None:
        stmt = stmt.where(DetectionRow.frp_mw <= f.max_frp)
    if f.daynight:
        stmt = stmt.where(DetectionRow.daynight == f.daynight.upper())
    if f.event_id:
        stmt = stmt.where(DetectionRow.event_id == f.event_id)
    return stmt


def _filter_detections(stmt: Select, f: DetectionFilter) -> Select:
    key = DetectionRow.frp_mw.desc() if f.order == "frp" else DetectionRow.acquired_at.desc()
    stmt = _where_detections(stmt, f).order_by(key)
    # With a radius the bbox pre-filter over-selects, so the limit is applied after the exact check.
    if not (f.area is not None and f.area.has_radius):
        stmt = stmt.limit(f.limit)
    return stmt


def query_detections(session: Session, f: DetectionFilter) -> list[FireDetection]:
    stmt = _filter_detections(select(DetectionRow), f)
    radius = f.area is not None and f.area.has_radius
    out = []
    for row in session.scalars(stmt):
        if radius and not f.area.contains(row.latitude, row.longitude):
            continue
        out.append(FireDetection.model_validate(row))
        if len(out) >= f.limit:
            break
    return out


CONFIDENCE_CODE = {"low": 0, "nominal": 1, "high": 2}


def query_detections_compact(session: Session, f: DetectionFilter) -> dict:
    """Columnar, render-only view of detections for the globe.

    ~10x smaller than the full model and skips ORM/pydantic per row. Full records are
    fetched one at a time (GET /api/detections/{id}) when the user clicks a point.
    """
    cols = (
        DetectionRow.id,
        DetectionRow.latitude,
        DetectionRow.longitude,
        DetectionRow.frp_mw,
        DetectionRow.confidence_level,
        DetectionRow.instrument,
        DetectionRow.acquired_at,
    )
    stmt = _filter_detections(select(*cols), f)
    radius = f.area is not None and f.area.has_radius
    ids, lat, lon, frp, conf, viirs, t = [], [], [], [], [], [], []
    for r in session.execute(stmt):
        if radius and not f.area.contains(r.latitude, r.longitude):
            continue
        ids.append(r.id)
        lat.append(round(r.latitude, 4))
        lon.append(round(r.longitude, 4))
        frp.append(round(r.frp_mw, 1) if r.frp_mw is not None else None)
        conf.append(CONFIDENCE_CODE.get(r.confidence_level, 1))
        viirs.append(1 if r.instrument == "VIIRS" else 0)
        t.append(int(r.acquired_at.replace(tzinfo=UTC).timestamp()))
        if len(ids) >= f.limit:
            break
    return {"count": len(ids), "id": ids, "lat": lat, "lon": lon, "frp": frp, "conf": conf, "viirs": viirs, "t": t}


def query_detection_grid(session: Session, f: DetectionFilter, cell_deg: float) -> dict:
    """Aggregate detections into lat/lon cells for the zoomed-out map (grouped markers).

    Each cell reports its detection count, intensity and a centroid (mean position of its
    detections, so a bubble sits on the fires, not on an arbitrary grid corner). With a
    radius filter the cells use its bounding box (approximate at the circle's edge).
    """
    is_pg = session.get_bind().dialect.name == "postgresql"

    def index(expr):
        # SQLite's CAST truncates, which is floor() here because the shifted value is >= 0.
        return func.floor(expr) if is_pg else cast(expr, Integer)

    gy = index((DetectionRow.latitude + 90) / cell_deg).label("gy")
    gx = index((DetectionRow.longitude + 180) / cell_deg).label("gx")
    stmt = select(
        gy,
        gx,
        func.count().label("n"),
        func.avg(DetectionRow.latitude).label("lat"),
        func.avg(DetectionRow.longitude).label("lon"),
        func.max(DetectionRow.frp_mw).label("max_frp"),
        func.sum(DetectionRow.frp_mw).label("sum_frp"),
        func.sum(case((DetectionRow.instrument == "VIIRS", 1), else_=0)).label("viirs"),
    )
    stmt = _where_detections(stmt, f).group_by(gy, gx)
    cells: dict[str, list] = {"lat": [], "lon": [], "count": [], "max_frp": [], "sum_frp": [], "viirs": []}
    total = 0
    for r in session.execute(stmt):
        total += r.n
        cells["lat"].append(round(r.lat, 4))
        cells["lon"].append(round(r.lon, 4))
        cells["count"].append(r.n)
        cells["max_frp"].append(round(r.max_frp, 1) if r.max_frp is not None else None)
        cells["sum_frp"].append(round(r.sum_frp or 0, 1))
        cells["viirs"].append(int(r.viirs or 0))
    return {"cell_deg": cell_deg, "total": total, "cells": len(cells["count"]), **cells}


@dataclass
class EventFilter:
    start: datetime | None = None
    end: datetime | None = None
    area: Area | None = None
    min_area: float | None = None
    max_area: float | None = None
    min_frp: float | None = None
    min_detections: int | None = None
    min_confidence_pct: int | None = None
    status: str | None = None
    instrument: str | None = None
    min_evidence: str | None = None  # single_detection | single_pass | multi_pass
    lifecycle_state: str | None = None
    sort: str = "recent"  # recent | area | frp
    limit: int = 1000


def _where_events(stmt: Select, f: EventFilter) -> Select:
    if f.start:  # events overlapping [start, end]
        stmt = stmt.where(EventRow.end_at >= to_naive_utc(f.start))
    if f.end:
        stmt = stmt.where(EventRow.start_at <= to_naive_utc(f.end))
    if f.area:
        stmt = f.area.apply(stmt, EventRow.latitude, EventRow.longitude)
    if f.min_area is not None:
        stmt = stmt.where(EventRow.area_km2 >= f.min_area)
    if f.max_area is not None:
        stmt = stmt.where(EventRow.area_km2 <= f.max_area)
    if f.min_frp is not None:
        stmt = stmt.where(EventRow.max_frp_mw >= f.min_frp)
    if f.min_detections is not None:
        stmt = stmt.where(EventRow.detection_count >= f.min_detections)
    if f.min_confidence_pct is not None:
        stmt = stmt.where(EventRow.max_confidence_pct >= f.min_confidence_pct)
    if f.status:
        stmt = stmt.where(EventRow.status == f.status)
    if f.lifecycle_state:
        stmt = stmt.where(EventRow.lifecycle_state == f.lifecycle_state)
    if f.min_evidence:
        stmt = stmt.where(EventRow.evidence.in_(EVIDENCE_ORDER[EVIDENCE_ORDER.index(f.min_evidence):]))
    if f.instrument == "MODIS":
        stmt = stmt.where(EventRow.modis_count > 0)
    elif f.instrument == "VIIRS":
        stmt = stmt.where(EventRow.viirs_count > 0)
    return stmt


EVIDENCE_ORDER = ["single_detection", "single_pass", "multi_pass"]


def resolve_event_id(session: Session, event_id: str, max_hops: int = 50) -> str | None:
    """Follow event_lineage from an id that is no longer canonical to the event that holds its
    detections now (merged/renamed: the target; split: the part that received most of them).
    None if the id never existed or its detections were all reclassified as non-fire."""
    seen = set()
    current = event_id
    for _ in range(max_hops):
        if session.get(EventRow, current) is not None:
            return current
        if current in seen:
            return None
        seen.add(current)
        rows = session.execute(
            select(EventLineageRow).where(EventLineageRow.old_id == current).order_by(EventLineageRow.at.desc(), EventLineageRow.id.desc())
        ).scalars().all()
        if not rows:
            return None
        latest = [r for r in rows if r.at == rows[0].at]
        best = max(latest, key=lambda r: (r.detections_moved, r.new_id or ""))
        if best.new_id is None:
            return None
        current = best.new_id
    return None


def count_events(session: Session, f: EventFilter) -> int:
    """Number of events matching the filters (radius filter counted on its bounding box)."""
    return session.scalar(_where_events(select(func.count()).select_from(EventRow), f)) or 0


def query_events(session: Session, f: EventFilter) -> list[FireEvent]:
    stmt = _where_events(select(EventRow), f)
    order = {
        "area": EventRow.area_km2.desc(),
        "frp": EventRow.max_frp_mw.desc(),
    }.get(f.sort, EventRow.end_at.desc())
    stmt = stmt.order_by(order)
    radius = f.area is not None and f.area.has_radius
    if not radius:
        stmt = stmt.limit(f.limit)
    out = []
    for row in session.scalars(stmt):
        if radius and not f.area.contains(row.latitude, row.longitude):
            continue
        out.append(FireEvent.model_validate(row))
        if len(out) >= f.limit:
            break
    return out


CALENDAR_LEVEL = 4  # coarsest fire_cells level (cells.MIN_LEVEL)


def calendar(session: Session, start: date, end: date, area: Area | None = None) -> list[CalendarDay]:
    """Daily activity between start and end (inclusive), zero-filled."""
    t0 = datetime.combine(start, datetime.min.time())
    t1 = datetime.combine(end + timedelta(days=1), datetime.min.time())

    ev_day = func.date(EventRow.start_at)
    ev_stmt = select(ev_day.label("day"), func.count().label("n")).where(
        EventRow.start_at >= t0, EventRow.start_at < t1
    )
    if area:
        day = func.date(DetectionRow.acquired_at)
        det_stmt = select(
            day.label("day"),
            func.count().label("n"),
            func.coalesce(func.sum(DetectionRow.frp_mw), 0).label("total"),
            func.coalesce(func.max(DetectionRow.frp_mw), 0).label("peak"),
        ).where(DetectionRow.acquired_at >= t0, DetectionRow.acquired_at < t1, DetectionRow.static_kind.is_(None))
        det_stmt = area.apply(det_stmt, DetectionRow.latitude, DetectionRow.longitude)
        ev_stmt = area.apply(ev_stmt, EventRow.latitude, EventRow.longitude)
        dets = {str(r.day): r for r in session.execute(det_stmt.group_by(day))}
    else:
        # Worldwide: read the coarsest level of the map pyramid (a few thousand rows per month
        # instead of millions of detections).
        h0 = int(t0.replace(tzinfo=UTC).timestamp() // 3600)
        h1 = int(t1.replace(tzinfo=UTC).timestamp() // 3600)
        dnum = (FireCellRow.hour / 24).label("dnum")
        stmt = (
            select(
                dnum,
                func.sum(FireCellRow.n).label("n"),
                func.sum(FireCellRow.frp_sum).label("total"),
                func.max(FireCellRow.frp_max).label("peak"),
            )
            .where(FireCellRow.level == CALENDAR_LEVEL, FireCellRow.hour >= h0, FireCellRow.hour < h1,
                   FireCellRow.is_static.is_(False))
            .group_by(dnum)
        )
        epoch = date(1970, 1, 1)
        dets = {(epoch + timedelta(days=int(r.dnum))).isoformat(): r for r in session.execute(stmt)}
    evs = {str(r.day): r.n for r in session.execute(ev_stmt.group_by(ev_day))}

    out = []
    d = start
    while d <= end:
        key = d.isoformat()
        r = dets.get(key)
        out.append(
            CalendarDay(
                date=key,
                detections=r.n if r else 0,
                events_started=evs.get(key, 0),
                total_frp_mw=round(float(r.total), 1) if r else 0.0,
                max_frp_mw=round(float(r.peak), 1) if r else 0.0,
            )
        )
        d += timedelta(days=1)
    return out


def nearby_activity(session: Session, lat: float, lon: float, radius_km: float, since: datetime, until: datetime) -> int:
    area = Area(lat=lat, lon=lon, radius_km=radius_km)
    stmt = select(DetectionRow.latitude, DetectionRow.longitude).where(
        DetectionRow.acquired_at >= to_naive_utc(since),
        DetectionRow.acquired_at < to_naive_utc(until),
        DetectionRow.static_kind.is_(None),  # a gas flare next door is not fire activity
    )
    stmt = area.apply(stmt, DetectionRow.latitude, DetectionRow.longitude)
    return sum(1 for r in session.execute(stmt) if area.contains(r.latitude, r.longitude))
