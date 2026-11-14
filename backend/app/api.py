import asyncio
import csv
import hashlib
import io
import json
import threading
from collections import OrderedDict
from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Literal

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import Session

from app.config import Settings
from app.ingest.firms_client import FirmsError
from app.ingest.sources import PUBLIC_REGIONS, SOURCES
from app.models import AlertRow, AlertRuleRow, DetectionRow, EventHistoryRow, EventLineageRow, EventPassRow, EventRow, IngestRunRow
from app.schemas import (
    Alert,
    AlertRule,
    AlertRuleIn,
    CalendarDay,
    EventHistoryEntry,
    EventLineage,
    EventPass,
    FireDetection,
    FireEvent,
    FireEventDetail,
    IngestReport,
    IngestRequest,
)
from app.services import cells, queries
from app.services.ndvi import CopernicusNdvi, NdviResult
from app.services.alerts import deliver_webhooks, evaluate_rules
from app.services.coverage import day_status
from app.services.history import COUNTRY_BBOX, MAX_BACKFILL_DAYS, BackfillJob, BackfillRequest, run_backfill
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
        # newest satellite observation in the database: how fresh "Latest" really is
        "latest_detection_at": (newest.replace(tzinfo=UTC) if (newest := session.scalar(select(func.max(DetectionRow.acquired_at)))) else None),
        "refresh_minutes": settings.ingest_interval_minutes if not settings.is_demo else 0,
        "event_active_hours": settings.event_active_hours,
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
    include_static: Annotated[bool, Query(description="Include volcanoes, gas flares and offshore platforms")] = False,
    limit: Annotated[int, Query(ge=1, le=100000)] = 5000,
) -> queries.DetectionFilter:
    return queries.DetectionFilter(
        start=start, end=end, area=area, instruments=instrument, satellites=satellite,
        min_confidence=min_confidence, min_frp=min_frp, max_frp=max_frp, daynight=daynight,
        event_id=event_id, include_static=include_static, limit=limit,
    )


DetectionFilterDep = Annotated[queries.DetectionFilter, Depends(detection_filter)]


@router.get("/detections", response_model=list[FireDetection])
def list_detections(session: SessionDep, f: DetectionFilterDep):
    return queries.query_detections(session, f)


@router.get("/detections/compact")
def list_detections_compact(session: SessionDep, f: DetectionFilterDep):
    """Columnar arrays (id, lat, lon, frp, conf 0-2, viirs 0/1, t epoch s) for map rendering."""
    return queries.query_detections_compact(session, f)


@router.get("/detections/grid")
def detection_grid(
    session: SessionDep,
    f: DetectionFilterDep,
    cell_deg: Annotated[float, Query(ge=0.01, le=10, description="Cell size in degrees")] = 1.0,
):
    """Grouped markers for the zoomed-out map: per-cell count, intensity and centroid.
    `total` is the exact number of matching detections (no row limit applies)."""
    return queries.query_detection_grid(session, f, cell_deg)


@router.get("/detections/{detection_id}", response_model=FireDetection)
def get_detection(detection_id: str, session: SessionDep):
    row = session.get(DetectionRow, detection_id)
    if not row:
        raise HTTPException(404, "detection not found")
    return FireDetection.model_validate(row)


def event_filter(
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
    min_evidence: Annotated[Literal["single_detection", "single_pass", "multi_pass"] | None,
                            Query(description="single_pass hides lone pixels; multi_pass keeps fires seen on 2+ overpasses")] = None,
    lifecycle_state: Literal["active", "reactivated", "inactive", "archived"] | None = None,
    sort: Literal["recent", "area", "frp"] = "recent",
    limit: Annotated[int, Query(ge=1, le=10000)] = 1000,
) -> queries.EventFilter:
    return queries.EventFilter(
        start=start, end=end, area=area, min_area=min_area, max_area=max_area, min_frp=min_frp,
        min_detections=min_detections, min_confidence_pct=min_confidence_pct, status=status,
        instrument=instrument, min_evidence=min_evidence, lifecycle_state=lifecycle_state, sort=sort, limit=limit,
    )


EventFilterDep = Annotated[queries.EventFilter, Depends(event_filter)]


@router.get("/events", response_model=list[FireEvent])
def list_events(session: SessionDep, f: EventFilterDep):
    return queries.query_events(session, f)


@router.get("/events/count")
def events_count(session: SessionDep, f: EventFilterDep):
    """Exact number of matching events (the list endpoint is capped by `limit`)."""
    return {"count": queries.count_events(session, f)}


def _canonical(session: Session, event_id: str) -> str:
    cid = queries.resolve_event_id(session, event_id)
    if cid is None:
        raise HTTPException(404, "event not found")
    return cid


@router.get("/events/{event_id}", response_model=FireEventDetail)
def get_event(event_id: str, session: SessionDep):
    """An event with its detections. An id that was merged or renamed resolves to the event that holds
    its detections now (`resolved_from` = the id asked for), so old links and alerts keep working."""
    cid = _canonical(session, event_id)
    row = session.get(EventRow, cid)
    dets = queries.query_detections(session, queries.DetectionFilter(event_id=cid, limit=50000))
    data = FireEvent.model_validate(row).model_dump(exclude={"detected_footprint_km2", "burned_area_km2"})
    return FireEventDetail(**data, detections=dets, resolved_from=event_id if cid != event_id else None)


