"""Per-fire report: the ground a fire burns on, and what Sentinel-2/3/5P saw.

* `ground` (no Copernicus quota): land cover under the detections and around the fire (ESA
  WorldCover), terrain (Copernicus DEM GLO-90): elevation, slope, which way the slopes face.
* `satellites` (Copernicus processing units, cached): vegetation before the fire, burn scar
  (dNBR), daytime ground temperature, smoke (CO and aerosol index). See sentinel.py for what each
  measurement means.

Reports are cached in data/enrichment.db (never in the production database), per event version:
an event that gains detections gets a new report. Satellite reports of fires still burning are
refreshed once a day (new images keep arriving).
"""

import asyncio
import json
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
from sqlalchemy import Column, DateTime, Integer, MetaData, String, Table, Text, create_engine, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.models import DetectionRow, EventRow
from app.paths import data_dir
from app.services import geodata
from app.services.sentinel import CdseStats, SentinelError

BURNABLE = {"tree_cover", "shrubland", "grassland", "cropland", "wetland", "mangroves", "moss_lichen"}
COMPASS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]

meta = MetaData()
reports = Table(
    "fire_reports", meta,
    Column("event_id", String(32), primary_key=True),
    Column("kind", String(16), primary_key=True),
    Column("version", Integer, nullable=False),
    Column("day", String(10), nullable=False),
    Column("created_at", DateTime, nullable=False),
    Column("body", Text, nullable=False),
)


def default_path() -> Path:
    return data_dir() / "enrichment.db"


_engines: dict[str, object] = {}


def store(path: Path | None = None):
    p = str(path or default_path())
    if p not in _engines:
        Path(p).parent.mkdir(parents=True, exist_ok=True)
        e = create_engine(f"sqlite:///{Path(p).as_posix()}")
        meta.create_all(e)
        _engines[p] = e
    return _engines[p]


def _get(engine, event_id: str, kind: str, version: int, day: str | None) -> dict | None:
    with engine.connect() as c:
        row = c.execute(select(reports.c.version, reports.c.day, reports.c.body).where(
            reports.c.event_id == event_id, reports.c.kind == kind)).first()
    if row and row.version == version and (day is None or row.day == day):
        return json.loads(row.body)
    return None


def _put(engine, event_id: str, kind: str, version: int, body: dict) -> None:
    now = datetime.now(UTC).replace(tzinfo=None)
    stmt = sqlite_insert(reports).values(event_id=event_id, kind=kind, version=version, day=now.date().isoformat(),
                                         created_at=now, body=json.dumps(body, default=str))
    with engine.begin() as c:
        c.execute(stmt.on_conflict_do_update(index_elements=["event_id", "kind"],
                                             set_={k: stmt.excluded[k] for k in ("version", "day", "created_at", "body")}))


def padded_box(ev: EventRow, min_pad_km: float = 1.0, frac: float = 0.25) -> tuple[float, float, float, float]:
    """The event's box with a margin: the surroundings the fire can spread into."""
    lat = (ev.south + ev.north) / 2
    kx = 111.320 * max(math.cos(math.radians(lat)), 0.05)
    pad_y = max(min_pad_km / 110.574, (ev.north - ev.south) * frac)
    pad_x = max(min_pad_km / kx, (ev.east - ev.west) * frac)
    return (ev.west - pad_x, ev.south - pad_y, ev.east + pad_x, ev.north + pad_y)


