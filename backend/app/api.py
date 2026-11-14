from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Literal

import asyncio

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.ingest.firms_client import FirmsError
from app.ingest.sources import PUBLIC_REGIONS, SOURCES
from app.models import AlertRow, AlertRuleRow, DetectionRow, EventRow, IngestRunRow
from app.schemas import (
    Alert,
    AlertRule,
    AlertRuleIn,
    CalendarDay,
    FireDetection,
    FireEvent,
    FireEventDetail,
    IngestReport,
    IngestRequest,
)
from app.services import queries
from app.services.ndvi import CopernicusNdvi, NdviResult
from app.services.history import COUNTRY_BBOX, BackfillJob, BackfillRequest, run_backfill
from app.services.pipeline import firms_client, run_ingest
from app.services.risk import RiskForecast, activity_trend, compute_risk, fetch_weather

router = APIRouter(prefix="/api")


def get_session(request: Request):
    with request.app.state.session_factory() as session:
        yield session


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_http(request: Request) -> httpx.AsyncClient:
    return request.app.state.http


SessionDep = Annotated[Session, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
HttpDep = Annotated[httpx.AsyncClient, Depends(get_http)]


def area_params(
    bbox: Annotated[str | None, Query(description="west,south,east,north")] = None,
    lat: Annotated[float | None, Query(ge=-90, le=90)] = None,
    lon: Annotated[float | None, Query(ge=-180, le=180)] = None,
    radius_km: Annotated[float | None, Query(gt=0, le=5000)] = None,
) -> queries.Area | None:
    try:
        parsed = queries.parse_bbox(bbox)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    area = queries.Area(bbox=parsed, lat=lat, lon=lon, radius_km=radius_km)
    if (lat is None) != (lon is None) or (radius_km is not None and lat is None):
        raise HTTPException(422, "lat, lon and radius_km must be given together")
    return area if (parsed or area.has_radius) else None


AreaDep = Annotated[queries.Area | None, Depends(area_params)]


@router.get("/health")
def health(request: Request, session: SessionDep, settings: SettingsDep):
    last = session.scalars(select(IngestRunRow).order_by(IngestRunRow.id.desc()).limit(1)).first()
    return {
        "status": "ok",
        "demo_mode": settings.is_demo,
        # demo | firms_public (keyless, last 7 days) | firms_api (MAP_KEY: history + any area)
        "data_source": settings.data_source,
        "history_enabled": bool(settings.firms_map_key) and not settings.is_demo,
        "public_region": settings.public_region,
        "seeding": getattr(request.app.state, "seeding", False),
        "copernicus_enabled": settings.copernicus_enabled,
        "detections": session.scalar(select(func.count()).select_from(DetectionRow)),
        "events": session.scalar(select(func.count()).select_from(EventRow)),
        "last_ingest": last.finished_at.replace(tzinfo=UTC) if last else None,
    }


@router.get("/sources")
def sources():
    return {"sources": [vars(s) for s in SOURCES.values()], "public_regions": PUBLIC_REGIONS}


@router.post("/ingest", response_model=IngestReport)
async def ingest(req: IngestRequest, request: Request, settings: SettingsDep, http: HttpDep):
    return await run_ingest(request.app.state.session_factory, settings, http, req)


def detection_filter(
    area: AreaDep,
    start: datetime | None = None,
    end: datetime | None = None,
    instrument: Annotated[list[Literal["MODIS", "VIIRS"]] | None, Query()] = None,
    satellite: Annotated[list[str] | None, Query()] = None,
    min_confidence: Literal["low", "nominal", "high"] | None = None,
    min_frp: float | None = None,
    max_frp: float | None = None,
    daynight: Literal["D", "N"] | None = None,
    event_id: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100000)] = 5000,
) -> queries.DetectionFilter:
    return queries.DetectionFilter(
        start=start, end=end, area=area, instruments=instrument, satellites=satellite,
        min_confidence=min_confidence, min_frp=min_frp, max_frp=max_frp, daynight=daynight,
        event_id=event_id, limit=limit,
    )


DetectionFilterDep = Annotated[queries.DetectionFilter, Depends(detection_filter)]


@router.get("/detections", response_model=list[FireDetection])
def list_detections(session: SessionDep, f: DetectionFilterDep):
    return queries.query_detections(session, f)


