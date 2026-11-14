"""The chord-distance test in cluster_detections must link exactly the pairs the haversine rule links.

Edge cases: pairs 1 m inside / outside the link distance, at the equator, mid and high latitudes,
across the antimeridian and next to the poles, for both edges (E1 contiguity, E2 continuity),
plus random clouds checked against an all-pairs brute force (which also validates the grid index).
"""

import math
import random
from datetime import UTC, datetime, timedelta

import pytest

from app.ingest.clustering import EARTH_RADIUS_KM, cluster_detections, footprint_radius_km, haversine_km
from app.schemas import FireDetection
from benchmarks import scenarios as sc
from benchmarks.reference import cluster_bruteforce, cluster_haversine_grid, partition

T0 = datetime(2026, 8, 1, tzinfo=UTC)
P = (1.0, 24.0, 72.0, 0.0)


def det(i, lat, lon, hours=0.0, scan=0.375, track=0.375, ins="VIIRS"):
    return FireDetection(
        id=f"e{i:05d}", source="x", instrument=ins, satellite="NOAA-20" if ins == "VIIRS" else "Terra", processing_level="NRT",
        latitude=lat, longitude=lon, acquired_at=T0 + timedelta(hours=hours), frp_mw=5.0, scan_km=scan, track_km=track,
        pixel_area_km2=scan * track, nominal_resolution_m=375, confidence_raw="n", confidence_pct=55, confidence_level="nominal",
    )


def destination(lat, lon, bearing_deg, km):
    """Point `km` away along a great circle (spherical, same radius as the clustering)."""
    d = km / EARTH_RADIUS_KM
    p1, l1, b = math.radians(lat), math.radians(lon), math.radians(bearing_deg)
    p2 = math.asin(math.sin(p1) * math.cos(d) + math.cos(p1) * math.sin(d) * math.cos(b))
    l2 = l1 + math.atan2(math.sin(b) * math.sin(d) * math.cos(p1), math.cos(d) - math.sin(p1) * math.sin(p2))
    return math.degrees(p2), (math.degrees(l2) + 540) % 360 - 180


PLACES = [(0.0, 20.0), (-34.6, -58.4), (65.0, 25.0), (0.0, 179.9995), (52.0, -179.9998), (89.95, 10.0), (-89.95, -120.0)]


@pytest.mark.parametrize("lat,lon", PLACES)
@pytest.mark.parametrize("bearing", [0, 45, 90, 135])
@pytest.mark.parametrize("hours,tol", [(6.0, 1.0), (48.0, 0.0)])  # E1 within 24 h, E2 within 72 h (pixels overlap)
def test_link_threshold_plus_minus_one_metre(lat, lon, bearing, hours, tol):
    a = det(0, lat, lon)
    reach = 2 * footprint_radius_km(a) + tol
    for delta_km, linked in ((-0.001, True), (0.001, False)):
        b_lat, b_lon = destination(lat, lon, bearing, reach + delta_km)
        b = det(1, b_lat, b_lon, hours=hours)
        assert (haversine_km(lat, lon, b_lat, b_lon) <= reach) is linked  # the reference agrees with the geometry
        groups = cluster_detections([a, b], *P)
        assert (len(groups) == 1) is linked, (lat, lon, bearing, hours, delta_km)


def test_antimeridian_neighbours_link():
    a, b = det(0, -17.0, 179.999), det(1, -17.0, -179.999)  # ~213 m apart across 180 degrees
    assert len(cluster_detections([a, b], *P)) == 1


@pytest.mark.parametrize("seed", range(6))
def test_random_clouds_match_bruteforce(seed):
    rng = random.Random(seed)
    lat0, lon0 = rng.choice(PLACES)
    dets = []
    for i in range(160):
        b_lat, b_lon = destination(lat0, lon0, rng.uniform(0, 360), abs(rng.gauss(0, 4)))
        modis = rng.random() < 0.3
        dets.append(det(i, b_lat, b_lon, hours=rng.uniform(0, 120), ins="MODIS" if modis else "VIIRS",
                        scan=rng.uniform(1, 4.8) if modis else rng.uniform(0.375, 0.8),
                        track=rng.uniform(1, 2.0) if modis else rng.uniform(0.375, 0.78)))
    for params in ((1.0, 24.0, 0.0, 0.0), P, (2.0, 12.0, 96.0, 0.5)):
        assert partition(cluster_detections(dets, *params)) == set(cluster_bruteforce(dets, *params))


@pytest.mark.parametrize("scenario", sc.all_scenarios(), ids=lambda s: s.name)
def test_synthetic_scenarios_match_both_references(scenario):
    dets = scenario.detections
    for params in ((1.0, 24.0, 0.0, 0.0), P):
        chord = partition(cluster_detections(dets, *params))
        assert chord == set(cluster_bruteforce(dets, *params)) == set(cluster_haversine_grid(dets, *params))
