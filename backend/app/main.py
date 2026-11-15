import asyncio
import contextlib
import logging
from collections import OrderedDict
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select

from fastapi.concurrency import run_in_threadpool

from app import migrations, models  # noqa: F401  (registers tables)
from app.api import router
from app.config import Settings, get_settings
from app.db import Base, make_engine, make_session_factory
from app.schemas import IngestRequest
from app.services.ndvi import CopernicusNdvi
from app.services.sentinel import CdseStats
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
        # Never upgrade a database holding data unless explicitly allowed (FIRELENS_ALLOW_SCHEMA_UPGRADE).
        # Checked before create_all, which would otherwise add the new tables.
        pending = migrations.guard(engine, settings.allow_schema_upgrade)
        if pending:
            log.warning("upgrading the database (explicitly allowed): %s", "; ".join(pending))
        Base.metadata.create_all(engine)  # creates missing tables; columns are handled by migrations
        app.state.settings = settings
        app.state.session_factory = make_session_factory(engine)
        # Add new columns in place; rebuild fire events if the logic that builds them changed.
        await run_in_threadpool(migrations.upgrade, engine, app.state.session_factory, settings)
        app.state.http = httpx.AsyncClient(headers={"User-Agent": "FireLens/0.2"}, transport=http_transport)
        app.state.jobs = {}
        app.state.tile_cache = OrderedDict()  # built map tiles, keyed by data version
        app.state.seeding = False
        app.state.cdse = (
            CdseStats(app.state.http, settings.copernicus_client_id, settings.copernicus_client_secret)
            if settings.copernicus_enabled
            else None
        )
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
    if settings.web_dir and Path(settings.web_dir, "index.html").is_file():
        # the website, from the same origin as the API (desktop app); /api routes come first
        app.mount("/", StaticFiles(directory=settings.web_dir, html=True), name="web")
    return app


logging.basicConfig(level=logging.INFO)
# httpx logs every request URL at INFO; the FIRMS area/archive API carries the MAP_KEY in the URL.
logging.getLogger("httpx").setLevel(logging.WARNING)
app = create_app()