def compass(deg: float) -> str:
    return COMPASS[int(((deg % 360) + 22.5) // 45) % 8]


# ---------------------------------------------------------------------------- ground


def ground(session: Session, ev: EventRow, budget_s: float = 15.0) -> dict:
    wc, dem = geodata.worldcover(), geodata.dem()
    box = padded_box(ev)
    rows = session.execute(select(DetectionRow.latitude, DetectionRow.longitude).where(
        DetectionRow.event_id == ev.id, DetectionRow.static_kind.is_(None))).all()
    out: dict = {"event_id": ev.id, "box": list(box)}
    # land cover under the detections, and around the fire
    if rows:
        lat, lon = (np.asarray(x, dtype=float) for x in zip(*rows))
        codes, ok = wc.sample(lat, lon, budget_s)
        out["under_detections"] = _shares(codes[ok]) if ok.any() else None
    grid, cov, _, _ = wc.window(*box, step=wc.pixel_deg, budget_s=budget_s)
    around = _shares(grid.ravel()) if cov > 0 else None
    out["around"] = around
    out["landcover_coverage"] = round(cov, 3)
    if around:
        out["burnable_share_around"] = round(sum(c["share"] for c in around if c["class"] in BURNABLE), 3)
    # terrain at 90 m
    dstep = 1 / 1200
    elev, dcov, lats, _ = dem.window(*box, step=dstep, budget_s=budget_s)
    if dcov > 0 and elev.size >= 9:
        slope, aspect = geodata.terrain(elev, lats, dstep)
        w = slope.ravel()
        a = np.radians(aspect.ravel())
        # slope-weighted circular mean of the downhill direction: where the steep ground faces
        sx, sy = float((w * np.sin(a)).sum()), float((w * np.cos(a)).sum())
        facing = (math.degrees(math.atan2(sx, sy)) + 360) % 360
        strength = math.hypot(sx, sy) / (w.sum() or 1.0)  # 0 = slopes face every way, 1 = all one way
        out["terrain"] = {
            "elevation_min_m": int(elev.min()), "elevation_mean_m": int(round(float(elev.mean()))), "elevation_max_m": int(elev.max()),
            "slope_mean_deg": round(float(slope.mean()), 1), "slope_p90_deg": round(float(np.percentile(slope, 90)), 1),
            "steep_share": round(float((slope >= 20).mean()), 3),  # fire runs much faster uphill on these
            "slopes_face": compass(facing) if strength >= 0.2 else None, "facing_strength": round(strength, 2),
            "coverage": round(dcov, 3), "source": "Copernicus DEM GLO-90",
        }
    out["source"] = "ESA WorldCover 2021 (read at ~150 m); Copernicus DEM GLO-90"
    return out


def _shares(codes: np.ndarray) -> list[dict]:
    total = len(codes)
    out = []
    for code in np.unique(codes):
        name = geodata.LANDCOVER.get(int(code), "no_data")
        n = int((codes == code).sum())
        out.append({"class": name, "label": geodata.LANDCOVER_LABEL[name], "share": round(n / total, 4)})
    return sorted(out, key=lambda c: -c["share"])


def ground_cached(session: Session, ev: EventRow, engine=None) -> dict:
    engine = engine or store()
    hit = _get(engine, ev.id, "ground", ev.version, None)
    if hit:
        return hit
    body = ground(session, ev)
    complete = body.get("landcover_coverage", 0) >= 1 and (body.get("terrain") or {}).get("coverage", 0) >= 1
    if complete:  # tiles still downloading: try again next time
        _put(engine, ev.id, "ground", ev.version, body)
    return body


# ---------------------------------------------------------------------------- satellites


def _utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


async def satellites(client: CdseStats, ev: EventRow, now: datetime | None = None) -> dict:
    now = now or datetime.now(UTC)
    start, end = _utc(ev.start_at), _utc(ev.end_at)
    box = list(padded_box(ev))
    lat, lon = (ev.south + ev.north) / 2, (ev.west + ev.east) / 2
    active = ev.lifecycle_state == "active" or now - end < timedelta(days=1)
    # burn scar: after the fire if it is over, otherwise "so far" (any clear image since it started)
    after_from = start + timedelta(days=1) if active else end + timedelta(hours=12)
    after_to = min(now, after_from + timedelta(days=40))
    during = (start, max(end, start + timedelta(days=1)))
    smoke_box = [lon - 0.5, lat - 0.5, lon + 0.5, lat + 0.5]  # ~100 km: plumes drift

    async def safe(coro):
        try:
            return await coro
        except (SentinelError, Exception) as exc:  # one failed measurement must not hide the others
            return {"available": False, "error": str(exc)[:200]}

    jobs = {
        "vegetation_before": client.vegetation(box, start - timedelta(days=30), start),
        "surface_temperature": client.surface_temperature(box, during),
        "smoke": client.smoke(smoke_box, during),
    }
    if after_to > after_from:
        jobs["burn_scar"] = client.burn_scar(box, start, after_from, after_to)
    results = dict(zip(jobs, await asyncio.gather(*(safe(c) for c in jobs.values()))))
    if "burn_scar" not in results:
        results["burn_scar"] = {"available": False, "reason": "too_recent"}
    results["burn_scar"]["so_far"] = active
    results.update({"event_id": ev.id, "active": active, "box": box, "smoke_box": smoke_box,
                    "detected_footprint_km2": ev.area_km2, "computed_at": now.isoformat()})
    return results


async def satellites_cached(client: CdseStats, ev: EventRow, engine=None) -> dict:
    engine = engine or store()
    now = datetime.now(UTC)
    burning = ev.lifecycle_state == "active" or now - _utc(ev.end_at) < timedelta(days=1)
    # a burning fire gets new images every day; a finished one only until its burn scar is measured
    hit = _get(engine, ev.id, "satellites", ev.version, now.date().isoformat() if burning else None)
    if hit and not burning and not (hit.get("burn_scar") or {}).get("available"):
        hit = None if (now - _utc(ev.end_at)) < timedelta(days=45) else hit
    if hit:
        return hit
    body = await satellites(client, ev)
    if not any(isinstance(v, dict) and v.get("error") for v in body.values()):
        _put(engine, ev.id, "satellites", ev.version, body)
    return body
