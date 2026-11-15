"""The vectorized coincident-scene matching against a brute-force haversine reference."""

import math
import random

import numpy as np
import pytest

from app.ingest.clustering import haversine_km
from app.services import sensor_compare as sc


def random_dets(rng: random.Random, n: int, centres: list[tuple[float, float]]) -> sc.Dets:
    rows = []
    for _ in range(n):
        lat0, lon0 = rng.choice(centres)
        lat = max(-89.99, min(89.99, lat0 + rng.gauss(0, 0.15)))
        lon = (lon0 + rng.gauss(0, 0.15) + 180) % 360 - 180
        scan, track = rng.choice([(0.375, 0.375), (0.6, 0.5), (1.0, 1.0), (2.5, 1.4)])
        rows.append((rng.choice(["Aqua", "NOAA-20"]), lat, lon, 1_800_000_000 + 60 * rng.randrange(0, 600),
                     0.5 * math.hypot(scan, track), round(rng.uniform(0.5, 80), 2), rng.choice(sc.CONFIDENCE)))
    sat, lat, lon, t, r, frp, conf = map(list, zip(*rows))
    f = lambda xs: np.asarray(xs, dtype=float)  # noqa: E731
    return sc.Dets(np.asarray(sat), f(lat), f(lon), f(t), f(r), f(frp), np.asarray(conf))


# fires in ordinary places, across the antimeridian and next to a pole
CENTRES = [(-10.0, -55.0), (-10.05, -54.95), (65.0, 179.95), (65.02, -179.97), (89.9, 20.0), (89.9, -160.0)]


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_matches_equal_brute_force(seed):
    rng = random.Random(seed)
    a, b = random_dets(rng, 300, CENTRES), random_dets(rng, 400, CENTRES)
    window = 120 * 60
    i, j = sc._matches(a, np.arange(len(a)), b, window)
    got = set(zip(i.tolist(), j.tolist()))
    want = {(p, q) for p in range(len(a)) for q in range(len(b))
            if abs(a.t[p] - b.t[q]) <= window
            and haversine_km(a.lat[p], a.lon[p], b.lat[q], b.lon[q]) <= a.radius[p] + b.radius[q] + sc.MATCH_TOLERANCE_KM}
    assert want and got == want


@pytest.mark.parametrize("seed", [4, 5])
def test_scene_equals_brute_force(seed):
    rng = random.Random(seed)
    a, b = random_dets(rng, 300, CENTRES), random_dets(rng, 60, CENTRES[:3])
    window = 60 * 60
    got = sc._in_scene(a, b, window)
    want = []
    for p in range(len(a)):
        clat = (math.floor(a.lat[p] / sc.SCENE_CELL_DEG) + 0.5) * sc.SCENE_CELL_DEG
        clon = (math.floor(a.lon[p] / sc.SCENE_CELL_DEG) + 0.5) * sc.SCENE_CELL_DEG
        want.append(any(abs(a.t[p] - b.t[q]) <= window and haversine_km(clat, clon, b.lat[q], b.lon[q]) <= sc.SCENE_KM
                        for q in range(len(b))))
    assert any(want) and not all(want) and got.tolist() == want


def test_empty_inputs():
    rng = random.Random(0)
    a = random_dets(rng, 10, CENTRES)
    empty = a.take(np.zeros(len(a), dtype=bool))
    assert len(sc._matches(a, np.arange(len(a)), empty, 3600)[0]) == 0
    assert not sc._in_scene(a, empty, 3600).any()
    assert sc.compare(empty, np.zeros(0, bool), a, np.ones(len(a), bool), 120, "x", "y")["clusters"]["count"] == 0
