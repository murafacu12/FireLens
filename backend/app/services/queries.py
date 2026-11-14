"""Read-side queries with the filters exposed by the API."""

import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import Session

from app.ingest.clustering import haversine_km
from app.models import DetectionRow, EventRow
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
        if self.bbox:
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
    limit: int = 5000


def query_detections(session: Session, f: DetectionFilter) -> list[FireDetection]:
    stmt = select(DetectionRow)
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
    stmt = stmt.order_by(DetectionRow.acquired_at.desc())
    radius = f.area is not None and f.area.has_radius
    if not radius:
        stmt = stmt.limit(f.limit)
    out = []
    for row in session.scalars(stmt):
        if radius and not f.area.contains(row.latitude, row.longitude):
            continue
        out.append(FireDetection.model_validate(row))
        if len(out) >= f.limit:
            break
    return out


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
    sort: str = "recent"  # recent | area | frp
    limit: int = 1000


def query_events(session: Session, f: EventFilter) -> list[FireEvent]:
    stmt = select(EventRow)
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
    if f.instrument == "MODIS":
        stmt = stmt.where(EventRow.modis_count > 0)
    elif f.instrument == "VIIRS":
        stmt = stmt.where(EventRow.viirs_count > 0)
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


def calendar(session: Session, start: date, end: date, area: Area | None = None) -> list[CalendarDay]:
    """Daily activity between start and end (inclusive), zero-filled."""
    t0 = datetime.combine(start, datetime.min.time())
    t1 = datetime.combine(end + timedelta(days=1), datetime.min.time())

    day = func.date(DetectionRow.acquired_at)
    det_stmt = select(
        day.label("day"),
        func.count().label("n"),
        func.coalesce(func.sum(DetectionRow.frp_mw), 0).label("total"),
        func.coalesce(func.max(DetectionRow.frp_mw), 0).label("peak"),
    ).where(DetectionRow.acquired_at >= t0, DetectionRow.acquired_at < t1)
    ev_day = func.date(EventRow.start_at)
    ev_stmt = select(ev_day.label("day"), func.count().label("n")).where(
        EventRow.start_at >= t0, EventRow.start_at < t1
    )
    if area:
        det_stmt = area.apply(det_stmt, DetectionRow.latitude, DetectionRow.longitude)
        ev_stmt = area.apply(ev_stmt, EventRow.latitude, EventRow.longitude)
    dets = {str(r.day): r for r in session.execute(det_stmt.group_by(day))}
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
        DetectionRow.acquired_at >= to_naive_utc(since), DetectionRow.acquired_at < to_naive_utc(until)
    )
    stmt = area.apply(stmt, DetectionRow.latitude, DetectionRow.longitude)
    return sum(1 for r in session.execute(stmt) if area.contains(r.latitude, r.longitude))
