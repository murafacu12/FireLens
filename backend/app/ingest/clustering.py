"""Group harmonized detections (any sensor) into fire events, and describe each event.

Clustering = connected components of a space-time graph (single linkage, i.e. ST-DBSCAN with
minPts = 1). It is deterministic and independent of processing order, which is what lets an
incremental update produce exactly the events a full rebuild would. Two detections are linked by:

* E1 contiguity: observed at most `max_gap_hours` apart and their pixel footprints within
  `tolerance_km` of touching (geodesic distance between centres <= r_i + r_j + tolerance);
* E2 continuity: observed between `max_gap_hours` and `continuity_gap_hours` apart and their pixels
  overlapping (<= r_i + r_j + continuity_tolerance_km, default 0). This bridges missed passes,
  cloud and smouldering on the *same ground* without chaining new burns next to old scars.

See docs/CLUSTERING_ALGORITHM.md for the measurements behind the defaults.
"""

import hashlib
import json
import math
import statistics
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app.schemas import FireDetection

KM_PER_DEG_LAT = 110.574
KM_PER_DEG_LON_EQ = 111.320
AREA_CELL_KM = 0.125
EARTH_RADIUS_KM = 6371.0088
KM_PER_DEG_SPHERE = math.pi * EARTH_RADIUS_KM / 180  # 111.195: the sphere distances are measured on


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(a)))


def footprint_radius_km(d: FireDetection) -> float:
    """Half-diagonal of the actual pixel (scan x track): pixel extent + where the fire can be inside it."""
    return 0.5 * math.hypot(d.scan_km, d.track_km)


class _UnionFind:
    def __init__(self, n: int) -> None:
        self.parent = list(range(n))

    def find(self, i: int) -> int:
        while self.parent[i] != i:
            self.parent[i] = self.parent[self.parent[i]]
            i = self.parent[i]
        return i

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def lon_columns(lat: float, gx: int, link_km: float, cell_deg: float, cell_lon: float, n_lon: int):
    """Longitude cells that can hold a point within `link_km` of a point at `lat` in column gx.

    Exact spherical bound: two points at most L apart, both at |latitude| <= phi, differ in longitude
    by at most 2*asin(sin(L/2R) / cos(phi)). Near a pole that reaches 180 degrees: every column.
    """
    phi = math.radians(min(abs(lat) + cell_deg, 90.0))
    arg = math.sin(link_km / (2 * EARTH_RADIUS_KM)) / max(math.cos(phi), 1e-12)
    if arg >= 1:
        return range(n_lon)
    kx = math.ceil(math.degrees(2 * math.asin(arg)) / cell_lon)
    if 2 * kx + 1 >= n_lon:
        return range(n_lon)
    return [(gx + dx) % n_lon for dx in range(-kx, kx + 1)]


