from datetime import UTC, datetime

import pytest

from app.ingest.normalize import NormalizationError, normalize_csv, normalize_row, parse_acquired_at
from app.ingest.sources import get_source

MODIS_CSV = """latitude,longitude,brightness,scan,track,acq_date,acq_time,satellite,instrument,confidence,version,bright_t31,frp,daynight
-31.3512,-64.5521,345.2,1.6,1.2,2026-11-10,145,T,MODIS,82,6.1NRT,301.4,48.7,D
-31.3600,-64.5400,312.0,1.6,1.2,2026-11-10,145,A,MODIS,25,6.1NRT,295.0,8.1,D
95.0,-64.5,312.0,1.6,1.2,2026-11-10,145,A,MODIS,25,6.1NRT,295.0,8.1,D
"""

VIIRS_CSV = """latitude,longitude,bright_ti4,scan,track,acq_date,acq_time,satellite,instrument,confidence,version,bright_ti5,frp,daynight
-31.35234,-64.55012,367.0,0.39,0.36,2026-11-10,1712,N20,VIIRS,h,2.0NRT,298.10,12.4,D
-31.35111,-64.54888,330.5,0.39,0.36,2026-11-10,1712,N,VIIRS,n,2.0NRT,290.00,3.1,N
-31.35111,-64.54888,330.5,0.39,0.36,2026-11-10,1712,1,VIIRS,x,2.0NRT,290.00,3.1,N
"""


def test_parse_time_pads_missing_leading_zeros():
    assert parse_acquired_at("2026-11-10", "5") == datetime(2026, 11, 10, 0, 5, tzinfo=UTC)
    assert parse_acquired_at("2026-11-10", "145") == datetime(2026, 11, 10, 1, 45, tzinfo=UTC)
    with pytest.raises(NormalizationError):
        parse_acquired_at("2026-11-10", "2460")


def test_modis_rows_harmonized():
    result = normalize_csv(MODIS_CSV, get_source("MODIS_NRT"))
    assert result.rows == 3
    assert len(result.detections) == 2
    assert result.rejected[0][0] == 4  # latitude out of range, reported with its CSV line
    terra, aqua = result.detections
    assert terra.instrument == "MODIS" and terra.satellite == "Terra"
    assert terra.confidence_pct == 82 and terra.confidence_level == "high"
    assert aqua.satellite == "Aqua" and aqua.confidence_level == "low"
    assert terra.brightness_k == 345.2 and terra.brightness_secondary_k == 301.4
    assert terra.pixel_area_km2 == pytest.approx(1.92)
    assert terra.processing_level == "NRT" and terra.nominal_resolution_m == 1000


def test_viirs_rows_harmonized():
    result = normalize_csv(VIIRS_CSV, get_source("VIIRS_NOAA20_NRT"))
    assert len(result.detections) == 2
    assert len(result.rejected) == 1  # confidence 'x'
    hi, nominal = result.detections
    assert hi.satellite == "NOAA-20" and hi.confidence_level == "high" and hi.confidence_pct == 90
    assert hi.brightness_saturated is True
    assert nominal.satellite == "Suomi NPP" and nominal.daynight == "N"
    assert nominal.confidence_level == "nominal"
    assert hi.acquired_at == datetime(2026, 11, 10, 17, 12, tzinfo=UTC)


def test_source_mismatch_rejected():
    row = {"latitude": "1", "longitude": "1", "bright_ti4": "300", "acq_date": "2026-01-01",
           "acq_time": "1200", "confidence": "n"}
    with pytest.raises(NormalizationError):
        normalize_row(row, get_source("MODIS_NRT"))


def test_sp_and_nrt_share_id():
    nrt = MODIS_CSV.splitlines()[:2]
    sp = [nrt[0], nrt[1].replace("6.1NRT", "6.1")]
    a = normalize_csv("\n".join(nrt), get_source("MODIS_NRT")).detections[0]
    b = normalize_csv("\n".join(sp), get_source("MODIS_SP")).detections[0]
    assert a.id == b.id
    assert (a.processing_level, b.processing_level) == ("NRT", "SP")


def test_rejects_non_firms_payload():
    with pytest.raises(NormalizationError):
        normalize_csv("Invalid MAP_KEY.", get_source("MODIS_NRT"))
