"""Meteomatics Weather API client (https://api.meteomatics.com), HTTP basic auth.

Credentials come from .env (FIRELENS_METEOMATICS_USERNAME / _PASSWORD) and are never logged:
requests go through httpx with `auth=`, so they are not part of any URL.

Request form: /{time}/{parameters}/{locations}/json, where
  time        2026-11-01T00:00:00Z--2026-11-02T00:00:00Z:PT1H   (start--end:step)
  parameters  wind_speed_10m:kmh,wind_dir_10m:d,...              (comma-separated)
  locations   -31.4,-64.2+-30.1,-60.5                            (several points with '+')
"""

from datetime import datetime

import httpx

BASE = "https://api.meteomatics.com"
# what the fire-spread work needs, per hour
SPREAD_PARAMS = ["wind_speed_10m:kmh", "wind_dir_10m:d", "wind_gusts_10m_1h:kmh", "relative_humidity_2m:p",
                 "precip_1h:mm", "t_2m:C"]


class MeteomaticsError(RuntimeError):
    pass


def _t(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


class Meteomatics:
    def __init__(self, http: httpx.AsyncClient | httpx.Client, username: str, password: str):
        self._http = http
        self._auth = (username, password)

    def _url(self, t0: datetime, t1: datetime, step: str, params: list[str], points: list[tuple[float, float]]) -> str:
        locs = "+".join(f"{lat:.4f},{lon:.4f}" for lat, lon in points)
        return f"{BASE}/{_t(t0)}--{_t(t1)}:{step}/{','.join(params)}/{locs}/json"

    @staticmethod
    def _parse(body: dict, points: list[tuple[float, float]]) -> list[dict]:
        """-> one Open-Meteo-like hourly block per point: {"time": [...], param: [...]}."""
        out = [{"time": []} for _ in points]
        for entry in body.get("data", []):
            param = entry["parameter"]
            for i, coord in enumerate(entry["coordinates"]):
                dates = coord["dates"]
                if not out[i]["time"]:
                    out[i]["time"] = [d["date"].replace("Z", "") for d in dates]
                out[i][param] = [d["value"] for d in dates]
        return out

    def hourly_sync(self, t0: datetime, t1: datetime, points: list[tuple[float, float]], params: list[str] = SPREAD_PARAMS) -> list[dict]:
        r = self._http.get(self._url(t0, t1, "PT1H", params, points), auth=self._auth, timeout=120)
        if r.status_code != 200:
            raise MeteomaticsError(f"Meteomatics {r.status_code}: {r.text[:200]}")
        return self._parse(r.json(), points)

    async def hourly(self, t0: datetime, t1: datetime, points: list[tuple[float, float]], params: list[str] = SPREAD_PARAMS) -> list[dict]:
        r = await self._http.get(self._url(t0, t1, "PT1H", params, points), auth=self._auth, timeout=120)
        if r.status_code != 200:
            raise MeteomaticsError(f"Meteomatics {r.status_code}: {r.text[:200]}")
        return self._parse(r.json(), points)

    def user_stats_sync(self) -> dict:
        """The account's plan, limits and usage (no weather data)."""
        r = self._http.get(f"{BASE}/user_stats_json", auth=self._auth, timeout=30)
        if r.status_code != 200:
            raise MeteomaticsError(f"Meteomatics {r.status_code}: {r.text[:200]}")
        return r.json()
