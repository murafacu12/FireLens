"""Fire statistics and trends for an area and a period.

Read-only, and built on tables that already exist: the map pyramid (`fire_cells`, hourly sums per
cell x sensor x confidence) for wide areas, `detections` for small ones, and `fire_events`.

What the numbers mean (and don't):
* a *detection* is one satellite pixel that saw fire on one overpass. More satellites or more
  overpasses give more detections of the same fire, so counts measure observed fire activity,
  not the number of fires and not burned area;
* FRP is instantaneous power. It is never summed over time here: the report gives the strongest
  pixel and the mean pixel FRP per sensor (MODIS and VIIRS are not cross-calibrated);
* a comparison with the previous period is only as good as the data loaded for both, so each
  period reports how much of it is loaded (`coverage`).
"""

import math
import statistics
from collections import Counter, defaultdict
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models import DetectionRow, EventRow, FireCellRow
from app.schemas import FireEvent
from app.services import cells
from app.services.coverage import WORLD, BBox, day_status

RAW_MAX_SPAN_DEG = 3.0  # smaller areas are counted exactly from detections
MAX_CELLS_ACROSS = 256  # wider areas use a pyramid level with at most this many cells across
MAX_DAYS = 400
EPOCH = date(1970, 1, 1)
SENSORS = ("VIIRS", "MODIS")


def _span(b: BBox) -> tuple[float, float]:
    w, s, e, n = b
    return (e - w) % 360 or (360.0 if e != w else 0.0), n - s


def _lon_ranges(w: float, e: float) -> list[tuple[float, float]]:
    return [(w, e)] if w <= e else [(w, 180.0), (-180.0, e)]


def _hourly(session: Session, bbox: BBox, h0: int, h1: int) -> tuple[dict, str]:
    """{(hour, sensor, confidence): [n, frp_sum, frp_max]} for hours h0..h1 (inclusive), plus the
    method used: 'detections' (exact) or 'cells_<deg>' (cells whose centre is in the area)."""
    out: dict[tuple, list] = defaultdict(lambda: [0, 0.0, 0.0])
    width, height = _span(bbox)
    w, s, e, n = bbox
    if max(width, height) <= RAW_MAX_SPAN_DEG:
        stmt = select(
            DetectionRow.acq_hour, DetectionRow.instrument, DetectionRow.confidence_level, func.count(),
            func.coalesce(func.sum(DetectionRow.frp_mw), 0.0), func.coalesce(func.max(DetectionRow.frp_mw), 0.0),
        ).where(
            DetectionRow.acq_hour >= h0, DetectionRow.acq_hour <= h1, DetectionRow.static_kind.is_(None),
            DetectionRow.latitude >= s, DetectionRow.latitude <= n,
            or_(*[(DetectionRow.longitude >= a) & (DetectionRow.longitude <= b) for a, b in _lon_ranges(w, e)]),
        ).group_by(DetectionRow.acq_hour, DetectionRow.instrument, DetectionRow.confidence_level)
        for h, ins, conf, cnt, fs, fm in session.execute(stmt):
            out[(h, ins, conf)] = [cnt, fs, fm]
        return out, "detections"
    level = cells.MAX_STORED_LEVEL
    while level > cells.MIN_LEVEL and max(width, height) / cells.cell_deg(level) > MAX_CELLS_ACROSS:
        level -= 1
    c = cells.cell_deg(level)
    # cells whose centre lies in the area: centre = -180 + (cx + 0.5) * c
    y0, y1 = math.ceil((s + 90) / c - 0.5), math.floor((n + 90) / c - 0.5)
    for a, b in _lon_ranges(w, e):
        x0, x1 = math.ceil((a + 180) / c - 0.5), math.floor((b + 180) / c - 0.5)
        if x1 < x0 or y1 < y0:
            continue
        sh = cells.TILE_SHIFT
        stmt = select(
            FireCellRow.hour, FireCellRow.instrument, FireCellRow.confidence_level, func.sum(FireCellRow.n),
            func.sum(FireCellRow.frp_sum), func.max(FireCellRow.frp_max),
        ).where(
            FireCellRow.level == level, FireCellRow.tx >= x0 >> sh, FireCellRow.tx <= x1 >> sh,
            FireCellRow.ty >= y0 >> sh, FireCellRow.ty <= y1 >> sh,
            FireCellRow.hour >= h0, FireCellRow.hour <= h1, FireCellRow.is_static.is_(False),
            FireCellRow.cx >= x0, FireCellRow.cx <= x1, FireCellRow.cy >= y0, FireCellRow.cy <= y1,
        ).group_by(FireCellRow.hour, FireCellRow.instrument, FireCellRow.confidence_level)
        for h, ins, conf, cnt, fs, fm in session.execute(stmt):
            acc = out[(h, ins, conf)]
            acc[0] += cnt
            acc[1] += fs
            acc[2] = max(acc[2], fm)
    return out, f"cells_{c:g}"


