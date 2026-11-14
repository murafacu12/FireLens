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

# Clouds, cloud shadow, cirrus and snow (Scene Classification) are masked out.
EVALSCRIPT = """//VERSION=3
function setup() {
  return {
    input: [{ bands: ["B04", "B08", "SCL", "dataMask"] }],
    output: [
      { id: "ndvi", bands: 1, sampleType: "FLOAT32" },
      { id: "dataMask", bands: 1 }
    ]
  };
}
function evaluatePixel(s) {
  var invalid = [3, 8, 9, 10, 11].indexOf(s.SCL) !== -1;
  var ndvi = (s.B08 - s.B04) / (s.B08 + s.B04 + 1e-6);
  return { ndvi: [ndvi], dataMask: [s.dataMask && !invalid ? 1 : 0] };
}"""


class NdviResult(BaseModel):
    latitude: float
    longitude: float
    ndvi_mean: float | None
    valid_fraction: float | None
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
        # Most recent interval with valid (cloud-free) pixels wins.
        for interval in sorted(resp.json().get("data", []), key=lambda d: d["interval"]["from"], reverse=True):
            stats = interval["outputs"]["ndvi"]["bands"]["B0"]["stats"]
            samples = stats.get("sampleCount") or 0
            valid = samples - (stats.get("noDataCount") or 0)
            mean = stats.get("mean")
            if valid > 0 and isinstance(mean, (int, float)) and math.isfinite(mean):
                return NdviResult(
                    latitude=lat,
                    longitude=lon,
                    ndvi_mean=round(mean, 4),
                    valid_fraction=round(valid / samples, 3),
                    interval_from=interval["interval"]["from"],
                    interval_to=interval["interval"]["to"],
                )
        return NdviResult(latitude=lat, longitude=lon, ndvi_mean=None, valid_fraction=None, interval_from=None, interval_to=None)
