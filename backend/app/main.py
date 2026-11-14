import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import func, select

from app import models  # noqa: F401  (registers tables)
from app.api import router
from app.config import Settings, get_settings
from app.db import Base, make_engine, make_session_factory
from app.services.ndvi import CopernicusNdvi
from app.services.pipeline import run_ingest

log = logging.getLogger("firelens")


async def _scheduler(app: FastAPI) -> None:
    settings: Settings = app.state.settings
    while True:
        await asyncio.sleep(settings.ingest_interval_minutes * 60)
        try:
            await run_ingest(app.state.session_factory, settings, app.state.http)
        except Exception:  # keep the loop alive; the next tick retries
            log.exception("scheduled ingest failed")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        engine = make_engine(settings.database_url)
        Base.metadata.create_all(engine)  # TODO: Alembic migrations once the schema settles
        app.state.settings = settings
        app.state.session_factory = make_session_factory(engine)
        app.state.http = httpx.AsyncClient(headers={"User-Agent": "FireLens/0.1"})
        app.state.ndvi = (
            CopernicusNdvi(app.state.http, settings.copernicus_client_id, settings.copernicus_client_secret)
            if settings.copernicus_enabled
            else None
        )
        if settings.is_demo:
            with app.state.session_factory() as s:
                empty = not s.scalar(select(func.count()).select_from(models.DetectionRow))
            if empty:
                log.info("demo mode: seeding %s days of synthetic detections", settings.demo_days)
                await run_ingest(app.state.session_factory, settings, app.state.http)
        task = None
        if settings.ingest_interval_minutes > 0 and not settings.is_demo:
            task = asyncio.create_task(_scheduler(app))
        yield
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await app.state.http.aclose()
        engine.dispose()

    app = FastAPI(title="FireLens API", version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware, allow_origins=settings.cors_origins, allow_methods=["*"], allow_headers=["*"]
    )
    app.include_router(router)
    return app


logging.basicConfig(level=logging.INFO)
app = create_app()
