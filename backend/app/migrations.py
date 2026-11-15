"""Minimal in-place schema upgrades until Alembic is introduced (roadmap: PostgreSQL/PostGIS).

`Base.metadata.create_all` creates missing tables but never alters existing ones, so new columns
are added here. Derived data is versioned: when the logic that builds fire events (EVENTS_VERSION)
or the map pyramid (CELLS_VERSION) changes, it is rebuilt from the stored detections on start.
The per-sensor analysis views are recreated on every start (they cost nothing).
"""

import logging
import time

from sqlalchemy import Engine, inspect, select, text, update
from sqlalchemy.orm import Session

from app.models import AppMetaRow, DetectionRow

log = logging.getLogger(__name__)

# 2: area = union of nominal-resolution pixels (+ area_basis); non-vegetation sources excluded
# 3: E2 continuity edge, lifecycle states, per-pass FRP, evidence, area measures, lineage/history
EVENTS_VERSION = "3"
LEGACY_EVENTS_TABLE = "fire_events_legacy_v2"  # snapshot of the v2 events taken before the v3 rebuild
# 1: hourly cells, levels 4-9, per sensor x confidence x static
CELLS_VERSION = "1"

ADDED_COLUMNS = [
    ("detections", "static_kind", "VARCHAR(12)"),
    ("fire_events", "area_basis", "VARCHAR(16) DEFAULT 'viirs_375m'"),
    ("detections", "acq_hour", "INTEGER"),
    ("fire_events", "lifecycle_state", "VARCHAR(12) DEFAULT 'active'"),
    ("fire_events", "run_start_at", "DATETIME"),
    ("fire_events", "observed_hours", "FLOAT DEFAULT 0"),
    ("fire_events", "pass_count", "INTEGER DEFAULT 0"),
    ("fire_events", "evidence", "VARCHAR(16) DEFAULT 'single_detection'"),
    ("fire_events", "satellite_counts", "TEXT DEFAULT '{}'"),
    ("fire_events", "mean_frp_viirs_mw", "FLOAT"),
    ("fire_events", "mean_frp_modis_mw", "FLOAT"),
    ("fire_events", "peak_pass_frp_mw", "FLOAT DEFAULT 0"),
    ("fire_events", "peak_pass_sensor", "VARCHAR(8)"),
    ("fire_events", "intensity_trend", "VARCHAR(20) DEFAULT 'insufficient_data'"),
    ("fire_events", "extent_km", "FLOAT DEFAULT 0"),
    ("fire_events", "detection_hull_km2", "FLOAT DEFAULT 0"),
    ("fire_events", "footprint_growth_km2_per_day", "FLOAT"),
    ("fire_events", "footprint_growth_se", "FLOAT"),
    ("fire_events", "version", "INTEGER DEFAULT 1"),
    ("fire_events", "updated_at", "DATETIME"),
]
ADDED_INDEXES = [
    ("ix_detections_acq_hour", "detections", "acq_hour"),
    ("ix_detections_instrument_hour", "detections", "instrument, acq_hour"),
]

# MODIS and VIIRS stay in one harmonized table (most questions span both sensors), but each sensor
# can be analysed on its own, under the names its documentation uses. On PostgreSQL the table can
# become LIST-partitioned by instrument without changing these views.
_COMMON = """id, source, satellite, processing_level, latitude, longitude, acquired_at, acq_hour, daynight,
    frp_mw, scan_km, track_km, pixel_area_km2, confidence_level, detection_type AS type, static_kind,
    version, event_id, ingested_at"""
SENSOR_VIEWS = {
    "modis_detections": f"""SELECT {_COMMON},
    brightness_k AS brightness_t21_k, brightness_secondary_k AS bright_t31_k,
    confidence_pct AS confidence_pct
    FROM detections WHERE instrument = 'MODIS'""",
    "viirs_detections": f"""SELECT {_COMMON},
    brightness_k AS bright_ti4_k, brightness_secondary_k AS bright_ti5_k, brightness_saturated AS ti4_saturated,
    confidence_raw AS confidence_class, confidence_pct AS confidence_pct_harmonized
    FROM detections WHERE instrument = 'VIIRS'""",
}
STATIC_KIND_BY_TYPE = {1: "volcano", 2: "industrial", 3: "offshore"}


class SchemaUpgradeRequired(RuntimeError):
    """The database holds data and needs an upgrade that was not explicitly allowed."""


def pending_upgrade(engine: Engine) -> list[str]:
    """What an upgrade would change in an existing database with data, without changing anything.
    Empty or new databases return [] (they are set up freely)."""
    from app.db import Base  # local: models register on Base at import

    insp = inspect(engine)
    tables = set(insp.get_table_names())
    if "detections" not in tables:
        return []
    with engine.connect() as conn:
        if conn.execute(text("SELECT 1 FROM detections LIMIT 1")).first() is None:
            return []
        meta = dict(conn.execute(text("SELECT key, value FROM app_meta")).all()) if "app_meta" in tables else {}
    reasons = [f"create table {t}" for t in sorted(set(Base.metadata.tables) - tables)]
    for table, column, _ in ADDED_COLUMNS:
        if table in tables and column not in {c["name"] for c in insp.get_columns(table)}:
            reasons.append(f"add column {table}.{column}")
    if "acq_hour" in {c["name"] for c in insp.get_columns("detections")}:
        with engine.connect() as conn:
            missing = conn.execute(text("SELECT count(*) FROM detections WHERE acq_hour IS NULL")).scalar()
        if missing:
            reasons.append(f"fill acq_hour for {missing} detections (stored by older code)")
            reasons.append("rebuild map pyramid for those hours")
    if meta.get("events_version") != EVENTS_VERSION:
        reasons.append(f"rebuild fire events v{meta.get('events_version', '?')} -> v{EVENTS_VERSION}")
    if meta.get("cells_version") != CELLS_VERSION:
        reasons.append(f"rebuild map pyramid v{meta.get('cells_version', '?')} -> v{CELLS_VERSION}")
    return reasons


