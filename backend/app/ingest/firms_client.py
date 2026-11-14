"""Async client for NASA FIRMS.

Keyless:   public active-fire files (last 24h / 48h / 7d per region).
MAP_KEY:   area API (any date, NRT and SP archive), data availability, key status.
           GET /api/area/csv/{MAP_KEY}/{SOURCE}/{west,south,east,north|world}/{DAY_RANGE}[/{DATE}]
           (FIRMS retired the country API: it now answers "Invalid API call.")
"""

import csv
import io
from datetime import date, timedelta

import httpx

from app.ingest.sources import PUBLIC_REGIONS, PUBLIC_WINDOWS, get_source


class FirmsError(RuntimeError):
    pass


class FirmsRateLimited(FirmsError):
    pass


class FirmsClient:
    def __init__(self, http: httpx.AsyncClient, map_key: str | None, base_url: str, max_day_range: int = 5):
        self._http = http
        self._key = map_key or None
        self._base = base_url.rstrip("/")
        self._max_days = max_day_range

    @property
    def has_key(self) -> bool:
        return self._key is not None

    def _redact(self, text: str) -> str:
        return text.replace(self._key, "***") if self._key else text

    def _require_key(self) -> str:
        if not self._key:
            raise FirmsError(
                "A FIRMS MAP_KEY is required for this request (history older than 7 days). "
                "Get one free at https://firms.modaps.eosdis.nasa.gov/api/map_key/ and set FIRELENS_FIRMS_MAP_KEY."
            )
        return self._key

    async def _get_csv(self, url: str, timeout: float = 120) -> str:
        try:
            resp = await self._http.get(url, timeout=timeout)
        except httpx.HTTPError as exc:
            raise FirmsError(self._redact(f"FIRMS request failed: {exc!r}")) from None
        body = resp.text
        head = body[:200].strip()
        if resp.status_code == 429 or "exceed" in head.lower():
            raise FirmsRateLimited(self._redact(f"FIRMS rate limit: {head}"))
        # FIRMS answers some errors (bad key, bad source) with 200 + a plain-text message.
        if resp.status_code != 200 or "latitude" not in head.lower():
            raise FirmsError(self._redact(f"FIRMS {resp.status_code}: {head}"))
        return body

    # ------------------------------------------------------------------ keyless
    async def fetch_public_feed(self, source: str, region: str, window: str) -> str:
        src = get_source(source)
        if not src.public_feed:
            raise FirmsError(f"{src.id} has no public feed (only NRT products do)")
        if region not in PUBLIC_REGIONS:
            raise FirmsError(f"unknown region {region!r}; one of {', '.join(PUBLIC_REGIONS)}")
        if window not in PUBLIC_WINDOWS:
            raise FirmsError(f"window must be one of {PUBLIC_WINDOWS}")
        folder, prefix = src.public_feed
        return await self._get_csv(f"{self._base}/data/active_fire/{folder}/csv/{prefix}_{region}_{window}.csv")

    # ------------------------------------------------------------------ MAP_KEY
    async def fetch_area_csv(self, source: str, area: str, days: int, start: date | None = None) -> str:
        key = self._require_key()
        get_source(source)  # validate before spending a FIRMS transaction
        url = f"{self._base}/api/area/csv/{key}/{source}/{area}/{days}"
        return await self._get_csv(url + (f"/{start.isoformat()}" if start else ""))

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

    async def data_availability(self) -> dict[str, tuple[date, date]]:
        """Date span currently served for each product, e.g. {'VIIRS_SNPP_SP': (2012-01-20, 2025-06-30)}."""
        key = self._require_key()
        try:
            resp = await self._http.get(f"{self._base}/api/data_availability/csv/{key}/ALL", timeout=60)
        except httpx.HTTPError as exc:
            raise FirmsError(self._redact(f"FIRMS request failed: {exc!r}")) from None
        if resp.status_code != 200 or "data_id" not in resp.text[:200]:
            raise FirmsError(self._redact(f"FIRMS availability {resp.status_code}: {resp.text[:200].strip()}"))
        out = {}
        for row in csv.DictReader(io.StringIO(resp.text)):
            try:
                out[row["data_id"].strip()] = (date.fromisoformat(row["min_date"].strip()), date.fromisoformat(row["max_date"].strip()))
            except (KeyError, ValueError):
                continue
        return out

    async def key_status(self) -> dict:
        """Transaction usage for the MAP_KEY (FIRMS allows a fixed number per 10-minute window)."""
        key = self._require_key()
        try:
            resp = await self._http.get(f"{self._base}/mapserver/mapkey_status/", params={"MAP_KEY": key}, timeout=30)
            resp.raise_for_status()
            return resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise FirmsError(self._redact(f"FIRMS key status failed: {exc!r}")) from None
