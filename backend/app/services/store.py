"""Persistence of detections and incremental (re)building of fire events.

Worldwide data is ~120k detections per day. Two rules keep updates proportional to what
actually changed instead of to the size of the database:

* upserts only write new detections (and SP rows replacing their NRT twin); an hourly
  24 h refresh repeats ~95% rows that are already stored and those are left untouched;
* events are rebuilt from *what can have changed*: the changed detections plus the members of
  the events they belong to or could link to. Two events that were separate stay separate
  unless a changed detection joins them, so nothing else needs re-clustering, however dense
  the surrounding fire activity is (growing a rebuild region until no event straddled its
  edge made one new detection in the Sahel re-cluster the whole week of the whole region).
"""

import json
import math
from bisect import bisect_left
from collections import Counter, defaultdict
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import NamedTuple

from sqlalchemy import and_, case, delete, func, insert, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.ingest.clustering import EventDraft, build_event, cluster_detections
from app.models import DetectionRow, EventHistoryRow, EventLineageRow, EventPassRow, EventRow
from app.schemas import FireDetection, to_naive_utc

_CHUNK = 500
_ID_LOOKUP_CHUNK = 900  # stays under SQLite's bound-parameter limit on old builds
_DETECTION_COLUMNS = [c.name for c in DetectionRow.__table__.columns if c.name not in ("id", "event_id")]
TILE_DEG = 1.0  # changed detections are grouped per 1-degree tile to look up nearby events
# Largest pixel half-diagonal in FIRMS data (MODIS at the swath edge: 4.8 x 2.0 km -> 2.6 km).
# Reach pads derive from it: two such pixels + the edge's tolerance + a safety margin.
MAX_PIXEL_RADIUS_KM = 2.7
REACH_MARGIN_KM = 0.5
_HASH_DEG = 0.05  # spatial hash of changed detections for the reach test


class Seed(NamedTuple):
    """A detection that changed (new, upgraded NRT -> SP, or flagged as a non-fire heat source)."""

    lat: float
    lon: float
    t: datetime  # naive UTC, like the database
    id: str


class LightDetection(NamedTuple):
    """Just what clustering needs; ~10x cheaper than the full pydantic model for 100k+ rows."""

    id: str
    instrument: str
    satellite: str
    latitude: float
    longitude: float
    acquired_at: datetime  # aware UTC
    scan_km: float
    track_km: float
    frp_mw: float | None
    confidence_pct: int


def _insert_for(session: Session):
    return pg_insert if session.get_bind().dialect.name == "postgresql" else sqlite_insert


