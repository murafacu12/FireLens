"""Sentinel-2 L2A NDVI from the Copernicus Data Space Ecosystem (Sentinel Hub Statistical API).

Requires an OAuth client registered at https://shapps.dataspace.copernicus.eu/dashboard
(FIRELENS_COPERNICUS_CLIENT_ID / FIRELENS_COPERNICUS_CLIENT_SECRET).
"""

import math
import time
from datetime import UTC, datetime, timedelta

import httpx
from pydantic import BaseModel

TOKEN_URL = "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token"
STATS_URL = "https://sh.dataspace.copernicus.eu/api/v1/statistics"
CACHE_TTL_S = 24 * 3600

# One request answers two questions about the 5 km box around the point:
#   ndvi  - vegetation greenness of the LAND pixels (water returns NaN, which the statistics skip)
#   water - share of cloud-free pixels that are water (Scene Classification class 6)
# Excluded entirely (dataMask 0): 0 no data, 1 saturated/defective, 3 cloud shadow, 8-9 cloud,
# 10 cirrus, 11 snow. Water used to be averaged in; its negative NDVI dragged riverside and
# wetland values far below the vegetation's.
EVALSCRIPT = """//VERSION=3
function setup() {
  return {
    input: [{ bands: ["B04", "B08", "SCL", "dataMask"] }],
    output: [
      { id: "ndvi", bands: 1, sampleType: "FLOAT32" },
      { id: "water", bands: 1, sampleType: "FLOAT32" },
      { id: "dataMask", bands: 1 }
    ]
  };
}
function evaluatePixel(s) {
  var invalid = [0, 1, 3, 8, 9, 10, 11].indexOf(s.SCL) !== -1;
  var water = s.SCL === 6;
  var ndvi = (s.B08 - s.B04) / (s.B08 + s.B04 + 1e-6);
  return { ndvi: [water ? NaN : ndvi], water: [water ? 1 : 0], dataMask: [s.dataMask && !invalid ? 1 : 0] };
}"""

WATER_SURFACE = 0.9  # at least this share of the box is water -> nothing to burn


def _num(v) -> float | None:
    """Statistics may come back as a number, None or the string "NaN"."""
    return float(v) if isinstance(v, (int, float)) and math.isfinite(v) else None


class NdviResult(BaseModel):
    latitude: float
    longitude: float
    ndvi_mean: float | None  # land pixels only
    valid_fraction: float | None  # cloud-free share of the box
    water_fraction: float | None = None  # share of cloud-free pixels that are water
    # land | water (lake, river, coastal sea) | open_ocean (outside Sentinel-2's land/coast
    # acquisitions: 30 days without a single image) | unknown (only clouds in 30 days)
    surface: str = "unknown"
    interval_from: str | None
    interval_to: str | None
    source: str = "Copernicus Sentinel-2 L2A (CDSE Statistical API)"


class CopernicusNdvi:
    def __init__(self, http: httpx.AsyncClient, client_id: str, client_secret: str):
        self._http = http
        self._id = client_id
        self._secret = client_secret
        self._token: str | None = None
        self._token_exp = 0.0
        # NDVI changes over days, not minutes; each request spends Copernicus processing units.
        self._cache: dict[tuple[float, float], tuple[float, NdviResult]] = {}

    async def _access_token(self) -> str:
        if self._token and time.time() < self._token_exp - 60:
            return self._token
        resp = await self._http.post(
            TOKEN_URL,
            data={"grant_type": "client_credentials", "client_id": self._id, "client_secret": self._secret},
            timeout=20,
        )
        resp.raise_for_status()
        body = resp.json()
        self._token = body["access_token"]
        self._token_exp = time.time() + float(body.get("expires_in", 600))
        return self._token

    async def mean_ndvi(self, lat: float, lon: float, half_size_km: float = 2.5, days: int = 30) -> NdviResult:
        key = (round(lat, 2), round(lon, 2))  # ~1 km cells share a result
        hit = self._cache.get(key)
        if hit and time.time() - hit[0] < CACHE_TTL_S:
            return hit[1]
        result = await self._query(lat, lon, half_size_km, days)
        if result.surface != "unknown":  # don't pin a cloudy miss for a whole day
            if len(self._cache) > 5000:
                self._cache.clear()
            self._cache[key] = (time.time(), result)
        return result

    async def _query(self, lat: float, lon: float, half_size_km: float, days: int) -> NdviResult:
        dlat = half_size_km / 110.574
        dlon = half_size_km / (111.320 * max(math.cos(math.radians(lat)), 1e-3))
        end = datetime.now(UTC)
        body = {
            "input": {
                "bounds": {
                    "bbox": [lon - dlon, lat - dlat, lon + dlon, lat + dlat],
                    "properties": {"crs": "http://www.opengis.net/def/crs/EPSG/0/4326"},
                },
                "data": [{"type": "sentinel-2-l2a", "dataFilter": {"maxCloudCoverage": 60}}],
            },
            "aggregation": {
                "timeRange": {
                    "from": (end - timedelta(days=days)).strftime("%Y-%m-%dT00:00:00Z"),
                    "to": end.strftime("%Y-%m-%dT23:59:59Z"),
                },
                "aggregationInterval": {"of": "P5D"},
                "evalscript": EVALSCRIPT,
                "resx": 0.0005,
                "resy": 0.0005,
            },
        }
        token = await self._access_token()
        resp = await self._http.post(STATS_URL, json=body, headers={"Authorization": f"Bearer {token}"}, timeout=60)
        resp.raise_for_status()
        data = resp.json().get("data", [])
        empty = dict(latitude=lat, longitude=lon, ndvi_mean=None, valid_fraction=None, interval_from=None, interval_to=None)
        if not data:
            # Sentinel-2 revisits every land/coastal point every ~5 days: a month with no
            # acquisition at all means the point is out at sea.
            return NdviResult(**empty, water_fraction=1.0, surface="open_ocean")
        # Most recent interval with cloud-free pixels wins.
        for interval in sorted(data, key=lambda d: d["interval"]["from"], reverse=True):
            outputs = interval["outputs"]
            water_stats = outputs["water"]["bands"]["B0"]["stats"]
            samples = water_stats.get("sampleCount") or 0
            valid = samples - (water_stats.get("noDataCount") or 0)
            if valid <= 0:
                continue  # fully clouded interval
            water = _num(water_stats.get("mean")) or 0.0
            ndvi = _num(outputs["ndvi"]["bands"]["B0"]["stats"].get("mean"))
            return NdviResult(
                latitude=lat,
                longitude=lon,
                ndvi_mean=round(ndvi, 4) if ndvi is not None else None,
                valid_fraction=round(valid / samples, 3),
                water_fraction=round(water, 3),
                surface="water" if water >= WATER_SURFACE else "land",
                interval_from=interval["interval"]["from"],
                interval_to=interval["interval"]["to"],
            )
        return NdviResult(**empty)  # only clouds for a month: unknown