def guard(engine: Engine, allowed: bool) -> list[str]:
    """Refuse to start on a database with data that needs an upgrade nobody approved."""
    reasons = pending_upgrade(engine)
    if reasons and not allowed:
        raise SchemaUpgradeRequired(
            "The database needs an upgrade and holds data, so FireLens will not change it on its own: "
            + "; ".join(reasons)
            + ". Back it up, rehearse on a copy (docs/MIGRATION_V3.md), then start once with "
            "FIRELENS_ALLOW_SCHEMA_UPGRADE=true."
        )
    return reasons


def add_missing_columns(engine: Engine) -> list[str]:
    insp = inspect(engine)
    added = []
    with engine.begin() as conn:
        for table, column, ddl in ADDED_COLUMNS:
            if column not in {c["name"] for c in insp.get_columns(table)}:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
                added.append(f"{table}.{column}")
        # Every upgrade fills missing hours, not only the one that adds the column: code older than
        # the pyramid (e.g. after a rollback) stores rows without it, and they must not vanish from
        # the map when the database is upgraded again.
        conn.execute(text(f"UPDATE detections SET acq_hour = {_hour_sql(engine)} WHERE acq_hour IS NULL"))
        existing = {i["name"] for i in inspect(conn).get_indexes("detections")}
        for name, table, cols in ADDED_INDEXES:
            if name not in existing:
                conn.execute(text(f"CREATE INDEX {name} ON {table} ({cols})"))
    return added


def _hour_sql(engine: Engine) -> str:
    if engine.dialect.name == "postgresql":
        return "CAST(floor(extract(epoch from acquired_at) / 3600) AS INTEGER)"
    return "CAST(strftime('%s', acquired_at) AS INTEGER) / 3600"


def create_views(engine: Engine) -> None:
    """(Re)create the per-sensor views only when missing or out of date."""
    with engine.begin() as conn:
        for name, body in SENSOR_VIEWS.items():
            ddl = f"CREATE VIEW {name} AS {body}"
            current = conn.execute(text("SELECT sql FROM sqlite_master WHERE type='view' AND name=:n"), {"n": name}).scalar() \
                if engine.dialect.name == "sqlite" else None
            if current == ddl:
                continue
            conn.execute(text(f"DROP VIEW IF EXISTS {name}"))
            conn.execute(text(ddl))


def events_outdated(session: Session) -> bool:
    row = session.get(AppMetaRow, "events_version")
    has_data = session.scalar(select(DetectionRow.id).limit(1)) is not None
    return has_data and (row is None or row.value != EVENTS_VERSION)


def mark_events_current(session: Session) -> None:
    session.merge(AppMetaRow(key="events_version", value=EVENTS_VERSION))


def _meta(session: Session, key: str) -> str | None:
    row = session.get(AppMetaRow, key)
    return row.value if row else None


def snapshot_legacy_events(session: Session) -> None:
    """Before the first v3 rebuild, keep a copy of the previous events (nothing historical is deleted)."""
    if session.get_bind().dialect.name not in ("sqlite", "postgresql"):
        return
    exists = inspect(session.get_bind()).has_table(LEGACY_EVENTS_TABLE)
    if not exists and session.scalar(text("SELECT count(*) FROM fire_events")):
        session.execute(text(f"CREATE TABLE {LEGACY_EVENTS_TABLE} AS SELECT * FROM fire_events"))
        log.info("previous fire events kept in %s", LEGACY_EVENTS_TABLE)


def backfill_static_kind(session: Session) -> None:
    """Rows stored before static_kind existed: derive it from the archive's type code."""
    for code, kind in STATIC_KIND_BY_TYPE.items():
        session.execute(
            update(DetectionRow)
            .where(DetectionRow.detection_type == code, DetectionRow.static_kind.is_(None))
            .values(static_kind=kind)
        )


def upgrade(engine: Engine, session_factory, settings) -> None:
    filled_hours = 0
    if "acq_hour" in {c["name"] for c in inspect(engine).get_columns("detections")}:
        with engine.connect() as conn:
            filled_hours = conn.execute(text("SELECT count(*) FROM detections WHERE acq_hour IS NULL")).scalar() or 0
    added = add_missing_columns(engine)  # also fills missing acq_hour
    if added:
        log.info("schema upgraded: added %s", ", ".join(added))
    create_views(engine)
    with session_factory() as session:
        rebuilt_events = False
        if events_outdated(session):
            from app.services.store import cluster_params, rebuild_all_events  # local import: store imports models

            t = time.time()
            snapshot_legacy_events(session)
            backfill_static_kind(session)
            n = rebuild_all_events(session, **cluster_params(settings))
            rebuilt_events = True
            log.info("fire events rebuilt with logic v%s: %d events in %.1fs", EVENTS_VERSION, n, time.time() - t)
        if session.get(AppMetaRow, "events_version") is None or rebuilt_events:
            mark_events_current(session)  # a fresh database is current by definition
        # after the events: rebuilding them can flag gas flares, which the cells count separately
        hours_filled = filled_hours > 0
        if rebuilt_events or hours_filled or _meta(session, "cells_version") != CELLS_VERSION:
            from app.services.cells import bump_data_version, rebuild_all_cells

            t = time.time()
            n = rebuild_all_cells(session)
            session.merge(AppMetaRow(key="cells_version", value=CELLS_VERSION))
            bump_data_version(session)
            if n:
                log.info("map pyramid built (v%s): %d cells in %.1fs", CELLS_VERSION, n, time.time() - t)
        session.commit()
