"""Sentinel-2, Sentinel-3 and Sentinel-5P statistics over a fire, from the Copernicus Data Space
Ecosystem (Sentinel Hub Statistical API, same OAuth client as the NDVI service).

Every function answers one question about a box and a time span with one request; results are
small summaries (means, shares, histograms), never imagery. Each request spends Copernicus
processing units from the account's monthly quota, so callers cache them (see fire_report).

What each measurement is, and is not:
* Sentinel-2 L2A (10-20 m, every ~5 days, optical: clouds block it).
  - NDVI = (NIR - red) / (NIR + red): green vegetation, 0 bare ... ~0.9 dense canopy.
  - NDMI = (NIR - SWIR1) / (NIR + SWIR1): water in the vegetation; below ~0 is dry.
  - dNBR = NBR(before) - NBR(after), NBR = (NIR - SWIR2) / (NIR + SWIR2): the burn scar. USGS
    severity classes: < 0.1 unburned, 0.1-0.27 low, 0.27-0.44 moderate-low, 0.44-0.66 moderate-high,
    >= 0.66 high. Burned area here = pixels with dNBR >= 0.27 (low-severity burns under a canopy or
    in grass that greens up fast are missed; regrowth after the fire lowers dNBR).
* Sentinel-3 SLSTR L2 land surface temperature (1 km, ~daily, clouds block it): the skin
  temperature of the ground, not the air.
* Sentinel-5P TROPOMI (~5.5 x 3.5 km, daily): total carbon monoxide column (mol/m2) and the UV
  aerosol index (smoke and dust absorb UV; > ~1 is a clear absorbing plume). Wind moves plumes
  far from the fire, so a fire's smoke is measured over a box around it and compared with the
  same box in the previous weeks.
"""

import math
import time
from datetime import datetime, timedelta

import httpx

from app.services.ndvi import STATS_URL, TOKEN_URL

CRS84 = "http://www.opengis.net/def/crs/EPSG/0/4326"
DNBR_EDGES = [-2.0, 0.1, 0.27, 0.44, 0.66, 2.0]
SEVERITY = ["unburned", "low", "moderate_low", "moderate_high", "high"]


class SentinelError(RuntimeError):
    pass


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _num(v) -> float | None:
    return float(v) if isinstance(v, (int, float)) and math.isfinite(v) else None


def box_around(lat: float, lon: float, half_km: float) -> list[float]:
    dlat = half_km / 110.574
    dlon = half_km / (111.320 * max(math.cos(math.radians(lat)), 1e-3))
    return [lon - dlon, lat - dlat, lon + dlon, lat + dlat]


def area_km2(bbox: list[float]) -> float:
    w, s, e, n = bbox
    return (n - s) * 110.574 * (e - w) * 111.320 * math.cos(math.radians((n + s) / 2))