def cluster_detections(
    detections: Sequence[FireDetection],
    tolerance_km: float = 1.0,
    max_gap_hours: float = 24.0,
    continuity_gap_hours: float = 0.0,
    continuity_tolerance_km: float = 0.0,
) -> list[list[FireDetection]]:
    n = len(detections)
    if n == 0:
        return []
    radii = [footprint_radius_km(d) for d in detections]
    times = [d.acquired_at.timestamp() for d in detections]
    # Unit vectors: the chord between two of them is a monotonic function of their great-circle
    # distance, so "distance <= reach" is tested exactly as "chord <= 2 sin(reach / 2R)" with three
    # multiplications instead of a haversine (it is still geodesic, not a lat/lon difference).
    xyz = []
    for d in detections:
        phi, lam = math.radians(d.latitude), math.radians(d.longitude)
        xyz.append((math.cos(phi) * math.cos(lam), math.cos(phi) * math.sin(lam), math.sin(phi)))
    gap_s = max_gap_hours * 3600
    cont_s = max(continuity_gap_hours * 3600, gap_s)
    max_link_km = 2 * max(radii) + max(tolerance_km, continuity_tolerance_km if cont_s > gap_s else 0.0)
    any_link_c2 = (2 * math.sin(max_link_km / (2 * EARTH_RADIUS_KM))) ** 2  # cheap global reject
    cell_deg = max_link_km / KM_PER_DEG_SPHERE  # a cell spans the longest possible link
    n_lon = max(1, math.floor(360 / cell_deg))  # longitude cells wrap at the antimeridian (179.9 E ~ 179.9 W)
    cell_lon = 360 / n_lon
    # Time buckets of max_gap; continuity looks a few buckets further back/forward.
    kt = math.ceil(cont_s / gap_s)
    grid: dict[tuple[int, int, int], list[int]] = defaultdict(list)
    uf = _UnionFind(n)
    two_r = 2 * EARTH_RADIUS_KM

    for i, d in enumerate(detections):
        gy = math.floor(d.latitude / cell_deg)
        gx = math.floor((d.longitude + 180) / cell_lon) % n_lon
        gt = math.floor(times[i] / gap_s)
        xi, yi, zi = xyz[i]
        ti, ri = times[i], radii[i]
        cols = lon_columns(d.latitude, gx, max_link_km, cell_deg, cell_lon, n_lon)
        for dt in range(-kt, kt + 1):
            for dy in (-1, 0, 1):
                for cx in cols:
                    for j in grid.get((gy + dy, cx, gt + dt), ()):
                        apart = abs(times[j] - ti)
                        if apart > cont_s:
                            continue
                        xj, yj, zj = xyz[j]
                        c2 = (xi - xj) ** 2 + (yi - yj) ** 2 + (zi - zj) ** 2
                        if c2 > any_link_c2:
                            continue
                        reach = ri + radii[j] + (tolerance_km if apart <= gap_s else continuity_tolerance_km)
                        if c2 <= (2 * math.sin(reach / two_r)) ** 2:
                            uf.union(i, j)
        grid[(gy, gx, gt)].append(i)

    groups: dict[int, list[FireDetection]] = defaultdict(list)
    for i, d in enumerate(detections):
        groups[uf.find(i)].append(d)
    return list(groups.values())


# ------------------------------------------------------------------ longitude helpers (antimeridian)


def wrap_lon(lon: float) -> float:
    """Normalise to [-180, 180)."""
    return (lon + 180.0) % 360.0 - 180.0


def lon_offset(lon: float, ref: float) -> float:
    """Signed degrees east of `ref`, the short way round (179.9 is 0.2 deg west of -179.9)."""
    return wrap_lon(lon - ref)


def lon_interval(lons: Sequence[float]) -> tuple[float, float]:
    """Smallest longitude interval holding every value: (west, east), with west > east when it
    crosses the antimeridian (the convention the bbox filters use)."""
    xs = sorted(set(lons))
    if len(xs) == 1:
        return xs[0], xs[0]
    gaps = [(xs[k + 1] - xs[k], k) for k in range(len(xs) - 1)]
    wrap_gap = xs[0] + 360 - xs[-1]
    widest, k = max(gaps)
    if wrap_gap >= widest:
        return xs[0], xs[-1]
    return xs[k + 1], xs[k]  # the empty arc is inside: the interval wraps round 180


def weighted_centre(points: Sequence[tuple[float, float]], weights: Sequence[float]) -> tuple[float, float]:
    """Weighted mean position on the sphere (unit vectors), correct across the antimeridian."""
    x = y = z = 0.0
    for (lat, lon), w in zip(points, weights):
        p, l = math.radians(lat), math.radians(lon)
        x += w * math.cos(p) * math.cos(l)
        y += w * math.cos(p) * math.sin(l)
        z += w * math.sin(p)
    return math.degrees(math.atan2(z, math.hypot(x, y))), math.degrees(math.atan2(y, x))


# ------------------------------------------------------------------ area measures

# Each detection counts as its sensor's *nominal* (nadir) pixel, not its actual scan x track
# footprint: the footprint grows up to ~10x toward the swath edge (MODIS 1 -> 9.6 km2), so the
# same fire used to measure bigger just because it was seen at an angle.
NOMINAL_PIXEL_KM = {"VIIRS": 0.375, "MODIS": 1.0}
AREA_BASIS = {"VIIRS": "viirs_375m", "MODIS": "modis_1km"}


