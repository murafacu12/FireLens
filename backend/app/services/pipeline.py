"""Ingestion orchestration: fetch -> normalize -> store -> rebuild events -> alerts."""

import json
import logging
from datetime import UTC, date, datetime

import httpx
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.ingest.demo import SOURCE_PLATFORMS, generate_demo_csv
from app.ingest.firms_client import FirmsClient, FirmsError
from app.ingest.normalize import NormalizationError, normalize_csv
from app.ingest.sources import get_source
from app.models import IngestRunRow
from app.schemas import Alert, FireDetection, IngestReport, IngestRequest, SourceReport
from app.services.alerts import deliver_webhooks, evaluate_rules
from app.services.store import rebuild_events, refresh_event_status, upsert_detections

log = logging.getLogger(__name__)


def firms_client(settings: Settings, http: httpx.AsyncClient) -> FirmsClient:
    return FirmsClient(http, settings.firms_map_key, settings.firms_base_url, settings.firms_max_day_range)


def persist(
    factory: sessionmaker[Session], settings: Settings, detections: list[FireDetection], started: datetime, report: IngestReport
) -> list[tuple[Alert, str]]:
    """Store detections, rebuild the events they touch, evaluate alert rules. Returns webhooks to send."""
    with factory() as session:
        window = upsert_detections(session, detections)
        alerts_out: list[tuple[Alert, str]] = []
        if window:
            events = rebuild_events(
                session,
                window,
                settings.cluster_tolerance_km,
                settings.cluster_max_gap_hours,
                settings.event_active_hours,
            )
            report.events_rebuilt += len(events)
            created = evaluate_rules(session, events)
            report.alerts_triggered += len(created)
            alerts_out = [(Alert.model_validate(a), r.webhook_url) for a, r in created if r.webhook_url]
        refresh_event_status(session, settings.event_active_hours)
        report.stored += len(detections)
        session.add(
            IngestRunRow(
                started_at=started.replace(tzinfo=None),
                finished_at=datetime.now(UTC).replace(tzinfo=None),
                demo=report.demo,
                summary=report.model_dump_json(),
            )
        )
        session.commit()
        return alerts_out


async def _fetch_texts(source_id: str, settings: Settings, client: FirmsClient, req: IngestRequest, started: datetime) -> list[str]:
    source = get_source(source_id)
    if settings.is_demo:
        if source.id not in SOURCE_PLATFORMS:
            raise ValueError(f"{source.id} has no demo generator")
        return [generate_demo_csv(source.id, started, days=req.days or settings.demo_days)]
    # Explicit area/date/day requests need the API (and its MAP_KEY). Routine refreshes always use
    # the public regional files: free, keyless, and they don't spend the key's transaction budget.
    if req.area or req.date or req.days:
        if not client.has_key:
            raise FirmsError("area/date/days requests need a FIRMS MAP_KEY; use region/window for the public files")
        start = date.fromisoformat(req.date) if req.date else None
        return await client.fetch_range(source.id, req.area or settings.ingest_area, req.days or settings.ingest_days, start)
    return [await client.fetch_public_feed(source.id, req.region or settings.public_region, req.window or settings.public_window)]


async def run_ingest(
    factory: sessionmaker[Session], settings: Settings, http: httpx.AsyncClient, req: IngestRequest | None = None
) -> IngestReport:
    req = req or IngestRequest()
    started = datetime.now(UTC)
    client = firms_client(settings, http)
    report = IngestReport(demo=settings.is_demo, source_mode=settings.data_source, sources=[], stored=0, events_rebuilt=0, alerts_triggered=0)
    detections: list[FireDetection] = []

    for source_id in req.sources or settings.ingest_sources:
        try:
            texts = await _fetch_texts(source_id, settings, client, req, started)
            rows = accepted = rejected = 0
            for text in texts:
                result = normalize_csv(text, get_source(source_id))
                rows += result.rows
                accepted += len(result.detections)
                rejected += len(result.rejected)
                detections.extend(result.detections)
            report.sources.append(SourceReport(source=source_id, rows=rows, accepted=accepted, rejected=rejected))
        except (FirmsError, NormalizationError, ValueError) as exc:
            log.warning("ingest of %s failed: %s", source_id, exc)
            report.sources.append(SourceReport(source=source_id, rows=0, accepted=0, rejected=0, error=str(exc)))

    webhooks = await run_in_threadpool(persist, factory, settings, detections, started, report)
    if webhooks:
        await deliver_webhooks(http, webhooks)
    log.info("ingest finished: %s", json.loads(report.model_dump_json()))
    return report
