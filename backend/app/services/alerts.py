"""Alert rules evaluated against fire events after every ingestion."""

import logging
from collections.abc import Sequence
from datetime import UTC, datetime

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ingest.clustering import haversine_km
from app.models import AlertRow, AlertRuleRow, EventRow
from app.schemas import CONFIDENCE_ORDER, Alert

log = logging.getLogger(__name__)

# Minimum event max-confidence (pct) required by each rule level, on the harmonized scale.
CONFIDENCE_FLOOR = {"low": 0, "nominal": 30, "high": 80}


def severity(ev: EventRow) -> str:
    if ev.area_km2 >= 10 or ev.max_frp_mw >= 500:
        return "critical"
    if ev.area_km2 >= 1 or ev.max_frp_mw >= 100:
        return "warning"
    return "info"


def rule_matches(rule: AlertRuleRow, ev: EventRow) -> bool:
    if not rule.enabled or ev.status != "active":
        return False
    if None not in (rule.west, rule.south, rule.east, rule.north):
        if not rule.south <= ev.latitude <= rule.north:
            return False
        inside_lon = (
            rule.west <= ev.longitude <= rule.east
            if rule.west <= rule.east
            else ev.longitude >= rule.west or ev.longitude <= rule.east
        )
        if not inside_lon:
            return False
    if None not in (rule.center_lat, rule.center_lon, rule.radius_km):
        if haversine_km(rule.center_lat, rule.center_lon, ev.latitude, ev.longitude) > rule.radius_km:
            return False
    return (
        ev.max_confidence_pct >= CONFIDENCE_FLOOR[rule.min_confidence]
        and ev.max_frp_mw >= rule.min_frp_mw
        and ev.area_km2 >= rule.min_area_km2
        and ev.detection_count >= rule.min_detections
    )


def evaluate_rules(
    session: Session, events: Sequence[EventRow], rules: Sequence[AlertRuleRow] | None = None
) -> list[tuple[AlertRow, AlertRuleRow]]:
    """Create alerts for (rule, event) pairs that match and haven't alerted before.
    `rules` defaults to every enabled rule; events may be ORM rows or freshly built drafts."""
    if rules is None:
        rules = session.scalars(select(AlertRuleRow).where(AlertRuleRow.enabled.is_(True))).all()
    if not rules or not events:
        return []
    existing = {(r.rule_id, r.event_id) for r in session.execute(select(AlertRow.rule_id, AlertRow.event_id))}
    # An alert raised for an event id that was later merged or renamed counts for its successor:
    # the same fire must not alert twice just because its id changed.
    from app.services.queries import resolve_event_id  # local import: queries imports models only

    current = {}
    for rule_id, event_id in list(existing):
        if event_id not in current:
            current[event_id] = resolve_event_id(session, event_id)
        if current[event_id] and current[event_id] != event_id:
            existing.add((rule_id, current[event_id]))
    now = datetime.now(UTC).replace(tzinfo=None)
    created = []
    for ev in events:
        for rule in rules:
            if (rule.id, ev.id) in existing or not rule_matches(rule, ev):
                continue
            alert = AlertRow(
                rule_id=rule.id,
                rule_name=rule.name,
                event_id=ev.id,
                severity=severity(ev),
                message=(
                    f"{rule.name}: active fire near {ev.latitude:.3f}, {ev.longitude:.3f} - "
                    f"{ev.area_km2:.2f} km2, max FRP {ev.max_frp_mw:.0f} MW, {ev.detection_count} detections"
                ),
                latitude=ev.latitude,
                longitude=ev.longitude,
                area_km2=ev.area_km2,
                max_frp_mw=ev.max_frp_mw,
                triggered_at=now,
            )
            session.add(alert)
            existing.add((rule.id, ev.id))
            created.append((alert, rule))
    session.flush()
    return created


async def deliver_webhooks(http: httpx.AsyncClient, alerts: Sequence[tuple[Alert, str]]) -> None:
    """Best-effort POST of each alert to its rule's webhook. Failures are logged, never raised."""
    for alert, url in alerts:
        try:
            resp = await http.post(url, json=alert.model_dump(mode="json"), timeout=5)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            log.warning("webhook delivery failed for alert %s: %s", alert.id, exc)


__all__ = ["CONFIDENCE_ORDER", "evaluate_rules", "deliver_webhooks", "rule_matches", "severity"]
