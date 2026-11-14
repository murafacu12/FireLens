"""Persistence of detections and (re)building of fire events."""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.ingest.clustering import build_event, cluster_detections
from app.models import DetectionRow, EventRow
from app.schemas import FireDetection, to_naive_utc

_CHUNK = 500
_DETECTION_COLUMNS = [c.name for c in DetectionRow.__table__.columns if c.name not in ("id", "event_id")]


def _insert_for(session: Session):
    return pg_insert if session.get_bind().dialect.name == "postgresql" else sqlite_insert


def upsert_detections(session: Session, detections: Sequence[FireDetection]) -> tuple[datetime, datetime] | None:
    """Insert or update detections. Returns the (min, max) acquisition window touched.

    A standard-processing (SP) record replaces its near-real-time (NRT) twin, never the reverse.
    """
    if not detections:
        return None
    insert = _insert_for(session)
    now = datetime.now(UTC).replace(tzinfo=None)
    table = DetectionRow.__table__
    rows = []
    for d in detections:
        data = d.model_dump(exclude={"event_id"})
        data["acquired_at"] = to_naive_utc(d.acquired_at)
        data["ingested_at"] = now
        rows.append(data)
    stmt = insert(table)
    stmt = stmt.on_conflict_do_update(
        index_elements=["id"],
        set_={c: stmt.excluded[c] for c in _DETECTION_COLUMNS},
        where=(table.c.processing_level != "SP") | (stmt.excluded.processing_level == "SP"),
    )
    for i in range(0, len(rows), _CHUNK * 10):  # executemany: compiled once, batched by the driver
        session.execute(stmt, rows[i : i + _CHUNK * 10])
    times = [r["acquired_at"] for r in rows]
    return min(times), max(times)


def rebuild_events(
    session: Session,
    window: tuple[datetime, datetime],
    tolerance_km: float,
    max_gap_hours: float,
    active_hours: float,
    now: datetime | None = None,
) -> list[EventRow]:
    """Re-cluster every detection that could belong to an event touching `window`.

    The window is grown until no existing event straddles its edges, so events are
    always rebuilt from their complete set of detections.
    """
    now = now or datetime.now(UTC)
    pad = timedelta(hours=max_gap_hours)
    w0, w1 = window[0] - pad, window[1] + pad
    while True:
        affected = session.execute(
            select(EventRow.id, EventRow.start_at, EventRow.end_at).where(
                EventRow.end_at >= w0, EventRow.start_at <= w1
            )
        ).all()
        n0 = min([w0, *(a.start_at for a in affected)])
        n1 = max([w1, *(a.end_at for a in affected)])
        if (n0, n1) == (w0, w1):
            break
        w0, w1 = n0, n1

    if affected:
        ids = [a.id for a in affected]
        for i in range(0, len(ids), _CHUNK):
            session.execute(delete(EventRow).where(EventRow.id.in_(ids[i : i + _CHUNK])))

    rows = session.scalars(
        select(DetectionRow).where(DetectionRow.acquired_at >= w0, DetectionRow.acquired_at <= w1)
    ).all()
    detections = [FireDetection.model_validate(r) for r in rows]
    clusters = cluster_detections(detections, tolerance_km, max_gap_hours)

    events: list[EventRow] = []
    assignments: list[dict] = []
    for members in clusters:
        draft = build_event(members, now, active_hours)
        values = {k: v for k, v in vars(draft).items() if k != "member_ids"}
        values["start_at"] = to_naive_utc(draft.start_at)
        values["end_at"] = to_naive_utc(draft.end_at)
        ev = EventRow(**values)
        events.append(ev)
        assignments.extend({"id": mid, "event_id": draft.id} for mid in draft.member_ids)
    session.add_all(events)
    if assignments:
        session.execute(update(DetectionRow), assignments)
    session.flush()
    return events


def refresh_event_status(session: Session, active_hours: float, now: datetime | None = None) -> None:
    cutoff = to_naive_utc((now or datetime.now(UTC)) - timedelta(hours=active_hours))
    session.execute(update(EventRow).where(EventRow.end_at < cutoff).values(status="inactive"))
    session.execute(update(EventRow).where(EventRow.end_at >= cutoff).values(status="active"))