@router.get("/events/{event_id}/timeline", response_model=list[EventPass])
def event_timeline(event_id: str, session: SessionDep):
    """Whole-fire FRP per satellite overpass (instantaneous MW; MODIS and VIIRS are not cross-calibrated)."""
    cid = _canonical(session, event_id)
    return session.scalars(select(EventPassRow).where(EventPassRow.event_id == cid).order_by(EventPassRow.acquired_at)).all()


@router.get("/events/{event_id}/history", response_model=list[EventHistoryEntry])
def event_history(event_id: str, session: SessionDep):
    """Every recorded change of this event and of the ids merged into it, oldest first."""
    cid = queries.resolve_event_id(session, event_id) or event_id
    ids = {cid, event_id} | set(session.scalars(select(EventLineageRow.old_id).where(EventLineageRow.new_id == cid)))
    rows = session.scalars(select(EventHistoryRow).where(EventHistoryRow.event_id.in_(ids))
                           .order_by(EventHistoryRow.at, EventHistoryRow.id)).all()
    if not rows:
        raise HTTPException(404, "event not found")
    return rows


@router.get("/events/{event_id}/lineage", response_model=list[EventLineage])
def event_lineage(event_id: str, session: SessionDep):
    """Lineage records naming this id: what it absorbed (new_id = id) and what it became (old_id = id)."""
    return session.scalars(select(EventLineageRow).where((EventLineageRow.old_id == event_id) | (EventLineageRow.new_id == event_id))
                           .order_by(EventLineageRow.at, EventLineageRow.id)).all()


# ---------------------------------------------------------------------------- map tiles

TILE_CACHE_MAX = 4096  # built tiles kept in memory (a tile is ~1-60 KB before gzip)
_tile_lock = threading.Lock()


@router.get("/tiles/meta")
def tiles_meta(session: SessionDep):
    """What the globe needs to know to reuse its tiles: they stay valid until `data_version` changes
    (new detections were stored) or, for rolling "latest" windows, until the UTC `hour` changes."""
    return {
        "data_version": cells.data_version(session),
        "hour": cells.epoch_hour(datetime.now(UTC)),
        "tile_shift": cells.TILE_SHIFT,
        "min_level": cells.MIN_LEVEL,
        "max_cell_zoom": cells.MAX_CELL_ZOOM,
        "point_zoom": cells.POINT_ZOOM,
    }


@router.get("/tiles/{kind}/{z}/{x}/{y}")
def fire_tile(
    kind: Literal["cells", "points"],
    z: int,
    x: int,
    y: int,
    request: Request,
    session: SessionDep,
    f: DetectionFilterDep,
    ef: EventFilterDep,
    hours: Annotated[int | None, Query(ge=1, le=24 * 400, description="rolling window back from now")] = None,
):
    """One map tile (geographic grid: zoom z has 2^(z+1) x 2^z tiles of 180/2^z degrees).

    `cells` (z 0..7): 16 x 16 grouped markers, MODIS and VIIRS reported separately. `points`
    (z = 7 only, ~150 km): the individual detections and fire events. Tiles are cached and carry an
    ETag, so the browser re-downloads a tile only when its content changed.
    """
    if kind == "cells" and not 0 <= z <= cells.MAX_CELL_ZOOM:
        raise HTTPException(422, f"cell tiles exist for zoom 0-{cells.MAX_CELL_ZOOM}")
    if kind == "points" and z != cells.POINT_ZOOM:
        raise HTTPException(422, f"point tiles exist for zoom {cells.POINT_ZOOM} only")
    if not cells.valid_tile(z, x, y):
        raise HTTPException(404, "no such tile")
    h0, h1 = cells.resolve_hours(hours, f.start, f.end, datetime.now(UTC))
    params = tuple(sorted((k, v) for k, v in request.query_params.multi_items() if k not in ("hours", "start", "end")))
    key = (cells.data_version(session), kind, z, x, y, h0, h1, params)
    cache: OrderedDict = request.app.state.tile_cache
    with _tile_lock:
        hit = cache.get(key)
        if hit:
            cache.move_to_end(key)
    if hit is None:
        start, end = cells.hour_start(h0), cells.hour_start(h1 + 1) - timedelta(microseconds=1)
        f.start, f.end, ef.start, ef.end = start, end, start, end
        if kind == "points":
            data = cells.point_tile(session, x, y, f, ef)
        elif z + cells.TILE_SHIFT <= cells.MAX_STORED_LEVEL and not cells.needs_raw(f):
            data = cells.stored_cell_tile(session, z, x, y, h0, h1, f.min_confidence, f.include_static)
        else:
            data = cells.raw_cell_tile(session, z, x, y, f)
        body = json.dumps(data, separators=(",", ":")).encode()
        hit = (body, f'"{hashlib.sha1(body).hexdigest()[:20]}"')
        with _tile_lock:
            cache[key] = hit
            while len(cache) > TILE_CACHE_MAX:
                cache.popitem(last=False)
    body, etag = hit
    headers = {"ETag": etag, "Cache-Control": "no-cache"}  # always revalidate; unchanged tiles cost a 304
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    return Response(content=body, media_type="application/json", headers=headers)


