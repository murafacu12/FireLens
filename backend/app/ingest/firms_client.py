"""Thin async client for the NASA FIRMS area API.

https://firms.modaps.eosdis.nasa.gov/api/area/
GET /api/area/csv/{MAP_KEY}/{SOURCE}/{AREA}/{DAY_RANGE}[/{DATE}]
"""

from datetime import date, timedelta

import httpx

from app.ingest.sources import get_source


class FirmsError(RuntimeError):
    pass


class FirmsClient:
    def __init__(self, http: httpx.AsyncClient, map_key: str, base_url: str, max_day_range: int = 5):
        self._http = http
        self._key = map_key
        self._base = base_url.rstrip("/")
        self._max_days = max_day_range

    def _redact(self, text: str) -> str:
        return text.replace(self._key, "***") if self._key else text

    async def fetch_area_csv(self, source: str, area: str, days: int, start: date | None = None) -> str:
        get_source(source)  # validate before spending a FIRMS transaction
        url = f"{self._base}/api/area/csv/{self._key}/{source}/{area}/{days}"
        if start:
            url += f"/{start.isoformat()}"
        try:
            resp = await self._http.get(url, timeout=60)
        except httpx.HTTPError as exc:
            raise FirmsError(self._redact(f"FIRMS request failed: {exc}")) from None
        body = resp.text
        # FIRMS answers some errors (bad key, quota) with 200 + a plain-text message.
        if resp.status_code != 200 or "latitude" not in body[:200].lower():
            raise FirmsError(self._redact(f"FIRMS {resp.status_code}: {body[:200].strip()}"))
        return body

    async def fetch_range(self, source: str, area: str, days: int, start: date | None = None) -> list[str]:
        """Split long ranges into chunks the API accepts (DAY_RANGE is capped)."""
        if days <= self._max_days:
            return [await self.fetch_area_csv(source, area, days, start)]
        if start is None:
            start = date.today() - timedelta(days=days - 1)
        chunks = []
        offset = 0
        while offset < days:
            span = min(self._max_days, days - offset)
            chunks.append(await self.fetch_area_csv(source, area, span, start + timedelta(days=offset)))
            offset += span
        return chunks
