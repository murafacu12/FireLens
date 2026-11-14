"""Group harmonized detections (any sensor) into fire events.

Two detections belong to the same event when their pixel footprints are within
`tolerance_km` of touching and they were observed at most `max_gap_hours` apart.
Membership is transitive (union-find), so a fire that moves or grows over days stays
one event. A space-time grid keeps neighbour search close to linear.
"""

import hashlib
import math
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.schemas import FireDetection

KM_PER_DEG_LAT = 110.574
KM_PER_DEG_LON_EQ = 111.320
AREA_CELL_KM = 0.25
EARTH_RADIUS_KM = 6371.0088


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(a)))


def footprint_radius_km(d: FireDetection) -> float:
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


def cluster_detections(
    detections: Sequence[FireDetection], tolerance_km: float = 1.0, max_gap_hours: float = 24.0
) -> list[list[FireDetection]]:
    n = len(detections)
    if n == 0:
        return []
    radii = [footprint_radius_km(d) for d in detections]
    times = [d.acquired_at.timestamp() for d in detections]
    max_link_km = 2 * max(radii) + tolerance_km
    cell_deg = max_link_km / KM_PER_DEG_LAT
    gap_s = max_gap_hours * 3600
    grid: dict[tuple[int, int, int], list[int]] = defaultdict(list)
    uf = _UnionFind(n)

    for i, d in enumerate(detections):
        gy = math.floor(d.latitude / cell_deg)
        gx = math.floor(d.longitude / cell_deg)
        gt = math.floor(times[i] / gap_s)
        # Longitude cells shrink toward the poles: widen the search accordingly.
        cos_lat = max(math.cos(math.radians(min(abs(d.latitude) + cell_deg, 89.9))), 1e-3)
        kx = min(math.ceil(KM_PER_DEG_LAT / (KM_PER_DEG_LON_EQ * cos_lat)), 180)
        for dy in (-1, 0, 1):
            for dx in range(-kx, kx + 1):
                for dt in (-1, 0, 1):
                    for j in grid.get((gy + dy, gx + dx, gt + dt), ()):
                        if abs(times[j] - times[i]) > gap_s or uf.find(i) == uf.find(j):
                            continue
                        o = detections[j]
                        if haversine_km(d.latitude, d.longitude, o.latitude, o.longitude) <= (
                            radii[i] + radii[j] + tolerance_km
                        ):
                            uf.union(i, j)
        grid[(gy, gx, gt)].append(i)

    groups: dict[int, list[FireDetection]] = defaultdict(list)
    for i, d in enumerate(detections):
        groups[uf.find(i)].append(d)
    return list(groups.values())


def estimate_area_km2(members: Sequence[FireDetection], ref_lat: float) -> float:
    """Union of pixel footprints on a 250 m grid, using the finest sensor available.

    VIIRS (375 m) footprints are used whenever the event has VIIRS detections, so a fire
    seen by both sensors is not inflated by MODIS' 1 km+ pixels or double counted.
    This is the *active-fire footprint* over the event's life, not burned area.
    """
    viirs = [d for d in members if d.instrument == "VIIRS"]
    pixels = viirs or list(members)
    c = AREA_CELL_KM
    cos_lat = max(math.cos(math.radians(ref_lat)), 1e-3)
    cells: set[tuple[int, int]] = set()
    for d in pixels:
        x = d.longitude * KM_PER_DEG_LON_EQ * cos_lat
        y = d.latitude * KM_PER_DEG_LAT
        hx, hy = d.scan_km / 2, d.track_km / 2
        i0, i1 = math.ceil((x - hx) / c - 0.5), math.floor((x + hx) / c - 0.5)
        j0, j1 = math.ceil((y - hy) / c - 0.5), math.floor((y + hy) / c - 0.5)
        if i1 < i0:
            i0 = i1 = math.floor(x / c)
        if j1 < j0:
            j0 = j1 = math.floor(y / c)
        for i in range(i0, i1 + 1):
            for j in range(j0, j1 + 1):
                cells.add((i, j))
    return round(len(cells) * c * c, 3)


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
    total_frp_mw: float
    area_km2: float
    max_confidence_pct: int
    status: str
    member_ids: list[str]


def build_event(members: Sequence[FireDetection], now: datetime, active_hours: float = 24.0) -> EventDraft:
    ordered = sorted(members, key=lambda d: (d.acquired_at, d.id))
    frps = [d.frp_mw or 0.0 for d in ordered]
    total_frp = sum(frps)
    if total_frp > 0:  # FRP-weighted centroid sits on the most intense part of the fire
        lat = sum(d.latitude * w for d, w in zip(ordered, frps)) / total_frp
        lon = sum(d.longitude * w for d, w in zip(ordered, frps)) / total_frp
    else:
        lat = sum(d.latitude for d in ordered) / len(ordered)
        lon = sum(d.longitude for d in ordered) / len(ordered)
    end_at = ordered[-1].acquired_at
    return EventDraft(
        id="evt_" + hashlib.sha1(ordered[0].id.encode()).hexdigest()[:12],
        latitude=round(lat, 5),
        longitude=round(lon, 5),
        west=min(d.longitude for d in ordered),
        south=min(d.latitude for d in ordered),
        east=max(d.longitude for d in ordered),
        north=max(d.latitude for d in ordered),
        start_at=ordered[0].acquired_at,
        end_at=end_at,
        detection_count=len(ordered),
        modis_count=sum(1 for d in ordered if d.instrument == "MODIS"),
        viirs_count=sum(1 for d in ordered if d.instrument == "VIIRS"),
        max_frp_mw=round(max(frps), 2),
        total_frp_mw=round(total_frp, 2),
        area_km2=estimate_area_km2(ordered, lat),
        max_confidence_pct=max(d.confidence_pct for d in ordered),
        status="active" if now - end_at <= timedelta(hours=active_hours) else "inactive",
        member_ids=[d.id for d in ordered],
    )
