"""ORM tables. All datetimes are stored as naive UTC.

SQLite is the default for development; the schema is plain SQL types so it runs on
PostgreSQL unchanged (PostGIS geometry columns are a planned upgrade, see docs).
"""

from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, Float, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class DetectionRow(Base):
    __tablename__ = "detections"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    source: Mapped[str] = mapped_column(String(32))
    instrument: Mapped[str] = mapped_column(String(8))
    satellite: Mapped[str] = mapped_column(String(32))
    processing_level: Mapped[str] = mapped_column(String(8))
    latitude: Mapped[float] = mapped_column(Float)
    longitude: Mapped[float] = mapped_column(Float)
    acquired_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    # Whole hours since 1970-01-01 UTC: the time key shared with fire_cells and the analysis views.
    acq_hour: Mapped[int] = mapped_column(Integer, index=True)
    daynight: Mapped[str | None] = mapped_column(String(1))
    brightness_k: Mapped[float | None] = mapped_column(Float)
    brightness_secondary_k: Mapped[float | None] = mapped_column(Float)
    brightness_saturated: Mapped[bool] = mapped_column(Boolean, default=False)
    frp_mw: Mapped[float | None] = mapped_column(Float)
    scan_km: Mapped[float] = mapped_column(Float)
    track_km: Mapped[float] = mapped_column(Float)
    pixel_area_km2: Mapped[float] = mapped_column(Float)
    nominal_resolution_m: Mapped[int] = mapped_column(Integer)
    confidence_raw: Mapped[str] = mapped_column(String(8))
    confidence_pct: Mapped[int] = mapped_column(Integer)
    confidence_level: Mapped[str] = mapped_column(String(8))
    detection_type: Mapped[int | None] = mapped_column(Integer)
    static_kind: Mapped[str | None] = mapped_column(String(12))  # volcano | industrial | offshore
    version: Mapped[str | None] = mapped_column(String(16))
    event_id: Mapped[str | None] = mapped_column(String(32), index=True)
    ingested_at: Mapped[datetime] = mapped_column(DateTime)

    __table_args__ = (
        Index("ix_detections_lat_lon", "latitude", "longitude"),
        # Per-sensor access path: MODIS and VIIRS are analysed separately (views modis_/viirs_detections).
        Index("ix_detections_instrument_hour", "instrument", "acq_hour"),
    )


class FireCellRow(Base):
    """Pre-aggregated detections: one row per hour x grid cell x sensor x confidence class.

    This is the zoom pyramid the globe draws from. Level k cells are 180/2^k degrees, aligned to
    -180/-90, so a level-k cell's parent is (cx >> 1, cy >> 1). A map tile at zoom z shows the
    16 x 16 cells of level z + 4 that share its (tx, ty) = (cx >> 4, cy >> 4). Sums (not means) are
    stored so any set of hours and confidence classes adds up exactly; centroid = lat_sum / n.
    Derived data: rebuilt from `detections` for every hour that receives new detections.
    """

    __tablename__ = "fire_cells"

    level: Mapped[int] = mapped_column(Integer, primary_key=True)
    tx: Mapped[int] = mapped_column(Integer, primary_key=True)
    ty: Mapped[int] = mapped_column(Integer, primary_key=True)
    hour: Mapped[int] = mapped_column(Integer, primary_key=True)
    cx: Mapped[int] = mapped_column(Integer, primary_key=True)
    cy: Mapped[int] = mapped_column(Integer, primary_key=True)
    instrument: Mapped[str] = mapped_column(String(8), primary_key=True)
    confidence_level: Mapped[str] = mapped_column(String(8), primary_key=True)
    is_static: Mapped[bool] = mapped_column(Boolean, primary_key=True)
    n: Mapped[int] = mapped_column(Integer)
    frp_sum: Mapped[float] = mapped_column(Float)
    frp_max: Mapped[float] = mapped_column(Float)
    lat_sum: Mapped[float] = mapped_column(Float)
    lon_sum: Mapped[float] = mapped_column(Float)

    __table_args__ = (Index("ix_fire_cells_hour", "hour"), {"sqlite_with_rowid": False})


