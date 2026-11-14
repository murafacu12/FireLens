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
    version: Mapped[str | None] = mapped_column(String(16))
    event_id: Mapped[str | None] = mapped_column(String(32), index=True)
    ingested_at: Mapped[datetime] = mapped_column(DateTime)

    __table_args__ = (Index("ix_detections_lat_lon", "latitude", "longitude"),)


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
    max_confidence_pct: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(10))

    __table_args__ = (Index("ix_events_lat_lon", "latitude", "longitude"),)


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