class CdseStats:
    def __init__(self, http: httpx.AsyncClient, client_id: str, client_secret: str):
        self._http, self._id, self._secret = http, client_id, client_secret
        self._token: str | None = None
        self._exp = 0.0

    async def _auth(self) -> dict:
        if not self._token or time.time() > self._exp - 60:
            r = await self._http.post(TOKEN_URL, data={"grant_type": "client_credentials", "client_id": self._id,
                                                       "client_secret": self._secret}, timeout=20)
            r.raise_for_status()
            body = r.json()
            self._token, self._exp = body["access_token"], time.time() + float(body.get("expires_in", 600))
        return {"Authorization": f"Bearer {self._token}"}

    async def stats(self, bbox: list[float], data: dict, evalscript: str, t0: datetime, t1: datetime, res_deg: float,
                    interval: str | None = None, calculations: dict | None = None) -> list[dict]:
        days = max(1, math.ceil((t1 - t0).total_seconds() / 86400))
        agg = {"timeRange": {"from": _iso(t0), "to": _iso(t1)}, "aggregationInterval": {"of": interval or f"P{days}D",
               "lastIntervalBehavior": "SHORTEN"}, "evalscript": evalscript, "resx": res_deg, "resy": res_deg}
        body = {"input": {"bounds": {"bbox": bbox, "properties": {"crs": CRS84}}, "data": [data]}, "aggregation": agg}
        if calculations:
            body["calculations"] = calculations
        r = await self._http.post(STATS_URL, json=body, headers=await self._auth(), timeout=90)
        if r.status_code != 200:
            raise SentinelError(f"Copernicus statistics {r.status_code}: {r.text[:200]}")
        return r.json().get("data", [])

    # ------------------------------------------------------------------ Sentinel-2
    async def vegetation(self, bbox: list[float], t0: datetime, t1: datetime) -> dict:
        """NDVI and NDMI of the land, latest cloud-free value per pixel in [t0, t1)."""
        ev = """//VERSION=3
function setup() { return { input: [{ bands: ["B04", "B08", "B11", "SCL", "dataMask"] }], mosaicking: "ORBIT",
  output: [{ id: "ndvi", bands: 1, sampleType: "FLOAT32" }, { id: "ndmi", bands: 1, sampleType: "FLOAT32" }, { id: "dataMask", bands: 1 }] }; }
function clear(s) { return s.dataMask && [0, 1, 3, 6, 8, 9, 10, 11].indexOf(s.SCL) === -1; }
function evaluatePixel(samples) {
  for (var i = 0; i < samples.length; i++) { var s = samples[i]; if (clear(s)) {
    return { ndvi: [(s.B08 - s.B04) / (s.B08 + s.B04 + 1e-6)], ndmi: [(s.B08 - s.B11) / (s.B08 + s.B11 + 1e-6)], dataMask: [1] }; } }
  return { ndvi: [NaN], ndmi: [NaN], dataMask: [0] };
}"""
        data = await self.stats(bbox, {"type": "sentinel-2-l2a", "dataFilter": {"maxCloudCoverage": 80, "mosaickingOrder": "mostRecent"}},
                                ev, t0, t1, 0.0002)
        if not data:
            return {"available": False}
        o = data[-1]["outputs"]
        st = o["ndvi"]["bands"]["B0"]["stats"]
        n = st.get("sampleCount") or 0
        valid = n - (st.get("noDataCount") or 0)
        return {"available": valid > 0, "ndvi": _num(st.get("mean")), "ndmi": _num(o["ndmi"]["bands"]["B0"]["stats"].get("mean")),
                "clear_fraction": round(valid / n, 3) if n else 0.0, "from": _iso(t0), "to": _iso(t1)}

    async def burn_scar(self, bbox: list[float], fire_start: datetime, after_from: datetime, after_to: datetime,
                        before_days: int = 45) -> dict:
        """dNBR between the latest clear image before the fire and the latest clear image in
        [after_from, after_to]; burned area (dNBR >= 0.27) and severity shares."""
        start_ms = int(fire_start.timestamp() * 1000)
        after_ms = int(after_from.timestamp() * 1000)
        ev = f"""//VERSION=3
function setup() {{ return {{ input: [{{ bands: ["B08", "B12", "SCL", "dataMask"] }}], mosaicking: "ORBIT",
  output: [{{ id: "dnbr", bands: 1, sampleType: "FLOAT32" }}, {{ id: "burned", bands: 1, sampleType: "UINT8" }},
           {{ id: "severity", bands: 1, sampleType: "UINT8" }}, {{ id: "dataMask", bands: 1 }}] }}; }}
var EDGES = [0.1, 0.27, 0.44, 0.66];
function clear(s) {{ return s.dataMask && [0, 1, 3, 6, 8, 9, 10, 11].indexOf(s.SCL) === -1; }}
function nbr(s) {{ return (s.B08 - s.B12) / (s.B08 + s.B12 + 1e-6); }}
function evaluatePixel(samples, scenes) {{
  var pre = null, post = null;
  for (var i = 0; i < samples.length; i++) {{
    var t = Date.parse(scenes.orbits[i].dateFrom); var s = samples[i];
    if (!clear(s)) continue;
    if (t < {start_ms} && pre === null) pre = nbr(s);           // samples are newest first
    if (t >= {after_ms} && post === null) post = nbr(s);
  }}
  if (pre === null || post === null) return {{ dnbr: [NaN], burned: [0], severity: [0], dataMask: [0] }};
  var d = pre - post, k = 0;
  while (k < EDGES.length && d >= EDGES[k]) k++;
  return {{ dnbr: [d], burned: [d >= 0.27 ? 1 : 0], severity: [k], dataMask: [1] }};
}}"""
        t0 = fire_start - timedelta(days=before_days)
        data = await self.stats(bbox, {"type": "sentinel-2-l2a", "dataFilter": {"maxCloudCoverage": 80, "mosaickingOrder": "mostRecent"}},
                                ev, t0, after_to, 0.0002,
                                calculations={"severity": {"histograms": {"default": {"nBins": 5, "lowEdge": 0, "highEdge": 5}}}})
        if not data:
            return {"available": False, "reason": "no_images"}
        o = data[-1]["outputs"]
        st = o["burned"]["bands"]["B0"]["stats"]
        n = st.get("sampleCount") or 0
        valid = n - (st.get("noDataCount") or 0)
        if valid <= 0:
            return {"available": False, "reason": "no_clear_pair"}
        hist = o["severity"]["bands"]["B0"].get("histogram", {}).get("bins", [])
        counts = [b.get("count", 0) for b in hist]
        total = sum(counts) or 1
        box_km2 = area_km2(bbox)
        burned_share = _num(st.get("mean")) or 0.0
        return {
            "available": True,
            "clear_fraction": round(valid / n, 3),
            "burned_km2": round(burned_share * box_km2 * valid / n, 3),
            "box_km2": round(box_km2, 2),
            "mean_dnbr": _num(o["dnbr"]["bands"]["B0"]["stats"].get("mean")),
            "severity": {name: round(c / total, 4) for name, c in zip(SEVERITY, counts)} if len(counts) == len(SEVERITY) else {},
            "before_from": _iso(t0), "after_from": _iso(after_from), "after_to": _iso(after_to),
        }

    # ------------------------------------------------------------------ Sentinel-3 / 5P
    async def _daily_mean(self, bbox, data, band, t0, t1, res, percentile: int | None = None) -> list[tuple[str, float]]:
        """Per day: the mean of the box (or a percentile of its pixels)."""
        ev = f"""//VERSION=3
function setup() {{ return {{ input: [{{ bands: ["{band}", "dataMask"] }}], output: [{{ id: "v", bands: 1, sampleType: "FLOAT32" }}, {{ id: "dataMask", bands: 1 }}] }}; }}
function evaluatePixel(s) {{ return {{ v: [s.{band}], dataMask: [s.dataMask] }}; }}"""
        calc = {"v": {"statistics": {"default": {"percentiles": {"k": [percentile]}}}}} if percentile else None
        out = []
        for d in await self.stats(bbox, data, ev, t0, t1, res, interval="P1D", calculations=calc):
            st = d["outputs"]["v"]["bands"]["B0"]["stats"]
            v = _num((st.get("percentiles") or {}).get(f"{float(percentile)}")) if percentile else _num(st.get("mean"))
            if v is not None:
                out.append((d["interval"]["from"][:10], v))
        return out

    async def surface_temperature(self, bbox, during: tuple[datetime, datetime], before_days: int = 30) -> dict:
        """Daytime land surface temperature (deg C) during the fire vs the weeks before (same box).
        Daytime = the descending pass (~10:00 local). Clouds pass as cold "ground", so each day is
        summarised by its warmest 10 % of pixels (the clear ground) instead of the mean."""
        t0 = during[0] - timedelta(days=before_days)
        days = await self._daily_mean(bbox, {"type": "sentinel-3-slstr-l2", "dataFilter": {"orbitDirection": "DESCENDING"}},
                                      "LST", t0, during[1], 0.01, percentile=90)
        return _anomaly(days, during[0], scale=1.0, offset=-273.15, digits=1)

    async def smoke(self, bbox, during: tuple[datetime, datetime], before_days: int = 30) -> dict:
        t0 = during[0] - timedelta(days=before_days)
        s5p = {"type": "sentinel-5p-l2"}  # near-real-time products are only kept for recent days
        co = await self._daily_mean(bbox, s5p, "CO", t0, during[1], 0.05)
        ai = await self._daily_mean(bbox, s5p, "AER_AI_340_380", t0, during[1], 0.05)
        # CO in mol/m2 -> 1e18 molecules/cm2 (the unit plume studies quote): x 6.022e23 / 1e4 / 1e18
        return {"co": _anomaly(co, during[0], scale=6.022e23 / 1e4 / 1e18, digits=3), "aerosol_index": _anomaly(ai, during[0], digits=2)}


def _anomaly(days: list[tuple[str, float]], split: datetime, scale: float = 1.0, offset: float = 0.0, digits: int = 2) -> dict:
    """Mean during [split, ...) and before it, and the peak day during."""
    cut = split.strftime("%Y-%m-%d")
    before = [v * scale + offset for d, v in days if d < cut]
    during = [(d, v * scale + offset) for d, v in days if d >= cut]
    if not during:
        return {"available": False, "days_before": len(before)}
    mb = sum(before) / len(before) if before else None
    md = sum(v for _, v in during) / len(during)
    peak = max(during, key=lambda x: x[1])
    return {"available": True, "during": round(md, digits), "before": round(mb, digits) if mb is not None else None,
            "change": round(md - mb, digits) if mb is not None else None, "peak": round(peak[1], digits), "peak_day": peak[0],
            "days_during": len(during), "days_before": len(before)}