class EventRow(Base):
    __tablename__ = "fire_events"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    latitude: Mapped[float] = mapped_column(Float)
    longitude: Mapped[float] = mapped_column(Float)
    west: Mapped[float] = mapped_column(Float)
    south: Mapped[float] = mapped_column(Float)
    east: Mapped[float] = mapped_column(Float)
    north: Mapped[float] = mapped_column(Float)
    start_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    end_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    detection_count: Mapped[int] = mapped_column(Integer)
    modis_count: Mapped[int] = mapped_column(Integer)
    viirs_count: Mapped[int] = mapped_column(Integer)
    max_frp_mw: Mapped[float] = mapped_column(Float)
    total_frp_mw: Mapped[float] = mapped_column(Float)
    area_km2: Mapped[float] = mapped_column(Float)
    area_basis: Mapped[str] = mapped_column(String(16), default="viirs_375m")
    max_confidence_pct: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(10))  # legacy two-value view of lifecycle_state
    # --- event model v3 (docs/FIRE_EVENT_MODEL.md)
    lifecycle_state: Mapped[str] = mapped_column(String(12), default="active")  # active|reactivated|inactive|archived
    run_start_at: Mapped[datetime | None] = mapped_column(DateTime)  # start of the latest uninterrupted run
    observed_hours: Mapped[float] = mapped_column(Float, default=0.0)
    pass_count: Mapped[int] = mapped_column(Integer, default=0)
    evidence: Mapped[str] = mapped_column(String(16), default="single_detection")
    satellite_counts: Mapped[str] = mapped_column(Text, default="{}")  # JSON {satellite: detections}
    mean_frp_viirs_mw: Mapped[float | None] = mapped_column(Float)
    mean_frp_modis_mw: Mapped[float | None] = mapped_column(Float)
    peak_pass_frp_mw: Mapped[float] = mapped_column(Float, default=0.0)
    peak_pass_sensor: Mapped[str | None] = mapped_column(String(8))
    intensity_trend: Mapped[str] = mapped_column(String(20), default="insufficient_data")
    extent_km: Mapped[float] = mapped_column(Float, default=0.0)
    detection_hull_km2: Mapped[float] = mapped_column(Float, default=0.0)
    footprint_growth_km2_per_day: Mapped[float | None] = mapped_column(Float)
    footprint_growth_se: Mapped[float | None] = mapped_column(Float)
    version: Mapped[int] = mapped_column(Integer, default=1)  # bumped on every recorded change
    updated_at: Mapped[datetime | None] = mapped_column(DateTime)

    __table_args__ = (Index("ix_events_lat_lon", "latitude", "longitude"),)


class EventPassRow(Base):
    """Per-overpass intensity timeline of an event: the whole-fire FRP one satellite pass saw."""

    __tablename__ = "event_passes"

    event_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    acquired_at: Mapped[datetime] = mapped_column(DateTime, primary_key=True)
    satellite: Mapped[str] = mapped_column(String(32), primary_key=True)
    instrument: Mapped[str] = mapped_column(String(8))
    pixels: Mapped[int] = mapped_column(Integer)
    frp_sum: Mapped[float] = mapped_column(Float)
    frp_max: Mapped[float] = mapped_column(Float)


class EventLineageRow(Base):
    """Where an event id went when it stopped being canonical: merged into, split into, renamed to,
    or removed (all its detections reclassified as non-fire). Old ids keep resolving through it."""

    __tablename__ = "event_lineage"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    old_id: Mapped[str] = mapped_column(String(32), index=True)
    new_id: Mapped[str | None] = mapped_column(String(32), index=True)
    kind: Mapped[str] = mapped_column(String(12))  # merged | split | renamed | removed
    reason: Mapped[str] = mapped_column(String(32))  # new_detections | static_reclassified | rule_change
    detections_moved: Mapped[int] = mapped_column(Integer, default=0)
    at: Mapped[datetime] = mapped_column(DateTime, index=True)


