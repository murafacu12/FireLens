"""Registry of the FIRMS products FireLens understands."""

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class FirmsSource:
    id: str
    instrument: Literal["MODIS", "VIIRS"]
    satellite: str | None  # None when the product mixes platforms (MODIS = Terra + Aqua)
    processing_level: Literal["NRT", "SP"]
    nominal_resolution_m: int
    description: str
    # Keyless public files: https://firms.modaps.eosdis.nasa.gov/data/active_fire/{dir}/csv/{prefix}_{Region}_{window}.csv
    public_feed: tuple[str, str] | None = None


SOURCES: dict[str, FirmsSource] = {
    s.id: s
    for s in [
        FirmsSource("MODIS_NRT", "MODIS", None, "NRT", 1000, "MODIS Terra+Aqua, near real-time (C6.1)",
                    ("modis-c6.1", "MODIS_C6_1")),
        FirmsSource("MODIS_SP", "MODIS", None, "SP", 1000, "MODIS Terra+Aqua, standard processing archive"),
        FirmsSource("VIIRS_SNPP_NRT", "VIIRS", "Suomi NPP", "NRT", 375, "VIIRS S-NPP 375 m, near real-time",
                    ("suomi-npp-viirs-c2", "SUOMI_VIIRS_C2")),
        FirmsSource("VIIRS_SNPP_SP", "VIIRS", "Suomi NPP", "SP", 375, "VIIRS S-NPP 375 m, standard processing"),
        FirmsSource("VIIRS_NOAA20_NRT", "VIIRS", "NOAA-20", "NRT", 375, "VIIRS NOAA-20 375 m, near real-time",
                    ("noaa-20-viirs-c2", "J1_VIIRS_C2")),
        FirmsSource("VIIRS_NOAA20_SP", "VIIRS", "NOAA-20", "SP", 375, "VIIRS NOAA-20 375 m, standard processing"),
        FirmsSource("VIIRS_NOAA21_NRT", "VIIRS", "NOAA-21", "NRT", 375, "VIIRS NOAA-21 375 m, near real-time",
                    ("noaa-21-viirs-c2", "J2_VIIRS_C2")),
        FirmsSource("VIIRS_NOAA21_SP", "VIIRS", "NOAA-21", "SP", 375, "VIIRS NOAA-21 375 m, standard processing"),
    ]
}

# For history: per platform, the science-quality archive (SP) and the near-real-time product.
# SP is preferred wherever it exists; NRT fills the most recent months SP hasn't reached yet.
PLATFORM_PRODUCTS: dict[str, tuple[str | None, str]] = {
    "MODIS": ("MODIS_SP", "MODIS_NRT"),
    "SNPP": ("VIIRS_SNPP_SP", "VIIRS_SNPP_NRT"),
    "NOAA20": ("VIIRS_NOAA20_SP", "VIIRS_NOAA20_NRT"),
    "NOAA21": ("VIIRS_NOAA21_SP", "VIIRS_NOAA21_NRT"),
}

# Regions of the public active-fire files (exact FIRMS spelling).
PUBLIC_REGIONS = [
    "Global", "South_America", "Central_America", "USA_contiguous_and_Hawaii", "Canada", "Alaska", "Europe",
    "Northern_and_Central_Africa", "Southern_Africa", "Russia_Asia", "South_Asia", "SouthEast_Asia",
    "Australia_NewZealand",
]
PUBLIC_WINDOWS = ["24h", "48h", "7d"]
PUBLIC_WINDOW_HOURS = {"24h": 24, "48h": 48, "7d": 168}

# Approximate extent of each public-file region (west, south, east, north), used to record
# what a public refresh covers. FIRMS' exact region polygons are slightly different.
PUBLIC_REGION_BBOX: dict[str, tuple[float, float, float, float]] = {
    "Global": (-180, -90, 180, 90),
    "South_America": (-95, -60, -30, 15),
    "Central_America": (-120, 5, -57, 33),
    "USA_contiguous_and_Hawaii": (-161, 18, -66, 50),
    "Canada": (-141, 41, -52, 84),
    "Alaska": (-170, 51, -129, 72),
    "Europe": (-26, 34, 45, 72),
    "Northern_and_Central_Africa": (-26, -12, 63, 38),
    "Southern_Africa": (10, -36, 58, -5),
    "Russia_Asia": (26, 35, 180, 82),
    "South_Asia": (60, 5, 98, 38),
    "SouthEast_Asia": (88, -11, 142, 29),
    "Australia_NewZealand": (110, -48, 180, -9),
}


def get_source(source_id: str) -> FirmsSource:
    try:
        return SOURCES[source_id.upper()]
    except KeyError as exc:
        raise ValueError(f"Unknown FIRMS source {source_id!r}; known: {', '.join(SOURCES)}") from exc
