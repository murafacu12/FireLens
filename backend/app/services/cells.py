"""The zoom pyramid: pre-aggregated fire cells and the map tiles built from them.

How the globe stays fast worldwide (the same idea NASA FIRMS uses with its map tiles): the work is
done once, when data arrives, not every time the camera moves.

* `fire_cells` holds detections summed per UTC hour x grid cell x sensor x confidence class, for
  cell levels 4..9 (11.25 deg .. 0.35 deg). An ingest only recomputes the hours it touched.
* The map asks for fixed tiles (zoom z, column x, row y). A tile is a 16 x 16 grid of level z+4
  cells. Zoomed out, a tile is one indexed read of `fire_cells`; at finer levels (10, 11) cells are
  summed on the fly from `detections`, which is cheap because the tile is small; zoomed in to
  ~150 km tiles (POINT_ZOOM) the tile carries the individual detections and fire events.
* Tiles are identical for every viewer and change only when data changes, so they are cached
  (server LRU + HTTP ETag) and the browser only downloads tiles it has never seen.

Tile grid: geographic (EPSG:4326). Zoom z has 2^(z+1) x 2^z tiles of 180/2^z degrees; x counts
east from -180, y counts north from -90.
"""

from collections import defaultdict
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta

from sqlalchemy import Integer, case, cast, delete, func, insert, select
from sqlalchemy.orm import Session

from app.models import AppMetaRow, DetectionRow, FireCellRow
from app.schemas import CONFIDENCE_ORDER
from app.services import queries

TILE_SHIFT = 4  # a tile is 2^4 = 16 cells across
CELLS_PER_TILE = 1 << TILE_SHIFT
MIN_LEVEL = 4  # 11.25 deg cells: two tiles cover the world
MAX_STORED_LEVEL = 9  # 0.35 deg; finer cells are computed from detections per tile
MAX_CELL_LEVEL = 11  # 0.088 deg (~10 km); closer than that the map shows the detections themselves
MAX_CELL_ZOOM = MAX_CELL_LEVEL - TILE_SHIFT
POINT_ZOOM = 7  # 1.40625 deg (~150 km) tiles carry individual detections + events
POINT_TILE_LIMIT = 20_000
_INSERT_STEP = 5000
_HOUR_CHUNK = 400


def cell_deg(level: int) -> float:
    return 180.0 / (1 << level)


def tile_deg(z: int) -> float:
    return 180.0 / (1 << z)