# ---------------------------------------------------------------------------- analysis export

EXPORT_MAX_ROWS = 2_000_000


@router.get("/export/{sensor}.csv")
def export_sensor_csv(
    sensor: Literal["modis", "viirs"],
    request: Request,
    start: datetime,
    end: datetime,
    bbox: Annotated[str | None, Query(description="west,south,east,north")] = None,
    include_static: bool = False,
):
    """Detections of one sensor with that sensor's native field names (views modis_detections /
    viirs_detections), streamed as CSV for offline analysis."""
    try:
        box = queries.parse_bbox(bbox)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    if end < start or (end - start).days > 400:
        raise HTTPException(422, "range must be at most 400 days and start <= end")
    where = ["acquired_at >= :start", "acquired_at <= :end"]
    args: dict = {"start": start.astimezone(UTC).replace(tzinfo=None), "end": end.astimezone(UTC).replace(tzinfo=None)}
    if box:
        w, s_, e, n = box
        where.append("latitude BETWEEN :s AND :n")
        where.append("(longitude BETWEEN :w AND :e)" if w <= e else "(longitude >= :w OR longitude <= :e)")
        args.update(w=w, s=s_, e=e, n=n)
    if not include_static:
        where.append("static_kind IS NULL")
    sql = text(f"SELECT * FROM {sensor}_detections WHERE {' AND '.join(where)} ORDER BY acquired_at LIMIT {EXPORT_MAX_ROWS}")
    factory = request.app.state.session_factory

    def rows():
        with factory() as session:  # own session: the response outlives the request's dependencies
            result = session.execute(sql, args)
            buf = io.StringIO()
            writer = csv.writer(buf)
            writer.writerow(result.keys())
            for i, row in enumerate(result):
                writer.writerow(row)
                if i % 5000 == 4999:
                    yield buf.getvalue()
                    buf.seek(0)
                    buf.truncate()
            yield buf.getvalue()

    name = f"firelens_{sensor}_{start:%Y%m%d}_{end:%Y%m%d}.csv"
    return StreamingResponse(rows(), media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="{name}"'})


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
def create_rule(rule: AlertRuleIn, session: SessionDep, request: Request, background: BackgroundTasks):
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
    session.flush()
    # Fires already burning in the zone alert right away; otherwise a new rule would stay silent
    # until those fires next change (rules are evaluated on changed events during ingestion).
    if None not in bbox:
        zone = queries.Area(bbox=(rule.west, rule.south, rule.east, rule.north))
    elif None not in circle:
        zone = queries.Area(lat=rule.center_lat, lon=rule.center_lon, radius_km=rule.radius_km)
    else:
        zone = None
    stmt = select(EventRow).where(EventRow.status == "active")
    if zone:
        stmt = zone.apply(stmt, EventRow.latitude, EventRow.longitude)
    created = evaluate_rules(session, session.scalars(stmt).all(), rules=[row])
    session.commit()
    hooks = [(Alert.model_validate(a), row.webhook_url) for a, _ in created if row.webhook_url]
    if hooks:
        background.add_task(deliver_webhooks, request.app.state.http, hooks)
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
    land = None
    ndvi_client: CopernicusNdvi | None = request.app.state.ndvi
    if ndvi_client:
        try:
            land = await ndvi_client.mean_ndvi(lat, lon)
            ndvi = land.ndvi_mean
        except httpx.HTTPError:
            land = None  # fuel term is optional; degrade gracefully
    return RiskForecast(
        latitude=lat,
        longitude=lon,
        nearby_detections_7d=last,
        nearby_detections_prev_7d=prev,
        activity_trend=activity_trend(last, prev),
        ndvi=ndvi,
        surface=land.surface if land else None,
        water_fraction=land.water_fraction if land else None,
        days=compute_risk(daily, now.date(), last, ndvi, land.surface if land else None),
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


@router.get("/history/coverage")
def history_coverage(
    session: SessionDep,
    settings: SettingsDep,
    start: date,
    end: date,
    bbox: Annotated[str | None, Query(description="west,south,east,north; omit for the whole world")] = None,
):
    """Per day: is the requested area loaded? `loaded` (>=80% of it), `partial` or `missing`."""
    if start > end or (end - start).days >= MAX_BACKFILL_DAYS:
        raise HTTPException(422, f"range must be 1-{MAX_BACKFILL_DAYS} days and start <= end")
    try:
        view = queries.parse_bbox(bbox)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    days = day_status(session, start, end, view)
    if settings.is_demo:  # synthetic data covers everything it covers; nothing to load
        days = [{**d, "status": "loaded"} for d in days]
    return {
        "days": days,
        "missing": [d["date"] for d in days if d["status"] != "loaded"],
        "can_load": bool(settings.firms_map_key) and not settings.is_demo,
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