@router.get("/detections/compact")
def list_detections_compact(session: SessionDep, f: DetectionFilterDep):
    """Columnar arrays (id, lat, lon, frp, conf 0-2, viirs 0/1, t epoch s) for map rendering."""
    return queries.query_detections_compact(session, f)


@router.get("/detections/{detection_id}", response_model=FireDetection)
def get_detection(detection_id: str, session: SessionDep):
    row = session.get(DetectionRow, detection_id)
    if not row:
        raise HTTPException(404, "detection not found")
    return FireDetection.model_validate(row)


@router.get("/events", response_model=list[FireEvent])
def list_events(
    session: SessionDep,
    area: AreaDep,
    start: datetime | None = None,
    end: datetime | None = None,
    min_area: float | None = None,
    max_area: float | None = None,
    min_frp: float | None = None,
    min_detections: int | None = None,
    min_confidence_pct: Annotated[int | None, Query(ge=0, le=100)] = None,
    status: Literal["active", "inactive"] | None = None,
    instrument: Literal["MODIS", "VIIRS"] | None = None,
    sort: Literal["recent", "area", "frp"] = "recent",
    limit: Annotated[int, Query(ge=1, le=10000)] = 1000,
):
    f = queries.EventFilter(
        start=start, end=end, area=area, min_area=min_area, max_area=max_area, min_frp=min_frp,
        min_detections=min_detections, min_confidence_pct=min_confidence_pct, status=status,
        instrument=instrument, sort=sort, limit=limit,
    )
    return queries.query_events(session, f)


@router.get("/events/{event_id}", response_model=FireEventDetail)
def get_event(event_id: str, session: SessionDep):
    row = session.get(EventRow, event_id)
    if not row:
        raise HTTPException(404, "event not found")
    dets = queries.query_detections(session, queries.DetectionFilter(event_id=event_id, limit=50000))
    return FireEventDetail(**FireEvent.model_validate(row).model_dump(), detections=dets)


@router.get("/calendar", response_model=list[CalendarDay])
def get_calendar(session: SessionDep, area: AreaDep, start: date | None = None, end: date | None = None):
    end = end or datetime.now(UTC).date()
    start = start or end - timedelta(days=29)
    if start > end or (end - start).days > 400:
        raise HTTPException(422, "range must be 1-400 days and start <= end")
    return queries.calendar(session, start, end, area)


@router.get("/alerts/rules", response_model=list[AlertRule])
def list_rules(session: SessionDep):
    return session.scalars(select(AlertRuleRow).order_by(AlertRuleRow.id)).all()


@router.post("/alerts/rules", response_model=AlertRule, status_code=201)
def create_rule(rule: AlertRuleIn, session: SessionDep):
    bbox = [rule.west, rule.south, rule.east, rule.north]
    circle = [rule.center_lat, rule.center_lon, rule.radius_km]
    if any(v is not None for v in bbox) and None in bbox:
        raise HTTPException(422, "bbox needs west, south, east and north")
    if any(v is not None for v in circle) and None in circle:
        raise HTTPException(422, "circle needs center_lat, center_lon and radius_km")
    if rule.webhook_url and not rule.webhook_url.startswith(("https://", "http://")):
        raise HTTPException(422, "webhook_url must be http(s)")
    row = AlertRuleRow(**rule.model_dump(), created_at=datetime.now(UTC).replace(tzinfo=None))
    session.add(row)
    session.commit()
    return row


@router.delete("/alerts/rules/{rule_id}", status_code=204)
def delete_rule(rule_id: int, session: SessionDep):
    if not session.get(AlertRuleRow, rule_id):
        raise HTTPException(404, "rule not found")
    session.execute(delete(AlertRuleRow).where(AlertRuleRow.id == rule_id))
    session.commit()


@router.get("/alerts", response_model=list[Alert])
def list_alerts(session: SessionDep, limit: Annotated[int, Query(ge=1, le=1000)] = 100):
    return session.scalars(select(AlertRow).order_by(AlertRow.triggered_at.desc(), AlertRow.id.desc()).limit(limit)).all()