def _basis_pixels(members: Sequence[FireDetection]) -> tuple[list, str]:
    viirs = [d for d in members if d.instrument == "VIIRS"]
    pixels = viirs or list(members)
    return pixels, AREA_BASIS["VIIRS" if viirs else pixels[0].instrument]


def _pixel_cells(d, cos_lat: float, ref_lon: float = 0.0):
    c = AREA_CELL_KM
    half = NOMINAL_PIXEL_KM[d.instrument] / 2
    x = lon_offset(d.longitude, ref_lon) * KM_PER_DEG_LON_EQ * cos_lat
    y = d.latitude * KM_PER_DEG_LAT
    # cells whose centre lies inside the square: unbiased, and exactly 3x3 / 8x8 cells on average
    i0, i1 = math.ceil((x - half) / c - 0.5), math.floor((x + half) / c - 0.5)
    j0, j1 = math.ceil((y - half) / c - 0.5), math.floor((y + half) / c - 0.5)
    for i in range(i0, i1 + 1):
        for j in range(j0, j1 + 1):
            yield i, j


def estimate_area_km2(members: Sequence[FireDetection], ref_lat: float, ref_lon: float = 0.0) -> tuple[float, str]:
    """Detected footprint: union of nominal-resolution pixels on a 125 m grid, finest sensor available.

    Returns (area_km2, basis). VIIRS (375 m) is used whenever the event has VIIRS detections, so
    a fire seen by both sensors is not inflated by MODIS' 1 km pixels or double counted. This is the
    *active-fire detection footprint* over the event's life at sensor resolution, not burned area.
    """
    pixels, basis = _basis_pixels(members)
    cos_lat = max(math.cos(math.radians(ref_lat)), 1e-3)
    cells: set[tuple[int, int]] = set()
    for d in pixels:
        cells.update(_pixel_cells(d, cos_lat, ref_lon))
    return round(len(cells) * AREA_CELL_KM * AREA_CELL_KM, 3), basis


def detection_hull_km2(members: Sequence[FireDetection], ref_lat: float, ref_lon: float = 0.0) -> float:
    """Area of the convex hull of detection centres (local projection): where detections occurred.
    0 for fewer than 3 distinct, non-collinear points. Not a perimeter, not burned area."""
    cos_lat = max(math.cos(math.radians(ref_lat)), 1e-3)
    pts = sorted({(lon_offset(d.longitude, ref_lon) * KM_PER_DEG_LON_EQ * cos_lat, d.latitude * KM_PER_DEG_LAT) for d in members})
    if len(pts) < 3:
        return 0.0

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower, upper = [], []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    hull = lower[:-1] + upper[:-1]
    area = 0.0
    for k in range(len(hull)):
        x1, y1 = hull[k]
        x2, y2 = hull[(k + 1) % len(hull)]
        area += x1 * y2 - x2 * y1
    return round(abs(area) / 2, 3)


GROWTH_MIN_DAYS = 3
GROWTH_MIN_PASSES_PER_DAY = 2


def footprint_growth(members: Sequence[FireDetection], ref_lat: float, ref_lon: float = 0.0) -> tuple[float | None, float | None]:
    """Apparent growth of the detected footprint (km2/day) with its standard error.

    Least-squares slope of the cumulative footprint at the end of each UTC day. Only computed with
    >= 3 distinct days having >= 2 overpasses each; otherwise (None, None). It describes how the
    *detected* footprint grew, not the fire's rate of spread.
    """
    days: dict = defaultdict(set)
    for d in members:
        days[d.acquired_at.date()].add((d.satellite, d.acquired_at))
    good = sorted(day for day, passes in days.items() if len(passes) >= GROWTH_MIN_PASSES_PER_DAY)
    if len(good) < GROWTH_MIN_DAYS:
        return None, None
    pixels, _ = _basis_pixels(members)
    cos_lat = max(math.cos(math.radians(ref_lat)), 1e-3)
    by_day: dict = defaultdict(list)
    for d in pixels:
        by_day[d.acquired_at.date()].append(d)
    cells: set = set()
    xs, ys = [], []
    for day in sorted(by_day):
        for d in by_day[day]:
            cells.update(_pixel_cells(d, cos_lat, ref_lon))
        if day in good:
            xs.append((day - good[0]).days)
            ys.append(len(cells) * AREA_CELL_KM * AREA_CELL_KM)
    n = len(xs)
    if n < GROWTH_MIN_DAYS:
        return None, None
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    resid = sum((y - (my + slope * (x - mx))) ** 2 for x, y in zip(xs, ys))
    se = math.sqrt(resid / (n - 2) / sxx) if n > 2 else None
    return round(slope, 3), (round(se, 3) if se is not None else None)


