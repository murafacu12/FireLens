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


SOURCES: dict[str, FirmsSource] = {
    s.id: s
    for s in [
        FirmsSource("MODIS_NRT", "MODIS", None, "NRT", 1000, "MODIS Terra+Aqua, near real-time (C6.1)"),
        FirmsSource("MODIS_SP", "MODIS", None, "SP", 1000, "MODIS Terra+Aqua, standard processing archive"),
        FirmsSource("VIIRS_SNPP_NRT", "VIIRS", "Suomi NPP", "NRT", 375, "VIIRS S-NPP 375 m, near real-time"),
        FirmsSource("VIIRS_SNPP_SP", "VIIRS", "Suomi NPP", "SP", 375, "VIIRS S-NPP 375 m, standard processing"),
        FirmsSource("VIIRS_NOAA20_NRT", "VIIRS", "NOAA-20", "NRT", 375, "VIIRS NOAA-20 375 m, near real-time"),
        FirmsSource("VIIRS_NOAA20_SP", "VIIRS", "NOAA-20", "SP", 375, "VIIRS NOAA-20 375 m, standard processing"),
        FirmsSource("VIIRS_NOAA21_NRT", "VIIRS", "NOAA-21", "NRT", 375, "VIIRS NOAA-21 375 m, near real-time"),
    ]
}


def get_source(source_id: str) -> FirmsSource:
    try:
        return SOURCES[source_id.upper()]
    except KeyError as exc:
        raise ValueError(f"Unknown FIRMS source {source_id!r}; known: {', '.join(SOURCES)}") from exc