class EventHistoryRow(Base):
    """One row per recorded change of an event: created, grown, state change, merged, split..."""

    __tablename__ = "event_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(32), index=True)
    version: Mapped[int] = mapped_column(Integer)
    at: Mapped[datetime] = mapped_column(DateTime, index=True)
    change: Mapped[str] = mapped_column(String(16))  # created|grown|updated|state|merged|split|renamed|removed
    from_state: Mapped[str | None] = mapped_column(String(12))
    to_state: Mapped[str | None] = mapped_column(String(12))
    related_id: Mapped[str | None] = mapped_column(String(32))
    reason: Mapped[str | None] = mapped_column(String(32))
    snapshot: Mapped[str] = mapped_column(Text)  # JSON of the event's metrics at that version


class AlertRuleRow(Base):
    __tablename__ = "alert_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(120))
    west: Mapped[float | None] = mapped_column(Float)
    south: Mapped[float | None] = mapped_column(Float)
    east: Mapped[float | None] = mapped_column(Float)
    north: Mapped[float | None] = mapped_column(Float)
    center_lat: Mapped[float | None] = mapped_column(Float)
    center_lon: Mapped[float | None] = mapped_column(Float)
    radius_km: Mapped[float | None] = mapped_column(Float)
    min_confidence: Mapped[str] = mapped_column(String(8), default="nominal")
    min_frp_mw: Mapped[float] = mapped_column(Float, default=0)
    min_area_km2: Mapped[float] = mapped_column(Float, default=0)
    min_detections: Mapped[int] = mapped_column(Integer, default=1)
    webhook_url: Mapped[str | None] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime)


class AlertRow(Base):
    __tablename__ = "alerts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    rule_id: Mapped[int] = mapped_column(Integer, index=True)
    rule_name: Mapped[str] = mapped_column(String(120))
    event_id: Mapped[str] = mapped_column(String(32))
    severity: Mapped[str] = mapped_column(String(10))
    message: Mapped[str] = mapped_column(Text)
    latitude: Mapped[float] = mapped_column(Float)
    longitude: Mapped[float] = mapped_column(Float)
    area_km2: Mapped[float] = mapped_column(Float)
    max_frp_mw: Mapped[float] = mapped_column(Float)
    triggered_at: Mapped[datetime] = mapped_column(DateTime, index=True)

    __table_args__ = (UniqueConstraint("rule_id", "event_id", name="uq_alert_rule_event"),)


class IngestRunRow(Base):
    __tablename__ = "ingest_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    started_at: Mapped[datetime] = mapped_column(DateTime)
    finished_at: Mapped[datetime] = mapped_column(DateTime)
    demo: Mapped[bool] = mapped_column(Boolean)
    summary: Mapped[str] = mapped_column(Text)


class CoverageRow(Base):
    """What was actually fetched: a day of one FIRMS product over one area.

    Needed because "no detections" is ambiguous: no fires, or never loaded. The map's
    History mode uses it to tell the user (and load the archive) when a day is missing.
    """

    __tablename__ = "coverage"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    day: Mapped[date] = mapped_column(Date, index=True)
    west: Mapped[float] = mapped_column(Float)
    south: Mapped[float] = mapped_column(Float)
    east: Mapped[float] = mapped_column(Float)
    north: Mapped[float] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime)

    __table_args__ = (UniqueConstraint("day", "west", "south", "east", "north", "source", name="uq_coverage"),)


class AppMetaRow(Base):
    """Key/value bookkeeping, e.g. which version of the event-building logic built fire_events."""

    __tablename__ = "app_meta"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
