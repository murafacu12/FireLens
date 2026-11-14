"""Normalize raw FIRMS CSV rows (MODIS or VIIRS) into the canonical FireDetection model.

See docs/DATA_HARMONIZATION.md for the field mapping and the reasoning behind each rule.
"""

import csv
import hashlib
import io
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime

from app.ingest.sources import FirmsSource
from app.schemas import ConfidenceLevel, FireDetection

MODIS_SATELLITES = {"T": "Terra", "TERRA": "Terra", "A": "Aqua", "AQUA": "Aqua"}
VIIRS_SATELLITES = {
    "N": "Suomi NPP",
    "NPP": "Suomi NPP",
    "SNPP": "Suomi NPP",
    "SUOMI NPP": "Suomi NPP",
    "1": "NOAA-20",
    "N20": "NOAA-20",
    "J1": "NOAA-20",
    "NOAA-20": "NOAA-20",
    "2": "NOAA-21",
    "N21": "NOAA-21",
    "J2": "NOAA-21",
    "NOAA-21": "NOAA-21",
}

# VIIRS reports only a class. Map each class to a representative value inside the
# equivalent MODIS percentage band so both sensors can be filtered on one numeric scale.
VIIRS_CONFIDENCE: dict[str, tuple[ConfidenceLevel, int]] = {
    "l": ("low", 20),
    "low": ("low", 20),
    "n": ("nominal", 55),
    "nominal": ("nominal", 55),
    "h": ("high", 90),
    "high": ("high", 90),
}

# VIIRS I4 saturates around 367 K; values at/above are flagged, not trusted as temperatures.
VIIRS_I4_SATURATION_K = 367.0
NOMINAL_PIXEL_KM = {"MODIS": 1.0, "VIIRS": 0.375}
NOMINAL_RESOLUTION_M = {"MODIS": 1000, "VIIRS": 375}


class NormalizationError(ValueError):
    pass


@dataclass
class NormalizationResult:
    detections: list[FireDetection] = field(default_factory=list)
    rejected: list[tuple[int, str]] = field(default_factory=list)  # (csv line, reason)
    rows: int = 0


def modis_confidence_level(pct: int) -> ConfidenceLevel:
    # FIRMS FAQ: low < 30, nominal 30-79, high >= 80.
    if pct < 30:
        return "low"
    if pct < 80:
        return "nominal"
    return "high"


def _num(row: Mapping[str, str], key: str, required: bool = False) -> float | None:
    raw = row.get(key)
    if raw is None or str(raw).strip() == "":
        if required:
            raise NormalizationError(f"missing {key}")
        return None
    try:
        value = float(raw)
    except ValueError as exc:
        raise NormalizationError(f"invalid number in {key}: {raw!r}") from exc
    if not math.isfinite(value):
        raise NormalizationError(f"non-finite value in {key}")
    return value


def parse_acquired_at(date_str: str | None, time_str: str | None) -> datetime:
    """FIRMS gives acq_date (YYYY-MM-DD) and acq_time (HHMM UTC, leading zeros often dropped)."""
    if not date_str or time_str is None or str(time_str).strip() == "":
        raise NormalizationError("missing acq_date/acq_time")
    try:
        day = datetime.strptime(date_str.strip(), "%Y-%m-%d")
        hhmm = str(int(float(str(time_str).strip()))).zfill(4)
    except ValueError as exc:
        raise NormalizationError(f"invalid acquisition time {date_str!r} {time_str!r}") from exc
    hour, minute = int(hhmm[:-2]), int(hhmm[-2:])
    if hour > 23 or minute > 59:
        raise NormalizationError(f"invalid acq_time {time_str!r}")
    return day.replace(hour=hour, minute=minute, tzinfo=UTC)


def detect_instrument(row: Mapping[str, str]) -> str:
    if "bright_ti4" in row:
        return "VIIRS"
    if "brightness" in row:
        return "MODIS"
    instrument = (row.get("instrument") or "").strip().upper()
    if instrument in ("MODIS", "VIIRS"):
        return instrument
    raise NormalizationError("cannot determine instrument (no brightness/bright_ti4 column)")


def detection_id(instrument: str, satellite: str, lat: float, lon: float, when: datetime) -> str:
    # Same pixel from NRT and SP products yields the same id, so SP can supersede NRT on upsert.
    key = f"{instrument}|{satellite}|{lat:.4f}|{lon:.4f}|{when.strftime('%Y%m%d%H%M')}"
    return hashlib.sha1(key.encode()).hexdigest()[:20]