# ------------------------------------------------------------------ intensity & lifecycle

TREND_RATIO = 2.0
TREND_MIN_MW = 50.0
TREND_PREVIOUS_PASSES = 3


@dataclass
class Pass:
    """One satellite overpass over the fire: the whole-fire FRP that pass saw."""

    satellite: str
    instrument: str
    acquired_at: datetime
    pixels: int
    frp_sum: float
    frp_max: float


def overpasses(members: Sequence[FireDetection]) -> list[Pass]:
    acc: dict[tuple, list] = {}
    for d in members:
        key = (d.acquired_at, d.satellite)
        a = acc.setdefault(key, [d.instrument, 0, 0.0, 0.0])
        a[1] += 1
        a[2] += d.frp_mw or 0.0
        a[3] = max(a[3], d.frp_mw or 0.0)
    return [Pass(sat, ins, t, n, round(fs, 2), round(fm, 2)) for (t, sat), (ins, n, fs, fm) in sorted(acc.items())]


def intensity_trend(passes: list[Pass]) -> str:
    """Latest overpass vs the median of the previous ones *of the same sensor* (sensors differ)."""
    if not passes:
        return "insufficient_data"
    last = passes[-1]
    prev = [p.frp_sum for p in passes[:-1] if p.instrument == last.instrument][-TREND_PREVIOUS_PASSES:]
    if len(prev) < 2:
        return "insufficient_data"
    med = statistics.median(prev)
    if last.frp_sum >= TREND_RATIO * med and last.frp_sum - med >= TREND_MIN_MW:
        return "rising"
    if last.frp_sum * TREND_RATIO <= med and med - last.frp_sum >= TREND_MIN_MW:
        return "falling"
    return "stable"


def latest_run_start(times: Sequence[datetime], active_hours: float) -> datetime:
    """Start of the latest stretch of detections not interrupted by a quiet period > active_hours."""
    ts = sorted(set(times))
    start = ts[0]
    for a, b in zip(ts, ts[1:]):
        if b - a > timedelta(hours=active_hours):
            start = b
    return start


def lifecycle_state(start_at: datetime, end_at: datetime, run_start_at: datetime, now: datetime,
                    active_hours: float, archive_days: float) -> str:
    """ACTIVE / REACTIVATED / INACTIVE / ARCHIVED from the detections and the current time.

    Absence of detections does not mean a fire is out (cloud, smoke, canopy, revisit gaps), so
    INACTIVE only states "no detection for active_hours".
    """
    quiet = now - end_at
    if quiet > timedelta(days=archive_days):
        return "archived"
    if quiet > timedelta(hours=active_hours):
        return "inactive"
    if run_start_at > start_at and end_at - run_start_at < timedelta(hours=active_hours):
        return "reactivated"
    return "active"


def legacy_status(state: str) -> str:
    return "active" if state in ("active", "reactivated") else "inactive"


# ------------------------------------------------------------------ the event


@dataclass
class EventDraft:
    id: str
    latitude: float
    longitude: float
    west: float
    south: float
    east: float
    north: float
    start_at: datetime
    end_at: datetime
    detection_count: int
    modis_count: int
    viirs_count: int
    max_frp_mw: float
    total_frp_mw: float  # deprecated: a sum of instantaneous observations, not energy
    area_km2: float  # detected footprint
    area_basis: str
    max_confidence_pct: int
    status: str
    member_ids: list[str]
    observed_hours: float = 0.0
    pass_count: int = 0
    evidence: str = "single_detection"
    satellite_counts: str = "{}"
    mean_frp_viirs_mw: float | None = None
    mean_frp_modis_mw: float | None = None
    peak_pass_frp_mw: float = 0.0
    peak_pass_sensor: str | None = None
    intensity_trend: str = "insufficient_data"
    extent_km: float = 0.0
    detection_hull_km2: float = 0.0
    footprint_growth_km2_per_day: float | None = None
    footprint_growth_se: float | None = None
    lifecycle_state: str = "active"
    run_start_at: datetime | None = None
    passes: list[Pass] = field(default_factory=list)

    NOT_COLUMNS = ("member_ids", "passes")

    def row(self) -> dict:
        return {k: v for k, v in vars(self).items() if k not in self.NOT_COLUMNS}


