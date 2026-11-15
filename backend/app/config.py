from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration. Every field can be set with a FIRELENS_* env var or a .env file.

    List values are given as JSON, e.g. FIRELENS_INGEST_SOURCES='["MODIS_NRT","VIIRS_NOAA20_NRT"]'.
    """

    model_config = SettingsConfigDict(
        env_prefix="FIRELENS_", env_file=(".env", "../.env"), extra="ignore"
    )

    # NASA FIRMS. Without a MAP_KEY FireLens still runs on REAL data from the public
    # active-fire files (last 7 days). The key unlocks history (any date, SP archive).
    # https://firms.modaps.eosdis.nasa.gov/api/map_key/
    firms_map_key: str | None = None
    firms_base_url: str = "https://firms.modaps.eosdis.nasa.gov"
    firms_max_day_range: int = 5
    firms_request_pause_s: float = 1.0  # be polite with the transaction budget during backfills

    # Synthetic data for offline development / UI work. Opt-in.
    demo_mode: bool = False
    # A database that already holds detections is never upgraded (new tables/columns, event or map
    # rebuilds) unless this is set: back it up first (see docs/MIGRATION_V3.md). Empty databases
    # (first start, tests) are always set up automatically.
    allow_schema_upgrade: bool = False
    demo_days: int = 30

    # Default: separate files so demo and real data never mix (see database_url_effective).
    database_url: str | None = None
    cors_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]

    # Near-real-time ingestion
    ingest_interval_minutes: int = 60  # 0 disables the scheduler; FIRMS NRT refreshes every few hours
    ingest_sources: list[str] = [
        "MODIS_NRT",
        "VIIRS_SNPP_NRT",
        "VIIRS_NOAA20_NRT",
        "VIIRS_NOAA21_NRT",
    ]
    ingest_area: str = "world"  # explicit API requests (with key): 'world' or 'west,south,east,north'
    ingest_days: int = 1
    public_region: str = "Global"  # seed + scheduled refresh: FIRMS public-file region (no key needed)
    public_window: Literal["24h", "48h", "7d"] = "24h"  # what each scheduled refresh pulls
    seed_window: Literal["24h", "48h", "7d"] = "7d"  # first start with an empty database

    # Event clustering
    cluster_tolerance_km: float = 1.0
    cluster_max_gap_hours: float = 24.0
    event_active_hours: float = 24.0
    # E2 continuity edge: detections up to this far apart in time link only where their pixels
    # overlap (bridges missed passes / cloud on the same ground). 0 disables it.
    cluster_continuity_gap_hours: float = 72.0
    cluster_continuity_tolerance_km: float = 0.0
    event_archive_days: float = 30.0  # no detection for this long: lifecycle_state "archived"
    # E2 continuity edge: detections up to this far apart in time link only where their pixels
    # overlap (bridges missed passes / cloud on the same ground). 0 disables it.
    cluster_continuity_gap_hours: float = 72.0
    cluster_continuity_tolerance_km: float = 0.0
    event_archive_days: float = 30.0  # no detection for this long: lifecycle_state "archived"

    # Seasonal baseline (per-cell daily counts from the FIRMS archive; see app/services/baseline.py).
    # Default: data/baseline.db at the repository root. Opened read-only by the API.
    baseline_db_path: str | None = None

    # Third-party providers
    open_meteo_url: str = "https://api.open-meteo.com/v1/forecast"
    copernicus_client_id: str | None = None
    copernicus_client_secret: str | None = None
    # Meteomatics Weather API (https://www.meteomatics.com): historical and forecast weather for the
    # fire-spread work. Set in .env only; never logged.
    meteomatics_username: str | None = None
    meteomatics_password: str | None = None

    @property
    def is_demo(self) -> bool:
        return self.demo_mode

    @property
    def data_source(self) -> str:
        if self.demo_mode:
            return "demo"
        return "firms_api" if self.firms_map_key else "firms_public"

    @property
    def database_url_effective(self) -> str:
        if self.database_url:
            return self.database_url
        return "sqlite:///./firelens-demo.db" if self.demo_mode else "sqlite:///./firelens.db"

    @property
    def meteomatics_enabled(self) -> bool:
        return bool(self.meteomatics_username and self.meteomatics_password)

    @property
    def copernicus_enabled(self) -> bool:
        return bool(self.copernicus_client_id and self.copernicus_client_secret)


@lru_cache
def get_settings() -> Settings:
    return Settings()
