"""Reference implementations of the event linking rule, for equivalence checks only.

* `cluster_bruteforce`: every pair, haversine distance. No index, so it validates the grid search
  itself (antimeridian, poles, cell sizes). O(n^2): small sets only.
* `cluster_haversine_grid`: the production grid search with the original haversine predicate
  instead of the chord test. It validates the chord test on large real datasets.

Both must produce exactly the same partition as app.ingest.clustering.cluster_detections.
"""

import math
from collections import defaultdict

from app.ingest.clustering import (
    KM_PER_DEG_SPHERE,
    _UnionFind,
    footprint_radius_km,
    haversine_km,
    lon_columns,
)


def _linked(d, o, ri, rj, apart, gap_s, cont_s, tol, ctol) -> bool:
    if apart > cont_s:
        return False
    reach = ri + rj + (tol if apart <= gap_s else ctol)
    return haversine_km(d.latitude, d.longitude, o.latitude, o.longitude) <= reach


def _groups(dets, uf) -> list[frozenset]:
    g = defaultdict(set)
    for i, d in enumerate(dets):
        g[uf.find(i)].add(d.id)
    return [frozenset(v) for v in g.values()]


def cluster_bruteforce(dets, tolerance_km=1.0, max_gap_hours=24.0, continuity_gap_hours=0.0, continuity_tolerance_km=0.0):
    n = len(dets)
    radii = [footprint_radius_km(d) for d in dets]
    times = [d.acquired_at.timestamp() for d in dets]
    gap_s = max_gap_hours * 3600
    cont_s = max(continuity_gap_hours * 3600, gap_s)
    uf = _UnionFind(n)
    for i in range(n):
        for j in range(i + 1, n):
            if _linked(dets[i], dets[j], radii[i], radii[j], abs(times[i] - times[j]), gap_s, cont_s,
                       tolerance_km, continuity_tolerance_km):
                uf.union(i, j)
    return _groups(dets, uf)


def cluster_haversine_grid(dets, tolerance_km=1.0, max_gap_hours=24.0, continuity_gap_hours=0.0, continuity_tolerance_km=0.0):
    n = len(dets)
    if not n:
        return []
    radii = [footprint_radius_km(d) for d in dets]
    times = [d.acquired_at.timestamp() for d in dets]
    gap_s = max_gap_hours * 3600
    cont_s = max(continuity_gap_hours * 3600, gap_s)
    max_link = 2 * max(radii) + max(tolerance_km, continuity_tolerance_km if cont_s > gap_s else 0.0)
    cell = max_link / KM_PER_DEG_SPHERE
    n_lon = max(1, math.floor(360 / cell))
    cell_lon = 360 / n_lon
    kt = math.ceil(cont_s / gap_s)
    grid = defaultdict(list)
    uf = _UnionFind(n)
    for i, d in enumerate(dets):
        gy, gx, gt = math.floor(d.latitude / cell), math.floor((d.longitude + 180) / cell_lon) % n_lon, math.floor(times[i] / gap_s)
        cols = lon_columns(d.latitude, gx, max_link, cell, cell_lon, n_lon)
        for dt in range(-kt, kt + 1):
            for dy in (-1, 0, 1):
                for cx in cols:
                    for j in grid.get((gy + dy, cx, gt + dt), ()):
                        if _linked(d, dets[j], radii[i], radii[j], abs(times[j] - times[i]), gap_s, cont_s,
                                   tolerance_km, continuity_tolerance_km):
                            uf.union(i, j)
        grid[(gy, gx, gt)].append(i)
    return _groups(dets, uf)


def partition(groups) -> set[frozenset]:
    return {g if isinstance(g, frozenset) else frozenset(d.id for d in g) for g in groups}
