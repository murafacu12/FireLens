import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from sqlalchemy import func, select

from app import models  # noqa: F401  (registers tables)
from app.api import router
from app.config import Settings, get_settings
from app.db import Base, make_engine, make_session_factory
from app.schemas import IngestRequest
from app.services.ndvi import CopernicusNdvi
from app.services.pipeline import run_ingest

log = logging.getLogger("firelens")


async def _seed(app: FastAPI) -> None:
    """First start with an empty database: load initial data in the background."""
    settings: Settings = app.state.settings
    app.state.seeding = True
    try:
        req = None if settings.is_demo else IngestRequest(window=settings.seed_window)
        log.info("empty database: seeding (%s)", settings.data_source)
        await run_ingest(app.state.session_factory, settings, app.state.http, req)
    except Exception:
        log.exception("initial seed failed")
    finally:
        app.state.seeding = False


async def _scheduler(app: FastAPI) -> None:
    settings: Settings = app.state.settings
    while True:
        await asyncio.sleep(settings.ingest_interval_minutes * 60)
        try:
            await run_ingest(app.state.session_factory, settings, app.state.http)
        except Exception:  # keep the loop alive; the next tick retries
            log.exception("scheduled ingest failed")


def create_app(settings: Settings | None = None, http_transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    """`http_transport` lets tests replace the network (e.g. httpx.MockTransport for FIRMS)."""
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        engine = make_engine(settings.database_url_effective)
        Base.metadata.create_all(engine)  # TODO: Alembic migrations once the schema settles
        app.state.settings = settings
        app.state.session_factory = make_session_factory(engine)
        app.state.http = httpx.AsyncClient(headers={"User-Agent": "FireLens/0.2"}, transport=http_transport)
        app.state.jobs = {}
        app.state.seeding = False
        app.state.ndvi = (
            CopernicusNdvi(app.state.http, settings.copernicus_client_id, settings.copernicus_client_secret)
            if settings.copernicus_enabled
            else None
        )
        log.info("data source: %s, database: %s", settings.data_source, settings.database_url_effective)
        tasks: list[asyncio.Task] = []
        with app.state.session_factory() as s:
            empty = not s.scalar(select(func.count()).select_from(models.DetectionRow))
        if empty:
            if settings.is_demo:
                await _seed(app)  # synthetic data is local and fast: keep startup deterministic
            else:
                tasks.append(asyncio.create_task(_seed(app)))  # real data: don't block startup on the network
        if settings.ingest_interval_minutes > 0 and not settings.is_demo:
            tasks.append(asyncio.create_task(_scheduler(app)))
        yield
        for task in [*tasks, *(j.task for j in app.state.jobs.values() if j.task)]:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await app.state.http.aclose()
        engine.dispose()

    app = FastAPI(title="FireLens API", version="0.2.0", lifespan=lifespan)
    app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=5)
    app.add_middleware(
        CORSMiddleware, allow_origins=settings.cors_origins, allow_methods=["*"], allow_headers=["*"]
    )
    app.include_router(router)
    return app


logging.basicConfig(level=logging.INFO)
app = create_app()
