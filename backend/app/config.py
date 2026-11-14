from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration. Every field can be set with a FIRELENS_* env var or a .env file.

    List values are given as JSON, e.g. FIRELENS_INGEST_SOURCES='["MODIS_NRT","VIIRS_SNPP_NRT"]'.
    """

    model_config = SettingsConfigDict(
        env_prefix="FIRELENS_", env_file=(".env", "../.env"), extra="ignore"
    )

    # NASA FIRMS (https://firms.modaps.eosdis.nasa.gov/api/map_key/)
    firms_map_key: str | None = None
    firms_base_url: str = "https://firms.modaps.eosdis.nasa.gov"
    firms_max_day_range: int = 5

    # Demo mode generates synthetic FIRMS-format CSVs so the full pipeline runs without a MAP_KEY.
    # None = automatic (demo when no MAP_KEY is configured).
    demo_mode: bool | None = None
    demo_days: int = 30

    database_url: str = "sqlite:///./firelens.db"
    cors_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]

    # Periodic NRT ingestion (0 disables the scheduler).
    ingest_interval_minutes: int = 0
    ingest_sources: list[str] = [
        "MODIS_NRT",
        "VIIRS_SNPP_NRT",
        "VIIRS_NOAA20_NRT",
        "VIIRS_NOAA21_NRT",
    ]
    ingest_area: str = "world"
    ingest_days: int = 1

    # Event clustering
    cluster_tolerance_km: float = 1.0
    cluster_max_gap_hours: float = 24.0
    event_active_hours: float = 24.0

    # Third-party providers
    open_meteo_url: str = "https://api.open-meteo.com/v1/forecast"
    copernicus_client_id: str | None = None
    copernicus_client_secret: str | None = None

    @property
    def is_demo(self) -> bool:
        if self.demo_mode is not None:
            return self.demo_mode
        return not self.firms_map_key

    @property
    def copernicus_enabled(self) -> bool:
        return bool(self.copernicus_client_id and self.copernicus_client_secret)


@lru_cache
def get_settings() -> Settings:
    return Settings()