def tile_bbox(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    s = tile_deg(z)
    return (-180 + x * s, -90 + y * s, -180 + (x + 1) * s, -90 + (y + 1) * s)


def valid_tile(z: int, x: int, y: int) -> bool:
    return 0 <= x < (2 << z) and 0 <= y < (1 << z)


def epoch_hour(dt: datetime) -> int:
    """Whole hours since 1970-01-01 UTC (naive datetimes are UTC, like the database)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() // 3600)


def hour_start(h: int) -> datetime:
    return datetime(1970, 1, 1) + timedelta(hours=h)


def resolve_hours(hours: int | None, start: datetime | None, end: datetime | None, now: datetime) -> tuple[int, int]:
    """Inclusive hour range for a query. Rolling windows snap to whole UTC hours, so every viewer of
    "last 24 h" asks for the same tiles during an hour (and the caches can share them)."""
    if hours:
        h1 = epoch_hour(now)
        return h1 - hours, h1
    h0 = epoch_hour(start) if start else 0
    h1 = epoch_hour(end) if end else epoch_hour(now)
    return h0, h1


# ------------------------------------------------------------------ data version


def data_version(session: Session) -> int:
    row = session.get(AppMetaRow, "data_version")
    return int(row.value) if row else 0


def bump_data_version(session: Session) -> int:
    v = data_version(session) + 1
    session.merge(AppMetaRow(key="data_version", value=str(v)))
    return v


# ------------------------------------------------------------------ maintenance


def _index(session: Session, expr):
    # SQLite's CAST truncates, which is floor() here because the shifted value is >= 0.
    if session.get_bind().dialect.name == "postgresql":
        return cast(func.floor(expr), Integer)
    return cast(expr, Integer)


def refresh_cells(session: Session, hours: Iterable[int]) -> int:
    """Recompute every level of `fire_cells` for the given hours from `detections`.

    Recomputing whole hours (instead of adding increments) keeps the sums exact when an archive
    (SP) row replaces its NRT twin or a detection gets flagged as a gas flare. One SQL GROUP BY at
    the finest stored level; coarser levels are rolled up from it (a parent cell is cx >> 1).
    Returns the number of cell rows written.
    """
    hours = sorted(set(hours))
    written = 0
    c = cell_deg(MAX_STORED_LEVEL)
    gx = _index(session, (DetectionRow.longitude + 180) / c).label("gx")
    gy = _index(session, (DetectionRow.latitude + 90) / c).label("gy")
    static = case((DetectionRow.static_kind.is_(None), 0), else_=1).label("st")
    max_x, max_y = (2 << MAX_STORED_LEVEL) - 1, (1 << MAX_STORED_LEVEL) - 1
    for i in range(0, len(hours), _HOUR_CHUNK):
        chunk = hours[i : i + _HOUR_CHUNK]
        session.execute(delete(FireCellRow).where(FireCellRow.hour.in_(chunk)))
        stmt = (
            select(
                DetectionRow.acq_hour, gx, gy, DetectionRow.instrument, DetectionRow.confidence_level, static,
                func.count().label("n"),
                func.coalesce(func.sum(DetectionRow.frp_mw), 0.0).label("fs"),
                func.coalesce(func.max(DetectionRow.frp_mw), 0.0).label("fm"),
                func.sum(DetectionRow.latitude).label("ls"),
                func.sum(DetectionRow.longitude).label("os"),
            )
            .where(DetectionRow.acq_hour.in_(chunk))
            .group_by(DetectionRow.acq_hour, gx, gy, DetectionRow.instrument, DetectionRow.confidence_level, static)
        )
        level: dict[tuple, list] = defaultdict(lambda: [0, 0.0, 0.0, 0.0, 0.0])
        for r in session.execute(stmt):
            # lon = 180 / lat = 90 exactly would index one past the last cell
            key = (r.acq_hour, min(int(r.gx), max_x), min(int(r.gy), max_y), r.instrument, r.confidence_level, bool(r.st))
            _add(level[key], r.n, r.fs, r.fm, r.ls, r.os)
        rows = []
        for k in range(MAX_STORED_LEVEL, MIN_LEVEL - 1, -1):
            if k < MAX_STORED_LEVEL:
                parent: dict[tuple, list] = defaultdict(lambda: [0, 0.0, 0.0, 0.0, 0.0])
                for (h, cx, cy, ins, conf, st), v in level.items():
                    _add(parent[(h, cx >> 1, cy >> 1, ins, conf, st)], *v)
                level = parent
            for (h, cx, cy, ins, conf, st), (n, fs, fm, ls, os_) in level.items():
                rows.append({
                    "level": k, "tx": cx >> TILE_SHIFT, "ty": cy >> TILE_SHIFT, "hour": h, "cx": cx, "cy": cy,
                    "instrument": ins, "confidence_level": conf, "is_static": st,
                    "n": n, "frp_sum": fs, "frp_max": fm, "lat_sum": ls, "lon_sum": os_,
                })
        for j in range(0, len(rows), _INSERT_STEP):
            session.execute(insert(FireCellRow.__table__), rows[j : j + _INSERT_STEP])
        written += len(rows)
    return written


def _add(acc: list, n, fs, fm, ls, os_) -> None:
    acc[0] += n
    acc[1] += fs or 0.0
    acc[2] = max(acc[2], fm or 0.0)
    acc[3] += ls
    acc[4] += os_


def rebuild_all_cells(session: Session) -> int:
    session.execute(delete(FireCellRow))
    hours = [h for (h,) in session.execute(select(DetectionRow.acq_hour).distinct())]
    return refresh_cells(session, hours)


# ------------------------------------------------------------------ tiles


def _empty_cells() -> dict[str, list]:
    return {k: [] for k in ("c", "vn", "vsum", "vmax", "vlat", "vlon", "mn", "msum", "mmax", "mlat", "mlon")}


def _pack_cells(z: int, x: int, y: int, acc: dict[tuple[int, int], dict[str, list]]) -> dict:
    """Columnar cells, MODIS and VIIRS kept apart: the client merges them for the sensors switched on."""
    level = z + TILE_SHIFT
    out = _empty_cells()
    totals = {"VIIRS": 0, "MODIS": 0}
    x0, y0 = x << TILE_SHIFT, y << TILE_SHIFT
    for (cx, cy), by_ins in sorted(acc.items()):
        out["c"].append((cy - y0) * CELLS_PER_TILE + (cx - x0))
        for ins, p in (("VIIRS", "v"), ("MODIS", "m")):
            n, fs, fm, ls, os_ = by_ins.get(ins, (0, 0.0, 0.0, 0.0, 0.0))
            totals[ins] += n
            out[f"{p}n"].append(n)
            out[f"{p}sum"].append(round(fs, 1))
            out[f"{p}max"].append(round(fm, 1))
            out[f"{p}lat"].append(round(ls / n, 4) if n else 0)
            out[f"{p}lon"].append(round(os_ / n, 4) if n else 0)
    return {
        "kind": "cells", "z": z, "x": x, "y": y, "level": level, "cell_deg": cell_deg(level),
        "viirs_total": totals["VIIRS"], "modis_total": totals["MODIS"], "cells": out,
    }


def _allowed_confidence(min_confidence: str | None) -> list[str]:
    floor = CONFIDENCE_ORDER[min_confidence] if min_confidence else 0
    return [k for k, v in CONFIDENCE_ORDER.items() if v >= floor]


def needs_raw(f: queries.DetectionFilter) -> bool:
    """Filters the aggregates can't answer (they are summed per sensor + confidence class only)."""
    return bool(f.satellites or f.daynight or f.min_frp is not None or f.max_frp is not None or f.event_id
                or (f.area is not None and f.area.has_radius))


def stored_cell_tile(session: Session, z: int, x: int, y: int, h0: int, h1: int, min_confidence: str | None,
                     include_static: bool) -> dict:
    level = z + TILE_SHIFT
    stmt = select(
        FireCellRow.cx, FireCellRow.cy, FireCellRow.instrument,
        func.sum(FireCellRow.n).label("n"),
        func.sum(FireCellRow.frp_sum).label("fs"),
        func.max(FireCellRow.frp_max).label("fm"),
        func.sum(FireCellRow.lat_sum).label("ls"),
        func.sum(FireCellRow.lon_sum).label("os"),
    ).where(
        FireCellRow.level == level, FireCellRow.tx == x, FireCellRow.ty == y,
        FireCellRow.hour >= h0, FireCellRow.hour <= h1,
        FireCellRow.confidence_level.in_(_allowed_confidence(min_confidence)),
    )
    if not include_static:
        stmt = stmt.where(FireCellRow.is_static.is_(False))
    stmt = stmt.group_by(FireCellRow.cx, FireCellRow.cy, FireCellRow.instrument)
    acc: dict[tuple[int, int], dict] = defaultdict(dict)
    for r in session.execute(stmt):
        acc[(r.cx, r.cy)][r.instrument] = (r.n, r.fs, r.fm, r.ls, r.os)
    return _pack_cells(z, x, y, acc)


def raw_cell_tile(session: Session, z: int, x: int, y: int, f: queries.DetectionFilter) -> dict:
    """Same cells, summed straight from `detections` (finer levels, or filters the pyramid lacks)."""
    level = z + TILE_SHIFT
    c = cell_deg(level)
    gx = _index(session, (DetectionRow.longitude + 180) / c).label("gx")
    gy = _index(session, (DetectionRow.latitude + 90) / c).label("gy")
    stmt = select(
        gx, gy, DetectionRow.instrument,
        func.count().label("n"),
        func.coalesce(func.sum(DetectionRow.frp_mw), 0.0).label("fs"),
        func.coalesce(func.max(DetectionRow.frp_mw), 0.0).label("fm"),
        func.sum(DetectionRow.latitude).label("ls"),
        func.sum(DetectionRow.longitude).label("os"),
    )
    stmt = queries._where_detections(stmt, _tile_filter(f, z, x, y)).group_by(gx, gy, DetectionRow.instrument)
    x0, y0 = x << TILE_SHIFT, y << TILE_SHIFT
    acc: dict[tuple[int, int], dict] = defaultdict(dict)
    for r in session.execute(stmt):
        cx = min(int(r.gx), x0 + CELLS_PER_TILE - 1)
        cy = min(int(r.gy), y0 + CELLS_PER_TILE - 1)
        if cx < x0 or cy < y0:  # on the west/south edge line: belongs to the neighbour tile
            continue
        acc[(cx, cy)][r.instrument] = (r.n, r.fs, r.fm, r.ls, r.os)
    return _pack_cells(z, x, y, acc)


def _tile_filter(f: queries.DetectionFilter, z: int, x: int, y: int) -> queries.DetectionFilter:
    area = f.area or queries.Area()
    tile_area = queries.Area(bbox=tile_bbox(z, x, y), lat=area.lat, lon=area.lon, radius_km=area.radius_km)
    return queries.DetectionFilter(**{**vars(f), "area": tile_area, "instruments": None})


def _inside_tile(lat: float, lon: float, bbox) -> bool:
    w, s, e, n = bbox  # half-open, so a point on a tile edge is drawn once
    return (s <= lat < n or (n >= 90 and lat == 90)) and (w <= lon < e or (e >= 180 and lon == 180))


def point_tile(session: Session, x: int, y: int, f: queries.DetectionFilter, ef: queries.EventFilter) -> dict:
    """Zoomed in: the tile's individual detections (columnar, `viirs` 1/0 per point) and fire events."""
    z = POINT_ZOOM
    bbox = tile_bbox(z, x, y)
    tf = _tile_filter(f, z, x, y)
    tf.limit = POINT_TILE_LIMIT
    tf.order = "frp"  # strongest first if the cap is hit; also keeps the query on the lat/lon index
    pts = queries.query_detections_compact(session, tf)
    keep = [i for i in range(pts["count"]) if _inside_tile(pts["lat"][i], pts["lon"][i], bbox)]
    if len(keep) != pts["count"]:
        pts = {k: ([v[i] for i in keep] if isinstance(v, list) else v) for k, v in pts.items()}
        pts["count"] = len(keep)
    ef = queries.EventFilter(**{**vars(ef), "area": queries.Area(bbox=bbox, lat=ef.area.lat if ef.area else None,
                                                                   lon=ef.area.lon if ef.area else None,
                                                                   radius_km=ef.area.radius_km if ef.area else None),
                                 "instrument": None, "limit": 5000})
    events = [e.model_dump(mode="json") for e in queries.query_events(session, ef) if _inside_tile(e.latitude, e.longitude, bbox)]
    return {
        "kind": "points", "z": z, "x": x, "y": y,
        "truncated": pts["count"] >= POINT_TILE_LIMIT,
        "points": pts, "events": events,
    }


__all__ = [
    "CELLS_PER_TILE", "MAX_CELL_ZOOM", "MIN_LEVEL", "POINT_ZOOM", "bump_data_version",
    "data_version", "epoch_hour", "needs_raw", "point_tile", "raw_cell_tile", "rebuild_all_cells", "refresh_cells",
    "resolve_hours", "stored_cell_tile", "tile_bbox", "valid_tile",
]
