"""Canonical (harmonized) data model shared by ingestion, storage and the API."""

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Instrument = Literal["MODIS", "VIIRS"]
ConfidenceLevel = Literal["low", "nominal", "high"]
CONFIDENCE_ORDER: dict[str, int] = {"low": 0, "nominal": 1, "high": 2}


def as_utc(value: datetime) -> datetime:
    """Storage keeps naive UTC; everything leaving the domain is timezone-aware UTC."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def to_naive_utc(value: datetime) -> datetime:
    return as_utc(value).replace(tzinfo=None)


class _UtcModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    @field_validator("*", mode="after")
    @classmethod
    def utc_datetimes(cls, value):
        return as_utc(value) if isinstance(value, datetime) else value


class FireDetection(_UtcModel):
    """One active-fire pixel, harmonized across MODIS and VIIRS."""

    id: str
    source: str
    instrument: Instrument
    satellite: str
    processing_level: str  # NRT | SP
    latitude: float
    longitude: float
    acquired_at: datetime
    daynight: Literal["D", "N"] | None = None
    # MODIS: band 21/22 (~4 um). VIIRS: I4 (3.74 um). Not directly comparable across sensors.
    brightness_k: float | None = None
    # MODIS: band 31 (11 um). VIIRS: I5 (11.45 um).
    brightness_secondary_k: float | None = None
    brightness_saturated: bool = False
    frp_mw: float | None = None
    scan_km: float
    track_km: float
    pixel_area_km2: float
    nominal_resolution_m: int
    confidence_raw: str
    confidence_pct: int
    confidence_level: ConfidenceLevel
    detection_type: int | None = None
    version: str | None = None
    event_id: str | None = None


class FireEvent(_UtcModel):
    """A spatio-temporal cluster of detections from any sensor: one fire."""

    id: str
    latitude: float
    longitude: float
    west: float
    south: float
    east: float
    north: float
    start_at: datetime
    end_at: datetime
    detection_count: int
    modis_count: int
    viirs_count: int
    max_frp_mw: float
    total_frp_mw: float
    area_km2: float
    max_confidence_pct: int
    status: Literal["active", "inactive"]


class FireEventDetail(FireEvent):
    detections: list[FireDetection]


class CalendarDay(BaseModel):
    date: str
    detections: int
    events_started: int
    total_frp_mw: float
    max_frp_mw: float


class IngestRequest(BaseModel):
    sources: list[str] | None = None
    area: str | None = Field(None, description="API mode: 'world' or 'west,south,east,north'")
    days: int | None = Field(None, ge=1, le=31)
    date: str | None = Field(None, description="API mode: start date YYYY-MM-DD")
    region: str | None = Field(None, description="Keyless mode: FIRMS public-file region, e.g. South_America")
    window: Literal["24h", "48h", "7d"] | None = Field(None, description="Keyless mode: public-file window")


class SourceReport(BaseModel):
    source: str
    rows: int
    accepted: int
    rejected: int
    error: str | None = None


class IngestReport(BaseModel):
    demo: bool
    source_mode: str = "demo"  # demo | firms_public | firms_api
    sources: list[SourceReport]
    stored: int
    events_rebuilt: int
    alerts_triggered: int


class AlertRuleIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    west: float | None = Field(None, ge=-180, le=180)
    south: float | None = Field(None, ge=-90, le=90)
    east: float | None = Field(None, ge=-180, le=180)
    north: float | None = Field(None, ge=-90, le=90)
    center_lat: float | None = Field(None, ge=-90, le=90)
    center_lon: float | None = Field(None, ge=-180, le=180)
    radius_km: float | None = Field(None, gt=0, le=2000)
    min_confidence: ConfidenceLevel = "nominal"
    min_frp_mw: float = 0
    min_area_km2: float = 0
    min_detections: int = 1
    webhook_url: str | None = None
    enabled: bool = True


class AlertRule(AlertRuleIn, _UtcModel):
    id: int
    created_at: datetime


class Alert(_UtcModel):
    id: int
    rule_id: int
    rule_name: str
    event_id: str
    severity: Literal["info", "warning", "critical"]
    message: str
    latitude: float
    longitude: float
    area_km2: float
    max_frp_mw: float
    triggered_at: datetime
