"""Historical backfill from the FIRMS archive (requires a MAP_KEY).

For each platform and day, the science-quality archive (SP) is used when FIRMS has it,
otherwise the near-real-time product (NRT). Consecutive days of the same product are
fetched in chunks of up to FIRMS' DAY_RANGE limit. Each chunk is normalized, stored and
clustered immediately, so a long backfill shows progress and survives interruptions.
"""

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Literal

import httpx
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.ingest.firms_client import FirmsError, FirmsRateLimited
from app.ingest.normalize import NormalizationError, normalize_csv
from app.ingest.sources import PLATFORM_PRODUCTS, get_source
from app.schemas import IngestReport
from app.services.coverage import days_between
from app.services.pipeline import firms_client, persist

log = logging.getLogger(__name__)

Platform = Literal["MODIS", "SNPP", "NOAA20", "NOAA21"]
MAX_BACKFILL_DAYS = 400

# FIRMS retired its country endpoint (it answers "Invalid API call."), so a country is loaded
# through the area API with its bounding box (west, south, east, north). A box also covers
# slivers of neighbouring countries; precise borders need a polygon filter (roadmap).
COUNTRY_BBOX: dict[str, tuple[str, str]] = {
    "ARG": ("Argentina", "-73.6,-55.1,-53.6,-21.8"),
    "BOL": ("Bolivia", "-69.7,-22.9,-57.4,-9.7"),
    "BRA": ("Brazil", "-74.0,-33.8,-34.7,5.3"),
    "CHL": ("Chile", "-75.7,-56.0,-66.4,-17.5"),
    "COL": ("Colombia", "-79.0,-4.3,-66.8,12.5"),
    "ECU": ("Ecuador", "-81.1,-5.0,-75.2,1.5"),
    "PRY": ("Paraguay", "-62.7,-27.6,-54.2,-19.3"),
    "PER": ("Peru", "-81.4,-18.4,-68.6,0.0"),
    "URY": ("Uruguay", "-58.5,-35.0,-53.1,-30.1"),
    "VEN": ("Venezuela", "-73.4,0.6,-59.8,12.2"),
    "MEX": ("Mexico", "-117.2,14.5,-86.7,32.7"),
    "USA": ("United States (contiguous)", "-124.8,24.5,-66.9,49.4"),
    "CAN": ("Canada", "-141.0,41.7,-52.6,70.0"),
    "AUS": ("Australia", "113.3,-43.7,153.6,-10.7"),
    "PRT": ("Portugal", "-9.6,36.9,-6.2,42.2"),
    "ESP": ("Spain", "-9.4,35.9,3.3,43.8"),
    "GRC": ("Greece", "19.4,34.8,28.3,41.8"),
}
RATE_LIMIT_WAIT_S = 120
RATE_LIMIT_RETRIES = 3


class BackfillRequest(BaseModel):
    start: date
    end: date
    bbox: str | None = Field(None, description="west,south,east,north")
    country: str | None = Field(None, description="ISO 3166 alpha-3, e.g. ARG (see COUNTRY_BBOX)")
    platforms: list[Platform] = ["MODIS", "SNPP", "NOAA20", "NOAA21"]

    @model_validator(mode="after")
    def _check(self):
        if self.end < self.start:
            raise ValueError("end must be on or after start")
        if (self.end - self.start).days + 1 > MAX_BACKFILL_DAYS:
            raise ValueError(f"at most {MAX_BACKFILL_DAYS} days per backfill")
        if self.end > datetime.now(UTC).date():
            raise ValueError("end cannot be in the future")
        if bool(self.bbox) == bool(self.country):
            raise ValueError("give exactly one of bbox or country")
        if self.country:
            self.country = self.country.upper()
            if self.country not in COUNTRY_BBOX:
                raise ValueError(f"unsupported country {self.country!r}; one of {', '.join(COUNTRY_BBOX)} (or use bbox)")
        if self.bbox:
            try:
                w, s, e, n = (float(x) for x in self.bbox.split(","))
            except ValueError as exc:
                raise ValueError("bbox must be 'west,south,east,north'") from exc
            if not (-180 <= w < e <= 180 and -90 <= s < n <= 90):
                raise ValueError("bbox out of range (west < east, south < north)")
        return self


@dataclass(frozen=True)
class Chunk:
    source: str
    start: date
    days: int