def event_id_for(first_member_id: str) -> str:
    """Deterministic: the hash of the earliest detection. Merges keep the oldest event's id."""
    return "evt_" + hashlib.sha1(first_member_id.encode()).hexdigest()[:12]


def _mean_frp(members, instrument: str) -> float | None:
    vals = [d.frp_mw for d in members if d.instrument == instrument and d.frp_mw is not None]
    return round(sum(vals) / len(vals), 2) if vals else None


def build_event(members: Sequence[FireDetection], now: datetime, active_hours: float = 24.0,
                archive_days: float = 30.0) -> EventDraft:
    ordered = sorted(members, key=lambda d: (d.acquired_at, d.id))
    frps = [d.frp_mw or 0.0 for d in ordered]
    total_frp = sum(frps)
    # FRP-weighted centroid sits on the most intense part of the fire (mean on the sphere, so an
    # event spanning the antimeridian is centred at ~180, not at 0)
    weights = frps if total_frp > 0 else [1.0] * len(ordered)
    lat, lon = weighted_centre([(d.latitude, d.longitude) for d in ordered], weights)
    start_at, end_at = ordered[0].acquired_at, ordered[-1].acquired_at
    west, east = lon_interval([d.longitude for d in ordered])
    south, north = min(d.latitude for d in ordered), max(d.latitude for d in ordered)
    area, basis = estimate_area_km2(ordered, lat, lon)
    passes = overpasses(ordered)
    peak = max(passes, key=lambda p: (p.frp_sum, p.acquired_at))
    run_start = latest_run_start([d.acquired_at for d in ordered], active_hours)
    state = lifecycle_state(start_at, end_at, run_start, now, active_hours, archive_days)
    sats: dict[str, int] = defaultdict(int)
    for d in ordered:
        sats[d.satellite] += 1
    growth, growth_se = footprint_growth(ordered, lat, lon)
    return EventDraft(
        id=event_id_for(ordered[0].id),
        latitude=round(lat, 5),
        longitude=round(lon, 5),
        west=west,
        south=south,
        east=east,
        north=north,
        start_at=start_at,
        end_at=end_at,
        detection_count=len(ordered),
        modis_count=sum(1 for d in ordered if d.instrument == "MODIS"),
        viirs_count=sum(1 for d in ordered if d.instrument == "VIIRS"),
        max_frp_mw=round(max(frps), 2),
        total_frp_mw=round(total_frp, 2),
        area_km2=area,
        area_basis=basis,
        max_confidence_pct=max(d.confidence_pct for d in ordered),
        status=legacy_status(state),
        member_ids=[d.id for d in ordered],
        observed_hours=round((end_at - start_at).total_seconds() / 3600, 2),
        pass_count=len(passes),
        evidence="single_detection" if len(ordered) == 1 else "single_pass" if len(passes) == 1 else "multi_pass",
        satellite_counts=json.dumps(dict(sorted(sats.items()))),
        mean_frp_viirs_mw=_mean_frp(ordered, "VIIRS"),
        mean_frp_modis_mw=_mean_frp(ordered, "MODIS"),
        peak_pass_frp_mw=peak.frp_sum,
        peak_pass_sensor=peak.instrument,
        intensity_trend=intensity_trend(passes),
        extent_km=round(max(haversine_km(lat, west, lat, east), haversine_km(south, lon, north, lon)), 3),
        detection_hull_km2=detection_hull_km2(ordered, lat, lon),
        footprint_growth_km2_per_day=growth,
        footprint_growth_se=growth_se,
        lifecycle_state=state,
        run_start_at=run_start,
        passes=passes,
    )