@router.get("/risk", response_model=RiskForecast)
async def get_risk(
    request: Request,
    settings: SettingsDep,
    http: HttpDep,
    lat: Annotated[float, Query(ge=-90, le=90)],
    lon: Annotated[float, Query(ge=-180, le=180)],
):
    try:
        daily = await fetch_weather(http, settings.open_meteo_url, lat, lon)
    except (httpx.HTTPError, KeyError) as exc:
        raise HTTPException(502, f"weather provider unavailable: {exc}") from None

    now = datetime.now(UTC)

    def _activity():
        with request.app.state.session_factory() as s:
            last = queries.nearby_activity(s, lat, lon, 50, now - timedelta(days=7), now)
            prev = queries.nearby_activity(s, lat, lon, 50, now - timedelta(days=14), now - timedelta(days=7))
            return last, prev

    last, prev = await run_in_threadpool(_activity)
    ndvi = None
    ndvi_client: CopernicusNdvi | None = request.app.state.ndvi
    if ndvi_client:
        try:
            ndvi = (await ndvi_client.mean_ndvi(lat, lon)).ndvi_mean
        except httpx.HTTPError:
            ndvi = None  # fuel term is optional; degrade gracefully
    return RiskForecast(
        latitude=lat,
        longitude=lon,
        nearby_detections_7d=last,
        nearby_detections_prev_7d=prev,
        activity_trend=activity_trend(last, prev),
        ndvi=ndvi,
        days=compute_risk(daily, now.date(), last, ndvi),
    )


@router.get("/ndvi", response_model=NdviResult)
async def get_ndvi(
    request: Request,
    lat: Annotated[float, Query(ge=-90, le=90)],
    lon: Annotated[float, Query(ge=-180, le=180)],
):
    client: CopernicusNdvi | None = request.app.state.ndvi
    if not client:
        raise HTTPException(501, "Copernicus credentials not configured")
    try:
        return await client.mean_ndvi(lat, lon)
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"Copernicus request failed: {exc}") from None


# ---------------------------------------------------------------------------- history


def _require_history(settings: Settings) -> None:
    if settings.is_demo:
        raise HTTPException(409, "History backfill is disabled in demo mode.")
    if not settings.firms_map_key:
        raise HTTPException(
            400,
            "History needs a FIRMS MAP_KEY (free: https://firms.modaps.eosdis.nasa.gov/api/map_key/). "
            "Set FIRELENS_FIRMS_MAP_KEY in .env and restart the API. Without it, FireLens keeps the last 7 days "
            "of real data from the public FIRMS files.",
        )


@router.get("/history/availability")
async def history_availability(settings: SettingsDep, http: HttpDep):
    """Date span FIRMS serves per product, plus the MAP_KEY's transaction usage."""
    _require_history(settings)
    client = firms_client(settings, http)
    try:
        spans = await client.data_availability()
    except FirmsError as exc:
        raise HTTPException(502, str(exc)) from None
    try:
        status = await client.key_status()
    except FirmsError:
        status = None
    return {
        "products": {k: {"min_date": v[0].isoformat(), "max_date": v[1].isoformat()} for k, v in spans.items()},
        "key_status": status,
    }


@router.get("/history/countries")
def history_countries():
    """Countries loadable by name; each maps to a bounding box for the FIRMS area API."""
    return [{"code": k, "name": v[0], "bbox": v[1]} for k, v in COUNTRY_BBOX.items()]


@router.post("/history/backfill", status_code=202)
async def start_backfill(req: BackfillRequest, request: Request, settings: SettingsDep, http: HttpDep):
    _require_history(settings)
    jobs: dict[str, BackfillJob] = request.app.state.jobs
    if any(j.status in ("queued", "running") for j in jobs.values()):
        raise HTTPException(409, "A backfill is already running; wait for it or cancel it.")
    job = BackfillJob(request=req)
    jobs[job.id] = job
    job.task = asyncio.create_task(run_backfill(job, request.app.state.session_factory, settings, http))
    return job.public()


@router.get("/history/jobs")
def list_jobs(request: Request):
    jobs = sorted(request.app.state.jobs.values(), key=lambda j: j.created_at, reverse=True)
    return [j.public() for j in jobs]


@router.get("/history/jobs/{job_id}")
def get_job(job_id: str, request: Request):
    job = request.app.state.jobs.get(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    return job.public()


@router.delete("/history/jobs/{job_id}", status_code=202)
def cancel_job(job_id: str, request: Request):
    job = request.app.state.jobs.get(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    if job.task and not job.task.done():
        job.task.cancel()
    return job.public()