def plan_backfill(
    start: date,
    end: date,
    availability: dict[str, tuple[date, date]],
    platforms: list[str],
    max_days: int,
) -> tuple[list[Chunk], list[str]]:
    """Choose SP-else-NRT per platform and day; returns (chunks, notes about uncovered days)."""
    chunks: list[Chunk] = []
    notes: list[str] = []
    for platform in platforms:
        sp, nrt = PLATFORM_PRODUCTS[platform]
        days: list[tuple[date, str]] = []
        missing = 0
        d = start
        while d <= end:
            chosen = None
            for product in (sp, nrt):
                span = availability.get(product) if product else None
                if span and span[0] <= d <= span[1]:
                    chosen = product
                    break
            if chosen:
                days.append((d, chosen))
            else:
                missing += 1
            d += timedelta(days=1)
        if missing:
            notes.append(f"{platform}: {missing} day(s) not available in FIRMS")
        # group consecutive days of the same product into API-sized chunks
        i = 0
        while i < len(days):
            first, product = days[i]
            n = 1
            while (
                i + n < len(days)
                and n < max_days
                and days[i + n][1] == product
                and days[i + n][0] == first + timedelta(days=n)
            ):
                n += 1
            chunks.append(Chunk(product, first, n))
            i += n
    chunks.sort(key=lambda c: (c.start, c.source))
    return chunks, notes


@dataclass
class BackfillJob:
    request: BackfillRequest
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    status: Literal["queued", "running", "done", "failed", "cancelled"] = "queued"
    total_chunks: int = 0
    done_chunks: int = 0
    failed_chunks: int = 0
    detections: int = 0
    events_rebuilt: int = 0
    current: str | None = None
    notes: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None
    task: asyncio.Task | None = None

    def public(self) -> dict:
        return {
            "id": self.id,
            "status": self.status,
            "request": self.request.model_dump(mode="json"),
            "total_chunks": self.total_chunks,
            "done_chunks": self.done_chunks,
            "failed_chunks": self.failed_chunks,
            "progress": round(self.done_chunks / self.total_chunks, 3) if self.total_chunks else 0.0,
            "detections": self.detections,
            "events_rebuilt": self.events_rebuilt,
            "current": self.current,
            "notes": self.notes,
            "errors": self.errors[-20:],
            "created_at": self.created_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
        }


async def run_backfill(
    job: BackfillJob, factory: sessionmaker[Session], settings: Settings, http: httpx.AsyncClient
) -> None:
    client = firms_client(settings, http)
    req = job.request
    area = req.bbox or COUNTRY_BBOX[req.country][1]
    area_bbox = tuple(float(x) for x in area.split(","))
    job.status = "running"
    try:
        availability = await client.data_availability()
        chunks, notes = plan_backfill(req.start, req.end, availability, list(req.platforms), settings.firms_max_day_range)
        job.notes = notes
        job.total_chunks = len(chunks)
        for chunk in chunks:
            job.current = f"{chunk.source} {chunk.start.isoformat()} +{chunk.days}d"
            text = None
            for attempt in range(RATE_LIMIT_RETRIES + 1):
                try:
                    text = await client.fetch_area_csv(chunk.source, area, chunk.days, chunk.start)
                    break
                except FirmsRateLimited as exc:
                    if attempt == RATE_LIMIT_RETRIES:
                        raise
                    job.current = f"rate limited, waiting {RATE_LIMIT_WAIT_S}s ({exc})"
                    await asyncio.sleep(RATE_LIMIT_WAIT_S)
                except FirmsError as exc:  # one bad chunk must not sink a long backfill
                    job.errors.append(f"{chunk.source} {chunk.start}: {exc}")
                    break
            if text is None:
                job.failed_chunks += 1
            else:
                try:
                    result = normalize_csv(text, get_source(chunk.source))
                except NormalizationError as exc:
                    job.failed_chunks += 1
                    job.errors.append(f"{chunk.source} {chunk.start}: {exc}")
                else:
                    report = IngestReport(demo=False, source_mode="firms_api", sources=[], stored=0, events_rebuilt=0, alerts_triggered=0)
                    covered = (days_between(chunk.start, chunk.start + timedelta(days=chunk.days - 1)), area_bbox, chunk.source)
                    await run_in_threadpool(persist, factory, settings, result.detections, datetime.now(UTC), report, [covered])
                    job.detections += report.stored
                    job.events_rebuilt += report.events_rebuilt
            job.done_chunks += 1
            await asyncio.sleep(settings.firms_request_pause_s)
        # Partial failures still leave useful data ("done" + errors); nothing at all is a failure.
        job.status = "failed" if job.total_chunks and job.failed_chunks == job.total_chunks else "done"
    except asyncio.CancelledError:
        job.status = "cancelled"
        raise
    except FirmsError as exc:
        job.status = "failed"
        job.errors.append(str(exc))
    except Exception as exc:  # surface unexpected failures on the job instead of losing them
        log.exception("backfill %s failed", job.id)
        job.status = "failed"
        job.errors.append(f"internal error: {exc!r}")
    finally:
        job.current = None
        job.finished_at = datetime.now(UTC)
