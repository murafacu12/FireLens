"""Fire perimeter: the outline of an event's detected footprint.

The footprint (clustering.estimate_area_km2) is the union of the sensor pixels that saw fire, on a
125 m grid. Drawn as is, it is a cloud of squares with gaps where no overpass caught the fire. The
perimeter closes gaps up to about two pixels wide (a morphological closing), fills the holes inside,
and traces the outer edges. It is the *outline of where active fire was detected*, at sensor
resolution: not a mapped burn scar (see the Sentinel-2 dNBR burn scar for that).

Computed on request from the event's detections; nothing is stored.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
from affine import Affine
from rasterio import features
from scipy import ndimage

from app.ingest.clustering import (
    AREA_CELL_KM,
    KM_PER_DEG_LAT,
    KM_PER_DEG_LON_EQ,
    NOMINAL_PIXEL_KM,
    _basis_pixels,
    lon_offset,
    wrap_lon,
)

MIN_PIXELS = 3  # fewer: the circle marker says as much
GAP_PIXELS = 2  # gaps up to this many sensor pixels wide are closed
MAX_GRID = 2500  # cells per side; larger fires use coarser cells


@dataclass(frozen=True)
class Perimeter:
    rings: list[list[tuple[float, float]]]  # outer rings, (lon, lat), closed (first == last)
    area_km2: float  # area inside the outline
    basis: str  # which sensor's pixels drew it (viirs_375m / modis_1km)
    gap_km: float  # widest gap that was closed


def _simplify(points: np.ndarray, tol: float) -> np.ndarray:
    """Douglas-Peucker on a closed ring (iterative): drops the grid's staircase steps."""
    n = len(points)
    if n <= 4:
        return points
    keep = np.zeros(n, dtype=bool)
    keep[0] = keep[-1] = True
    # split the ring at its farthest point from the start, so both halves are open polylines
    far = int(np.argmax(np.hypot(*(points - points[0]).T)))
    keep[far] = True
    stack = [(0, far), (far, n - 1)]
    while stack:
        a, b = stack.pop()
        if b - a < 2:
            continue
        p, q = points[a], points[b]
        seg = q - p
        length = math.hypot(*seg)
        mid = points[a + 1:b]
        if length == 0:
            d = np.hypot(*(mid - p).T)
        else:
            d = np.abs(seg[0] * (mid[:, 1] - p[1]) - seg[1] * (mid[:, 0] - p[0])) / length
        k = int(np.argmax(d))
        if d[k] > tol:
            idx = a + 1 + k
            keep[idx] = True
            stack += [(a, idx), (idx, b)]
    return points[keep]


def event_perimeter(members: Sequence, ref_lat: float, ref_lon: float) -> Perimeter | None:
    """The outline of the detected footprint, or None when there are too few pixels to draw one."""
    if len(members) < MIN_PIXELS:
        return None
    pixels, basis = _basis_pixels(members)
    if len(pixels) < MIN_PIXELS:
        return None
    pix_km = NOMINAL_PIXEL_KM[pixels[0].instrument]
    cos_lat = max(math.cos(math.radians(ref_lat)), 1e-3)
    kx = KM_PER_DEG_LON_EQ * cos_lat
    x = np.array([lon_offset(d.longitude, ref_lon) * kx for d in pixels])
    y = np.array([(d.latitude - ref_lat) * KM_PER_DEG_LAT for d in pixels])

    gap_km = GAP_PIXELS * pix_km
    pad_km = gap_km + pix_km
    x0, x1 = x.min() - pad_km, x.max() + pad_km
    y0, y1 = y.min() - pad_km, y.max() + pad_km
    cell = max(AREA_CELL_KM, max(x1 - x0, y1 - y0) / MAX_GRID)
    w, h = math.ceil((x1 - x0) / cell), math.ceil((y1 - y0) / cell)

    # burn each pixel's square: cells whose centre lies inside it (as the footprint area does)
    half = pix_km / 2
    mask = np.zeros((h, w), dtype=bool)
    c0 = np.ceil((x - half - x0) / cell - 0.5).astype(int)
    c1 = np.floor((x + half - x0) / cell - 0.5).astype(int)
    r0 = np.ceil((y1 - (y + half)) / cell - 0.5).astype(int)  # rows run north to south
    r1 = np.floor((y1 - (y - half)) / cell - 0.5).astype(int)
    for a, b, c, d in zip(r0, r1, c0, c1):
        mask[max(a, 0):b + 1, max(c, 0):d + 1] = True
    if not mask.any():
        return None

    radius = max(1, round(gap_km / 2 / cell))
    yy, xx = np.ogrid[-radius:radius + 1, -radius:radius + 1]
    disk = xx * xx + yy * yy <= radius * radius
    closed = ndimage.binary_closing(mask, structure=disk)
    filled = ndimage.binary_fill_holes(closed | mask)

    to_world = Affine(cell, 0, x0, 0, -cell, y1)
    rings: list[list[tuple[float, float]]] = []
    for geom, value in features.shapes(filled.astype(np.uint8), mask=filled, transform=to_world, connectivity=8):
        if not value:
            continue
        outer = np.asarray(geom["coordinates"][0], dtype=float)  # holes were filled: outer ring only
        outer = _simplify(outer, tol=cell * 0.75)
        if len(outer) < 4:
            continue
        rings.append([
            (round(wrap_lon(ref_lon + px / kx), 5), round(ref_lat + py / KM_PER_DEG_LAT, 5))
            for px, py in outer
        ])
    if not rings:
        return None
    rings.sort(key=len, reverse=True)
    return Perimeter(
        rings=rings,
        area_km2=round(float(filled.sum()) * cell * cell, 3),
        basis=basis,
        gap_km=round(gap_km, 3),
    )