def upsert_detections(session: Session, detections: Sequence[FireDetection]) -> list[Seed]:
    """Write new detections; replace an NRT row when its SP (archive) twin arrives, never the reverse.

    Rows already stored at the same or better processing level are skipped entirely. Returns the
    detections that changed, which are the only ones whose events need rebuilding.
    """
    if not detections:
        return []
    existing: dict[str, str] = {}
    ids = list({d.id for d in detections})
    for i in range(0, len(ids), _ID_LOOKUP_CHUNK):
        chunk = ids[i : i + _ID_LOOKUP_CHUNK]
        rows = session.execute(select(DetectionRow.id, DetectionRow.processing_level).where(DetectionRow.id.in_(chunk)))
        existing.update((r.id, r.processing_level) for r in rows)  # a Result has .keys(): don't pass it to update()

    now = datetime.now(UTC).replace(tzinfo=None)
    new_rows, upgrades, seeds, seen = [], [], [], set()
    for d in detections:
        if d.id in seen:
            continue
        seen.add(d.id)
        stored = existing.get(d.id)
        is_new = stored is None
        is_upgrade = stored is not None and stored != "SP" and d.processing_level == "SP"
        if not (is_new or is_upgrade):
            continue
        row = d.model_dump(exclude={"event_id"})
        row["acquired_at"] = to_naive_utc(d.acquired_at)
        row["acq_hour"] = int(d.acquired_at.timestamp() // 3600)
        row["ingested_at"] = now
        (new_rows if is_new else upgrades).append(row)
        seeds.append(Seed(d.latitude, d.longitude, row["acquired_at"], d.id))

    table = DetectionRow.__table__
    step = _CHUNK * 10
    for i in range(0, len(new_rows), step):  # executemany: compiled once, batched by the driver
        session.execute(insert(table), new_rows[i : i + step])
    if upgrades:
        stmt = _insert_for(session)(table)
        stmt = stmt.on_conflict_do_update(index_elements=["id"], set_={c: stmt.excluded[c] for c in _DETECTION_COLUMNS})
        for i in range(0, len(upgrades), step):
            session.execute(stmt, upgrades[i : i + step])
    return seeds


def _tile(lat: float, lon: float) -> tuple[int, int]:
    return math.floor(lat / TILE_DEG), math.floor(lon / TILE_DEG)


def _lon_within(lon: float, west: float, east: float, pad: float) -> bool:
    """lon inside [west - pad, east + pad], going east from west (handles boxes crossing 180)."""
    width = (east - west) % 360
    d = (lon - west) % 360
    return d <= width + pad or 360 - d <= pad


def _events_within_reach(session: Session, seeds: Sequence[Seed], gap: timedelta, cont: timedelta,
                         pad_gap_km: float, pad_cont_km: float) -> set[str]:
    """Events a changed detection could link to. A seed within `gap` of an event's time span is
    tested against the event's box padded by `pad_gap_km` (E1 edge); a seed only within `cont`
    (E2, pixels must overlap) against the box padded by the smaller `pad_cont_km`. Longitudes
    wrap at the antimeridian."""
    n_cols = round(360 / _HASH_DEG)
    by_tile: dict[tuple[int, int], list[Seed]] = defaultdict(list)
    hashed: dict[tuple[int, int], list[Seed]] = defaultdict(list)
    for sd in seeds:
        by_tile[_tile(sd.lat, sd.lon)].append(sd)
        hashed[(math.floor(sd.lat / _HASH_DEG), math.floor((sd.lon + 180) / _HASH_DEG) % n_cols)].append(sd)
    times: dict[tuple[int, int], list[datetime]] = {}
    for key, group in hashed.items():
        group.sort(key=lambda sd: sd.t)
        times[key] = [sd.t for sd in group]
    far = max(gap, cont)
    pad_max = max(pad_gap_km, pad_cont_km if cont > gap else 0.0)

    def near_a_seed(ev) -> bool:
        cos_ev = max(math.cos(math.radians(min(89.9, max(abs(ev.south), abs(ev.north)) + pad_max / 110.574))), 1e-3)
        reach_lat, reach_lon = pad_max / 110.574, min(180.0, pad_max / (111.320 * cos_ev))
        width = (ev.east - ev.west) % 360
        first_col = math.floor((ev.west - reach_lon + 180) / _HASH_DEG)
        n_span = min(n_cols, math.ceil((width + 2 * reach_lon) / _HASH_DEG) + 1)
        lo, hi = ev.start_at - far, ev.end_at + far
        for gy in range(math.floor((ev.south - reach_lat) / _HASH_DEG), math.floor((ev.north + reach_lat) / _HASH_DEG) + 1):
            for k in range(n_span):
                key = (gy, (first_col + k) % n_cols)
                ts = times.get(key)
                if not ts:
                    continue
                group = hashed[key]
                for i in range(bisect_left(ts, lo), len(ts)):
                    sd = group[i]
                    if sd.t > hi:
                        break
                    pad = pad_gap_km if ev.start_at - gap <= sd.t <= ev.end_at + gap else pad_cont_km
                    dlat, dlon = pad / 110.574, min(180.0, pad / (111.320 * cos_ev))
                    if ev.south - dlat <= sd.lat <= ev.north + dlat and _lon_within(sd.lon, ev.west, ev.east, dlon):
                        return True
        return False

    found: set[str] = set()
    # Candidates are searched by centroid. The (FRP-weighted) centroid lies inside the event's box,
    # so an event touching the tile has it within its own extent (+ the pad) of the tile: the
    # largest stored event bounds the search.
    lon_span = case((EventRow.east >= EventRow.west, EventRow.east - EventRow.west), else_=EventRow.east - EventRow.west + 360)
    span = session.execute(select(func.max(EventRow.north - EventRow.south), func.max(lon_span))).one()
    ext_lat, ext_lon = span[0] or 0.0, span[1] or 0.0
    for (ty, tx), group in by_tile.items():
        t0 = min(sd.t for sd in group) - far
        t1 = max(sd.t for sd in group) + far
        edge_lat = min(89.9, max(abs(ty * TILE_DEG), abs((ty + 1) * TILE_DEG)) + ext_lat)
        r_lat = ext_lat + pad_max / 110.574
        r_lon = ext_lon + pad_max / (111.320 * max(math.cos(math.radians(edge_lat)), 0.01))
        lo_lon, hi_lon = tx * TILE_DEG - r_lon, (tx + 1) * TILE_DEG + r_lon
        if hi_lon - lo_lon >= 360:
            lon_ok = True
        elif lo_lon < -180:
            lon_ok = or_(EventRow.longitude >= lo_lon + 360, EventRow.longitude <= hi_lon)
        elif hi_lon > 180:
            lon_ok = or_(EventRow.longitude >= lo_lon, EventRow.longitude <= hi_lon - 360)
        else:
            lon_ok = and_(EventRow.longitude >= lo_lon, EventRow.longitude <= hi_lon)
        stmt = select(EventRow.id, EventRow.west, EventRow.south, EventRow.east, EventRow.north, EventRow.start_at, EventRow.end_at).where(
            EventRow.latitude >= ty * TILE_DEG - r_lat, EventRow.latitude <= (ty + 1) * TILE_DEG + r_lat,
            EventRow.end_at >= t0, EventRow.start_at <= t1,
        )
        if lon_ok is not True:
            stmt = stmt.where(lon_ok)
        found.update(ev.id for ev in session.execute(stmt) if ev.id not in found and near_a_seed(ev))
    return found


_LIGHT_COLUMNS = (
    DetectionRow.id, DetectionRow.instrument, DetectionRow.satellite, DetectionRow.latitude, DetectionRow.longitude,
    DetectionRow.acquired_at, DetectionRow.scan_km, DetectionRow.track_km, DetectionRow.frp_mw,
    DetectionRow.confidence_pct, DetectionRow.event_id,
)
# Fields whose change makes a new version of an event (history row). Lifecycle-only changes are "state".
_TRACKED = ("detection_count", "start_at", "end_at", "area_km2", "max_frp_mw", "latitude", "longitude", "pass_count")
_OLD_COLUMNS = (EventRow.id, EventRow.version, EventRow.updated_at, EventRow.lifecycle_state, *(getattr(EventRow, f) for f in _TRACKED))


def _chunks(items: Sequence, size: int = _ID_LOOKUP_CHUNK):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def cluster_params(settings) -> dict:
    """Keyword arguments for rebuild_events / rebuild_all_events from Settings."""
    return {
        "tolerance_km": settings.cluster_tolerance_km,
        "max_gap_hours": settings.cluster_max_gap_hours,
        "active_hours": settings.event_active_hours,
        "continuity_gap_hours": getattr(settings, "cluster_continuity_gap_hours", 0.0),
        "continuity_tolerance_km": getattr(settings, "cluster_continuity_tolerance_km", 0.0),
        "archive_days": getattr(settings, "event_archive_days", 30.0),
    }


def _light(row) -> LightDetection:
    return LightDetection(row.id, row.instrument, row.satellite, row.latitude, row.longitude,
                          row.acquired_at.replace(tzinfo=UTC), row.scan_km, row.track_km, row.frp_mw, row.confidence_pct)


def _naive(dt: datetime | None) -> datetime | None:
    return to_naive_utc(dt) if dt is not None and dt.tzinfo else dt


def _snapshot(d: EventDraft) -> str:
    keep = ("latitude", "longitude", "start_at", "end_at", "detection_count", "modis_count", "viirs_count", "pass_count",
            "evidence", "max_frp_mw", "peak_pass_frp_mw", "area_km2", "detection_hull_km2", "extent_km",
            "lifecycle_state", "intensity_trend")
    out = {}
    for k in keep:
        v = getattr(d, k)
        out[k] = _naive(v).isoformat() if isinstance(v, datetime) else v
    return json.dumps(out)


def rebuild_events(
    session: Session,
    seeds: Sequence[Seed],
    tolerance_km: float,
    max_gap_hours: float,
    active_hours: float,
    now: datetime | None = None,
    *,
    continuity_gap_hours: float = 0.0,
    continuity_tolerance_km: float = 0.0,
    archive_days: float = 30.0,
    reason: str = "new_detections",
    full: bool = False,
) -> list[EventDraft]:
    """Re-cluster the changed detections with the events they belong to or could join.

    Clustering links detections that are close in space and time; two events that were separate
    can only become one through a changed detection. So the job is exactly: the changed detections
    + every member of (a) events containing a changed detection (archive upgrade, newly flagged
    gas flare) and (b) events within link reach of one. `full=True` re-clusters everything (used
    when the rules change). Either way the previous assignment is diffed against the new one, so
    merges, splits and renames are recorded (event_lineage / event_history) and no id is lost.
    Returns the rebuilt events.
    """
    if not seeds and not full:
        return []
    now = now or datetime.now(UTC)
    now_naive = to_naive_utc(now)
    horizon = timedelta(hours=max(max_gap_hours, continuity_gap_hours))

    # 1. which events are affected, and which of them lose detections to the non-fire flag
    if full:
        affected = set(session.scalars(select(EventRow.id)))
        static_lost = set(session.scalars(
            select(DetectionRow.event_id).where(DetectionRow.static_kind.is_not(None), DetectionRow.event_id.is_not(None))
        ))
    else:
        seed_ids = list({sd.id for sd in seeds})
        affected, static_lost = set(), set()
        for chunk in _chunks(seed_ids):
            for eid, kind in session.execute(select(DetectionRow.event_id, DetectionRow.static_kind).where(
                    DetectionRow.id.in_(chunk), DetectionRow.event_id.is_not(None))):
                affected.add(eid)
                if kind is not None:
                    static_lost.add(eid)
        affected |= _events_within_reach(
            session, seeds, timedelta(hours=max_gap_hours), horizon,
            2 * MAX_PIXEL_RADIUS_KM + tolerance_km + REACH_MARGIN_KM,
            2 * MAX_PIXEL_RADIUS_KM + continuity_tolerance_km + REACH_MARGIN_KM,
        )

    # 2. their current state (for the diff) and every detection to re-cluster
    old_rows: dict[str, object] = {}
    for chunk in _chunks(sorted(affected)):
        old_rows.update((r.id, r) for r in session.execute(select(*_OLD_COLUMNS).where(EventRow.id.in_(chunk))))
    detections: dict[str, LightDetection] = {}
    prev_owner: dict[str, str | None] = {}

    def take(stmt):
        for row in session.execute(stmt):
            detections[row.id] = _light(row)
            prev_owner[row.id] = row.event_id

    fire = DetectionRow.static_kind.is_(None)
    if full:
        take(select(*_LIGHT_COLUMNS).where(fire))
    else:
        for chunk in _chunks(sorted(affected)):
            take(select(*_LIGHT_COLUMNS).where(DetectionRow.event_id.in_(chunk), fire))
        for chunk in _chunks(seed_ids):
            take(select(*_LIGHT_COLUMNS).where(DetectionRow.id.in_(chunk), fire))

    # 3. volcanoes, gas flares and offshore platforms are not wildfires: no event, no alert
    if full:
        session.execute(update(DetectionRow).where(DetectionRow.static_kind.is_not(None), DetectionRow.event_id.is_not(None))
                        .values(event_id=None))
        session.execute(delete(EventRow))
        session.execute(delete(EventPassRow))
    else:
        for chunk in _chunks(seed_ids):
            session.execute(update(DetectionRow).where(DetectionRow.id.in_(chunk), DetectionRow.static_kind.is_not(None))
                            .values(event_id=None))
        for chunk in _chunks(sorted(affected)):
            session.execute(delete(EventRow).where(EventRow.id.in_(chunk)))
            session.execute(delete(EventPassRow).where(EventPassRow.event_id.in_(chunk)))

    # 4. cluster and describe
    clusters = cluster_detections(list(detections.values()), tolerance_km, max_gap_hours,
                                  continuity_gap_hours, continuity_tolerance_km)
    drafts = [build_event(m, now, active_hours, archive_days) for m in clusters]
    new_owner = {mid: d.id for d in drafts for mid in d.member_ids}

    # 5. record what happened to every previous id, and version the events that changed
    history = _record_changes(session, old_rows, prev_owner, new_owner, drafts, static_lost, reason, now_naive)

    # 6. write
    event_rows, pass_rows, assignments = [], [], []
    for d in drafts:
        row = d.row()
        for k in ("start_at", "end_at", "run_start_at"):
            row[k] = _naive(row[k])
        row["version"], row["updated_at"] = history[d.id]
        event_rows.append(row)
        pass_rows.extend({"event_id": d.id, "acquired_at": _naive(p.acquired_at), "satellite": p.satellite,
                          "instrument": p.instrument, "pixels": p.pixels, "frp_sum": p.frp_sum, "frp_max": p.frp_max}
                         for p in d.passes)
        assignments.extend({"id": mid, "event_id": d.id} for mid in d.member_ids if prev_owner.get(mid) != d.id)
    step = _CHUNK * 10
    for table, rows in ((EventRow.__table__, event_rows), (EventPassRow.__table__, pass_rows)):
        for i in range(0, len(rows), step):
            session.execute(insert(table), rows[i : i + step])
    for i in range(0, len(assignments), step):
        session.execute(update(DetectionRow), assignments[i : i + step])
    session.flush()
    return drafts


def _record_changes(session: Session, old_rows: dict, prev_owner: dict, new_owner: dict, drafts: list[EventDraft],
                    static_lost: set, reason: str, now: datetime) -> dict[str, tuple[int, datetime | None]]:
    """Diff the previous assignment of detections to events against the new one.

    Writes event_lineage for ids that stopped being canonical (merged / split / renamed / removed)
    and event_history for every event that changed. Returns {new event id: (version, updated_at)}.
    """
    old_members: dict[str, list[str]] = defaultdict(list)
    for mid, owner in prev_owner.items():
        if owner in old_rows:
            old_members[owner].append(mid)
    new_ids = {d.id for d in drafts}
    # which previous events fed each new event
    sources: dict[str, Counter] = defaultdict(Counter)
    for old_id, mids in old_members.items():
        for mid in mids:
            sources[new_owner[mid]][old_id] += 1

    lineage, hist = [], []

    def why(old_id: str) -> str:
        return "static_reclassified" if old_id in static_lost else reason

    for old_id, row in old_rows.items():
        targets = Counter(new_owner[m] for m in old_members.get(old_id, ()))
        if old_id in new_ids:
            for t, n in sorted(targets.items()):
                if t != old_id:  # part of it now forms another event
                    lineage.append({"old_id": old_id, "new_id": t, "kind": "split", "reason": why(old_id), "detections_moved": n, "at": now})
            continue
        if not targets:
            kind, related = "removed", None
            lineage.append({"old_id": old_id, "new_id": None, "kind": kind, "reason": why(old_id), "detections_moved": 0, "at": now})
        elif len(targets) == 1:
            (t, n), = targets.items()
            kind = "merged" if len(sources[t]) > 1 or t in old_rows else "renamed"
            related = t
            lineage.append({"old_id": old_id, "new_id": t, "kind": kind, "reason": why(old_id), "detections_moved": n, "at": now})
        else:
            kind = "split"
            related = max(sorted(targets), key=lambda k: targets[k])
            lineage.extend({"old_id": old_id, "new_id": t, "kind": kind, "reason": why(old_id), "detections_moved": n, "at": now}
                           for t, n in sorted(targets.items()))
        hist.append({"event_id": old_id, "version": (row.version or 1) + 1, "at": now, "change": kind,
                     "from_state": row.lifecycle_state, "to_state": kind, "related_id": related, "reason": why(old_id),
                     "snapshot": "{}"})

    versions: dict[str, tuple[int, datetime | None]] = {}
    for d in drafts:
        old = old_rows.get(d.id)
        fed_by = sorted(o for o in sources.get(d.id, ()) if o != d.id)
        if old is None:
            change, version = "created", 1
        else:
            moved = any(_differs(getattr(old, f), getattr(d, f)) for f in _TRACKED)
            if fed_by:
                change = "merged"
            elif moved:
                change = "grown" if d.detection_count > old.detection_count else "updated"
            elif old.lifecycle_state != d.lifecycle_state:
                change = "state"
            else:
                versions[d.id] = (old.version or 1, old.updated_at)
                continue
            version = (old.version or 1) + 1
        hist.append({"event_id": d.id, "version": version, "at": now, "change": change,
                     "from_state": old.lifecycle_state if old is not None else None, "to_state": d.lifecycle_state,
                     "related_id": fed_by[0] if fed_by else None,
                     "reason": None if change in ("grown", "updated", "state") else (why(fed_by[0]) if fed_by else reason),
                     "snapshot": _snapshot(d)})
        versions[d.id] = (version, now)

    step = _CHUNK * 10
    for table, rows in ((EventLineageRow.__table__, lineage), (EventHistoryRow.__table__, hist)):
        for i in range(0, len(rows), step):
            session.execute(insert(table), rows[i : i + step])
    return versions


def _differs(a, b) -> bool:
    if isinstance(b, datetime):
        b = _naive(b)
    if isinstance(a, float) or isinstance(b, float):
        return a is None or b is None or abs(a - b) > 1e-6
    return a != b


def refresh_event_status(session: Session, active_hours: float, now: datetime | None = None, archive_days: float = 30.0) -> int:
    """Time-driven lifecycle transitions (ACTIVE/REACTIVATED -> INACTIVE -> ARCHIVED), each recorded
    in event_history. Returns the number of events that changed state."""
    now_naive = to_naive_utc(now or datetime.now(UTC))
    quiet = now_naive - timedelta(hours=active_hours)
    archive = now_naive - timedelta(days=archive_days)
    moves = [
        ("archived", (EventRow.end_at < archive, EventRow.lifecycle_state != "archived")),
        ("inactive", (EventRow.end_at < quiet, EventRow.end_at >= archive, EventRow.lifecycle_state.in_(("active", "reactivated")))),
    ]
    changed = 0
    for state, where in moves:
        rows = session.execute(select(EventRow.id, EventRow.version, EventRow.lifecycle_state).where(*where)).all()
        if not rows:
            continue
        for chunk in _chunks([r.id for r in rows]):
            session.execute(update(EventRow).where(EventRow.id.in_(chunk)).values(
                lifecycle_state=state, status="inactive", version=EventRow.version + 1, updated_at=now_naive))
        hist = [{"event_id": r.id, "version": (r.version or 1) + 1, "at": now_naive, "change": "state",
                 "from_state": r.lifecycle_state, "to_state": state, "related_id": None,
                 "reason": f"no detection for {active_hours:g} h" if state == "inactive" else f"no detection for {archive_days:g} days",
                 "snapshot": json.dumps({"lifecycle_state": state})} for r in rows]
        for i in range(0, len(hist), _CHUNK * 10):
            session.execute(insert(EventHistoryRow.__table__), hist[i : i + _CHUNK * 10])
        changed += len(rows)
    return changed


SITE_CELL_DEG = 0.005  # ~0.5 km cells; a site's 3x3 neighbourhood (~1.5 km) absorbs VIIRS geolocation error


def flag_static_sources(session: Session) -> list[Seed]:
    """Mark unlabelled (NRT) detections that sit on a site the FIRMS archive called non-vegetation.

    The archive (SP) labels each pixel: volcano, other static land source (gas flares, plants,
    refineries) or offshore (platforms). NRT data has no label, so the same gas flare shows up as
    a "fire" every night. Sites are learned from the archive rows themselves; archive-labelled rows
    keep their own verdict. Returns the detections whose flag changed (their events must be rebuilt).
    """
    c = SITE_CELL_DEG
    sites: dict[tuple[int, int], str] = {}
    for r in session.execute(
        select(DetectionRow.latitude, DetectionRow.longitude, DetectionRow.static_kind).where(
            DetectionRow.detection_type.in_((1, 2, 3))
        )
    ):
        sites[(math.floor(r.latitude / c), math.floor(r.longitude / c))] = r.static_kind
    changed: list[Seed] = []
    for (gy, gx), kind in sites.items():
        hits = session.execute(
            select(DetectionRow.id, DetectionRow.latitude, DetectionRow.longitude, DetectionRow.acquired_at).where(
                DetectionRow.latitude >= (gy - 1) * c,
                DetectionRow.latitude < (gy + 2) * c,
                DetectionRow.longitude >= (gx - 1) * c,
                DetectionRow.longitude < (gx + 2) * c,
                DetectionRow.detection_type.is_(None),
                DetectionRow.static_kind.is_(None),
            )
        ).all()
        if hits:
            ids = [h.id for h in hits]
            for i in range(0, len(ids), _CHUNK):
                session.execute(update(DetectionRow).where(DetectionRow.id.in_(ids[i : i + _CHUNK])).values(static_kind=kind))
            changed.extend(Seed(h.latitude, h.longitude, h.acquired_at, h.id) for h in hits)
    return changed


def rebuild_all_events(session: Session, tolerance_km: float, max_gap_hours: float, active_hours: float,
                       **params) -> int:
    """Re-cluster every detection (used when the event rules change). Records how every previous id
    maps to the new events, so nothing that referenced an old id is left dangling."""
    flag_static_sources(session)
    return len(rebuild_events(session, [], tolerance_km, max_gap_hours, active_hours, full=True,
                              reason=params.pop("reason", "rule_change"), **params))