def normalize_row(row: Mapping[str, str], source: FirmsSource | None = None) -> FireDetection:
    row = {k.strip().lower(): (v.strip() if isinstance(v, str) else v) for k, v in row.items() if k}
    instrument = detect_instrument(row)
    if source and source.instrument != instrument:
        raise NormalizationError(f"row looks like {instrument} but source is {source.id}")

    lat = _num(row, "latitude", required=True)
    lon = _num(row, "longitude", required=True)
    if not -90 <= lat <= 90 or not -180 <= lon <= 180:
        raise NormalizationError(f"coordinates out of range: {lat}, {lon}")
    acquired_at = parse_acquired_at(row.get("acq_date"), row.get("acq_time"))

    sat_raw = (row.get("satellite") or "").upper()
    table = MODIS_SATELLITES if instrument == "MODIS" else VIIRS_SATELLITES
    satellite = table.get(sat_raw) or (source.satellite if source else None) or (sat_raw or "Unknown")

    conf_raw = (row.get("confidence") or "").strip()
    if not conf_raw:
        raise NormalizationError("missing confidence")
    if instrument == "MODIS":
        brightness = _num(row, "brightness")
        secondary = _num(row, "bright_t31")
        saturated = False
        try:
            conf_pct = max(0, min(100, round(float(conf_raw))))
        except ValueError as exc:
            raise NormalizationError(f"invalid MODIS confidence {conf_raw!r}") from exc
        conf_level = modis_confidence_level(conf_pct)
    else:
        brightness = _num(row, "bright_ti4")
        secondary = _num(row, "bright_ti5")
        saturated = brightness is not None and brightness >= VIIRS_I4_SATURATION_K
        mapped = VIIRS_CONFIDENCE.get(conf_raw.lower())
        if mapped is None:
            try:  # some re-distributed datasets carry numeric VIIRS confidence
                conf_pct = max(0, min(100, round(float(conf_raw))))
            except ValueError as exc:
                raise NormalizationError(f"invalid VIIRS confidence {conf_raw!r}") from exc
            conf_level = modis_confidence_level(conf_pct)
        else:
            conf_level, conf_pct = mapped

    nominal_km = NOMINAL_PIXEL_KM[instrument]
    scan = _num(row, "scan") or nominal_km
    track = _num(row, "track") or nominal_km
    frp = _num(row, "frp")
    if frp is not None and frp < 0:
        frp = None

    daynight = (row.get("daynight") or "").upper()
    det_type = _num(row, "type")
    version = row.get("version") or None
    if version:
        level = "NRT" if "NRT" in version.upper() else "SP"
    else:
        level = source.processing_level if source else "NRT"

    return FireDetection(
        id=detection_id(instrument, satellite, lat, lon, acquired_at),
        source=source.id if source else f"{instrument}_UPLOAD",
        instrument=instrument,
        satellite=satellite,
        processing_level=level,
        latitude=lat,
        longitude=lon,
        acquired_at=acquired_at,
        daynight=daynight if daynight in ("D", "N") else None,
        brightness_k=brightness,
        brightness_secondary_k=secondary,
        brightness_saturated=saturated,
        frp_mw=frp,
        scan_km=scan,
        track_km=track,
        pixel_area_km2=round(scan * track, 4),
        nominal_resolution_m=NOMINAL_RESOLUTION_M[instrument],
        confidence_raw=conf_raw,
        confidence_pct=conf_pct,
        confidence_level=conf_level,
        detection_type=int(det_type) if det_type is not None else None,
        version=version,
    )


def normalize_csv(text: str, source: FirmsSource | None = None) -> NormalizationResult:
    result = NormalizationResult()
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    header = [h.strip().lower() for h in (reader.fieldnames or [])]
    if "latitude" not in header or "longitude" not in header:
        raise NormalizationError(f"not a FIRMS CSV (header: {text[:120]!r})")
    seen: set[str] = set()
    for line, row in enumerate(reader, start=2):
        result.rows += 1
        try:
            det = normalize_row(row, source)
        except NormalizationError as exc:
            result.rejected.append((line, str(exc)))
            continue
        if det.id in seen:
            continue
        seen.add(det.id)
        result.detections.append(det)
    return result