def _events_where(stmt, bbox: BBox, t0: datetime, t1: datetime):
    w, s, e, n = bbox
    return stmt.where(
        EventRow.start_at >= t0, EventRow.start_at < t1, EventRow.latitude >= s, EventRow.latitude <= n,
        or_(*[(EventRow.longitude >= a) & (EventRow.longitude <= b) for a, b in _lon_ranges(w, e)]),
    )


def _coverage(session: Session, d0: date, d1: date, bbox: BBox) -> dict:
    days = day_status(session, d0, d1, None if bbox == WORLD else bbox if bbox[0] <= bbox[2] else None)
    loaded = sum(1 for d in days if d["status"] == "loaded")
    return {"days": len(days), "loaded_days": loaded, "complete": loaded == len(days)}


def _pct(now: float, before: float) -> float | None:
    return round(100 * (now - before) / before, 1) if before else None


def analyze(session: Session, bbox: BBox | None, start: datetime, end: datetime, assume_loaded: bool = False) -> dict:
    """Statistics for [start, end) and the same-length period before it. Times are snapped to whole
    UTC hours (the resolution of the pyramid)."""
    bbox = bbox or WORLD
    h0, h1 = cells.epoch_hour(start), cells.epoch_hour(end - timedelta(seconds=1))
    n_hours = h1 - h0 + 1
    t0, t1 = cells.hour_start(h0), cells.hour_start(h1 + 1)
    hourly, method = _hourly(session, bbox, h0, h1)
    previous, _ = _hourly(session, bbox, h0 - n_hours, h0 - 1)

    # ---- totals
    by_sensor = {s: {"detections": 0, "frp_sum": 0.0, "max_frp_mw": 0.0} for s in SENSORS}
    by_conf: Counter = Counter()
    daily: dict[int, dict] = defaultdict(lambda: {"viirs": 0, "modis": 0, "max_frp_mw": 0.0})
    by_utc_hour = [[0, 0] for _ in range(24)]
    for (h, ins, conf), (cnt, fs, fm) in hourly.items():
        b = by_sensor.setdefault(ins, {"detections": 0, "frp_sum": 0.0, "max_frp_mw": 0.0})
        b["detections"] += cnt
        b["frp_sum"] += fs
        b["max_frp_mw"] = max(b["max_frp_mw"], fm)
        by_conf[conf] += cnt
        d = daily[h // 24]
        d["viirs" if ins == "VIIRS" else "modis"] += cnt
        d["max_frp_mw"] = max(d["max_frp_mw"], fm)
        by_utc_hour[h % 24][0 if ins == "VIIRS" else 1] += cnt
    total = sum(b["detections"] for b in by_sensor.values())
    prev_total = sum(v[0] for v in previous.values())

    # ---- events that started in the period
    ev_rows = session.execute(_events_where(
        select(EventRow.start_at, EventRow.evidence, EventRow.lifecycle_state), bbox, t0, t1)).all()
    ev_daily: Counter = Counter((r.start_at.date() - EPOCH).days for r in ev_rows)
    prev_events = session.scalar(_events_where(
        select(func.count()).select_from(EventRow), bbox, t0 - timedelta(hours=n_hours), t0)) or 0

    def top(order) -> list[dict]:
        rows = session.scalars(_events_where(select(EventRow), bbox, t0, t1).order_by(order.desc()).limit(5))
        return [FireEvent.model_validate(r).model_dump(mode="json", include={
            "id", "latitude", "longitude", "start_at", "end_at", "area_km2", "area_basis", "peak_pass_frp_mw",
            "peak_pass_sensor", "detection_count", "lifecycle_state", "status", "evidence", "west", "south", "east", "north"})
            for r in rows]

    # ---- daily series (zero-filled), trend inside the period, unusual days
    first_day, last_day = h0 // 24, h1 // 24
    series = []
    for dn in range(first_day, last_day + 1):
        d = daily.get(dn, {"viirs": 0, "modis": 0, "max_frp_mw": 0.0})
        series.append({"date": (EPOCH + timedelta(days=dn)).isoformat(), "viirs": d["viirs"], "modis": d["modis"],
                       "detections": d["viirs"] + d["modis"], "max_frp_mw": round(d["max_frp_mw"], 1),
                       "events_started": ev_daily.get(dn, 0)})
    counts = [d["detections"] for d in series]
    # Trend: second half of the period against the first, by hours (calendar days would be skewed
    # by the partial days at both ends of a period that doesn't start at midnight).
    trend = {"direction": "insufficient_data", "first_half": None, "second_half": None, "change_pct": None}
    if n_hours >= 48:
        mid = h0 + n_hours // 2
        first = sum(v[0] for (h, _, _), v in hourly.items() if h < mid)
        second = sum(v[0] for (h, _, _), v in hourly.items() if h >= h1 + 1 - n_hours // 2)
        change = _pct(second, first)
        trend = {"first_half": first, "second_half": second, "change_pct": change,
                 "direction": "insufficient_data" if change is None else "rising" if change >= 25 else "falling" if change <= -25 else "stable"}
    unusual = []
    if len(counts) >= 7:
        med = statistics.median(counts)
        mad = statistics.median(abs(c - med) for c in counts)
        # Robust "3 sigma": the spike itself doesn't inflate the threshold. The square-root floor is
        # the noise a count of that size has anyway, so perfectly regular days don't give a zero spread.
        limit = med + 3 * max(1.4826 * mad, math.sqrt(max(med, 1.0)))
        unusual = [{"date": d["date"], "detections": d["detections"], "typical": round(med, 1)}
                   for d in series if d["detections"] > limit]

    # ---- time of day: local solar time when the area is narrow enough for one to make sense
    width, _ = _span(bbox)
    local = width <= 45
    offset = round(((bbox[0] + width / 2 + 180) % 360 - 180) / 15) if local else 0
    by_hour = [{"hour": h, "viirs": by_utc_hour[(h - offset) % 24][0], "modis": by_utc_hour[(h - offset) % 24][1]} for h in range(24)]

    d0, d1 = EPOCH + timedelta(days=first_day), EPOCH + timedelta(days=last_day)
    prev_d0 = EPOCH + timedelta(days=(h0 - n_hours) // 24)
    prev_d1 = EPOCH + timedelta(days=(h0 - 1) // 24)
    cov, prev_cov = _coverage(session, d0, d1, bbox), _coverage(session, prev_d0, prev_d1, bbox)
    if assume_loaded:  # synthetic demo data has no coverage records
        for c in (cov, prev_cov):
            c.update(loaded_days=c["days"], complete=True)
    if not cov["complete"]:
        # Days that are not loaded look like quiet days: a "trend" or an "unusual day" would only
        # describe what happens to be loaded, so neither is stated.
        trend = {"direction": "not_comparable", "first_half": None, "second_half": None, "change_pct": None}
        unusual = []
    return {
        "bbox": list(bbox),
        "start": t0.replace(tzinfo=UTC), "end": t1.replace(tzinfo=UTC), "hours": n_hours,
        "method": method,
        "coverage": cov,
        "detections": {
            "total": total,
            "by_sensor": {s.lower(): {
                "detections": by_sensor[s]["detections"],
                "max_frp_mw": round(by_sensor[s]["max_frp_mw"], 1),
                "mean_pixel_frp_mw": round(by_sensor[s]["frp_sum"] / by_sensor[s]["detections"], 1) if by_sensor[s]["detections"] else None,
            } for s in SENSORS},
            "by_confidence": {k: by_conf.get(k, 0) for k in ("low", "nominal", "high")},
        },
        "events": {
            "started": len(ev_rows),
            "by_evidence": dict(Counter(r.evidence for r in ev_rows)),
            "by_lifecycle": dict(Counter(r.lifecycle_state for r in ev_rows)),
            "largest_footprint": top(EventRow.area_km2),
            "strongest": top(EventRow.peak_pass_frp_mw),
        },
        "previous": {
            "start": (t0 - timedelta(hours=n_hours)).replace(tzinfo=UTC), "end": t0.replace(tzinfo=UTC),
            "detections": prev_total, "events_started": prev_events, "coverage": prev_cov,
            "detections_change_pct": _pct(total, prev_total) if prev_cov["complete"] and cov["complete"] else None,
            "events_change_pct": _pct(len(ev_rows), prev_events) if prev_cov["complete"] and cov["complete"] else None,
            "comparable": prev_cov["complete"] and cov["complete"],
        },
        "daily": series,
        "trend": trend,
        "unusual_days": unusual,
        "time_of_day": {"basis": "local_solar" if local else "utc", "utc_offset_hours": offset, "hours": by_hour},
    }
