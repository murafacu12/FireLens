"""Static ground data: land cover (ESA WorldCover 2021) and elevation (Copernicus DEM GLO-90).

Both are read from their public cloud-optimized GeoTIFFs and kept in a local cache
(data/geo/, compressed .npz), so each tile is downloaded once:

* ESA WorldCover 2021 v200, 10 m, 3 x 3 deg tiles (s3://esa-worldcover, CC BY 4.0). FireLens
  reads the built-in 1/16 overview (~150 m pixels): close to a VIIRS pixel (375 m), 1.6 s and
  ~1 MB per tile instead of ~100 MB, with the same class shares as the full resolution
  (measured on the Sierras de Cordoba tile: within 0.6 percentage points per class).
* Copernicus DEM GLO-90, 90 m, 1 x 1 deg tiles (s3://copernicus-dem-90m, Copernicus licence).
  Ocean tiles do not exist: they read as sea level.

Requests never wait on many downloads: `ensure` fetches what fits in a time budget and queues the
rest for a background thread; callers report how much of the area was available.
"""

import logging
import math
import os
import queue
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

GDAL_ENV = dict(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR", CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif",
                GDAL_HTTP_MULTIRANGE="YES", GDAL_HTTP_TIMEOUT="60", GDAL_HTTP_MAX_RETRY="3", GDAL_HTTP_RETRY_DELAY="2")

# ESA WorldCover classes -> FireLens names (and what they mean for fire)
LANDCOVER = {
    10: "tree_cover", 20: "shrubland", 30: "grassland", 40: "cropland", 50: "built_up", 60: "bare",
    70: "snow_ice", 80: "water", 90: "wetland", 95: "mangroves", 100: "moss_lichen", 0: "no_data",
}
LANDCOVER_LABEL = {
    "tree_cover": "Forest / tree cover", "shrubland": "Shrubland", "grassland": "Grassland", "cropland": "Cropland",
    "built_up": "Built-up", "bare": "Bare / sparse", "snow_ice": "Snow and ice", "water": "Water", "wetland": "Wetland",
    "mangroves": "Mangroves", "moss_lichen": "Moss and lichen", "no_data": "No data",
}


def default_dir() -> Path:
    return Path(__file__).resolve().parents[3] / "data" / "geo"


@dataclass(frozen=True)
class Tile:
    """A cached raster: values plus the position of its north-west corner and pixel size (deg)."""
    data: np.ndarray
    west: float
    north: float
    dx: float
    dy: float

    def lookup(self, lat: np.ndarray, lon: np.ndarray) -> np.ndarray:
        r = np.clip(((self.north - lat) / self.dy).astype(np.int64), 0, self.data.shape[0] - 1)
        c = np.clip(((lon - self.west) / self.dx).astype(np.int64), 0, self.data.shape[1] - 1)
        return self.data[r, c]


class TileSource:
    """One public tiled raster with a local cache. Subclasses name the tiles and say how to read them."""

    name = "raster"
    tile_deg = 1
    fill = 0  # value of a tile that does not exist (open ocean)
    pixel_deg = 1 / 1200  # size of a cached pixel

    def __init__(self, cache_dir: Path):
        self.dir = cache_dir / self.name
        self.dir.mkdir(parents=True, exist_ok=True)
        self._mem: OrderedDict[tuple[int, int], Tile | None] = OrderedDict()
        self._lock = threading.Lock()
        self._fetching: dict[tuple[int, int], threading.Lock] = {}
        self._queue: queue.Queue = queue.Queue()
        self._worker: threading.Thread | None = None

    # -- naming
    def key(self, lat: float, lon: float) -> tuple[int, int]:
        d = self.tile_deg
        return (math.floor(lat / d) * d, math.floor(min(lon, 179.999999) / d) * d)

    def url(self, key: tuple[int, int]) -> str:  # pragma: no cover - per source
        raise NotImplementedError

    def read_remote(self, key: tuple[int, int]) -> Tile | None:
        import rasterio
        from rasterio.errors import RasterioIOError

        try:
            with rasterio.Env(**GDAL_ENV), rasterio.open("/vsicurl/" + self.url(key)) as ds:
                data, transform = self._read(ds)
        except RasterioIOError as exc:
            if "404" in str(exc) or "not recognized" in str(exc) or "No such file" in str(exc):
                return None  # no tile: open ocean
            raise
        return Tile(data, transform[2], transform[5], transform[0], -transform[4])

    def _read(self, ds):  # pragma: no cover - per source
        return ds.read(1), ds.transform

    # -- cache
    def _path(self, key) -> Path:
        return self.dir / f"{key[0]}_{key[1]}.npz"

    def cached(self, key) -> Tile | None | bool:
        """The tile, None (known to be ocean) or False (not downloaded yet)."""
        with self._lock:
            if key in self._mem:
                self._mem.move_to_end(key)
                return self._mem[key]
        p = self._path(key)
        if not p.is_file():
            return False
        try:
            with np.load(p) as z:  # closed at once: another process may replace the file
                t = None if z["absent"] else Tile(z["data"], float(z["west"]), float(z["north"]), float(z["dx"]), float(z["dy"]))
        except (OSError, ValueError, KeyError):  # being written by another process right now
            return False
        self._remember(key, t)
        return t

    MEM_TILES = 40  # ~5 MB each in memory (WorldCover at 1/16); worldwide queries re-read from disk

    def _remember(self, key, t) -> None:
        with self._lock:
            self._mem[key] = t
            self._mem.move_to_end(key)
            while len(self._mem) > self.MEM_TILES:
                self._mem.popitem(last=False)

    def fetch(self, key) -> Tile | None:
        with self._lock:
            lock = self._fetching.setdefault(key, threading.Lock())
        with lock:
            hit = self.cached(key)
            if hit is not False:
                return hit
            t = self.read_remote(key)
            tmp = self._path(key).with_name(f"{key[0]}_{key[1]}.{os.getpid()}.{threading.get_ident()}.tmp.npz")
            if t is None:
                np.savez_compressed(tmp, absent=True)
            else:
                np.savez_compressed(tmp, absent=False, data=t.data, west=t.west, north=t.north, dx=t.dx, dy=t.dy)
            try:
                tmp.replace(self._path(key))
            except PermissionError:  # another process wrote the same tile and is reading it
                tmp.unlink(missing_ok=True)
            self._remember(key, t)
            return t

    def ensure(self, keys, budget_s: float = 8.0) -> set:
        """Download missing tiles until the time budget is spent; queue the rest. Returns the keys
        that are available now."""
        ready, t0 = set(), time.monotonic()
        for k in keys:
            if self.cached(k) is not False:
                ready.add(k)
            elif time.monotonic() - t0 < budget_s:
                try:
                    self.fetch(k)
                    ready.add(k)
                except Exception as exc:  # network trouble: try again later in the background
                    log.warning("%s tile %s: %s", self.name, k, exc)
                    self._enqueue(k)
            else:
                self._enqueue(k)
        return ready

    def _enqueue(self, key) -> None:
        self._queue.put(key)
        if self._worker is None or not self._worker.is_alive():
            self._worker = threading.Thread(target=self._drain, name=f"{self.name}-tiles", daemon=True)
            self._worker.start()

    def _drain(self) -> None:
        while True:
            try:
                key = self._queue.get(timeout=30)
            except queue.Empty:
                return
            try:
                self.fetch(key)
            except Exception as exc:
                log.warning("%s tile %s failed in background: %s", self.name, key, exc)

    # -- reading
    def sample(self, lat: np.ndarray, lon: np.ndarray, budget_s: float = 8.0) -> tuple[np.ndarray, np.ndarray]:
        """Values at points, and whether each point's tile was available."""
        lat, lon = np.asarray(lat, dtype=float), np.asarray(lon, dtype=float)
        out = np.full(len(lat), self.fill, dtype=np.int32)
        ok = np.zeros(len(lat), dtype=bool)
        if not len(lat):
            return out, ok
        d = self.tile_deg
        ky = (np.floor(lat / d) * d).astype(np.int64)
        kx = (np.floor(np.minimum(lon, 179.999999) / d) * d).astype(np.int64)
        uniq, inv, counts = np.unique(np.column_stack([ky, kx]), axis=0, return_inverse=True, return_counts=True)
        inv = inv.ravel()
        order = np.argsort(-counts, kind="stable")  # the tiles with most points first
        ready = self.ensure([(int(uniq[i, 0]), int(uniq[i, 1])) for i in order], budget_s)
        by_tile = np.split(np.argsort(inv, kind="stable"), np.cumsum(counts)[:-1])
        for i, rows in enumerate(by_tile):
            k = (int(uniq[i, 0]), int(uniq[i, 1]))
            if k not in ready:
                continue
            t = self.cached(k)
            if t:
                out[rows] = t.lookup(lat[rows], lon[rows])
            ok[rows] = True  # an absent tile is known: open ocean
        return out, ok

    def window(self, west: float, south: float, east: float, north: float, step: float, budget_s: float = 20.0):
        """A regular grid (rows north->south) at `step` deg over the box, from the cached tiles;
        also the share of the grid whose tiles were available."""
        lats = np.arange(north - step / 2, south, -step)
        lons = np.arange(west + step / 2, east, step)
        gl, gm = np.meshgrid(lats, lons, indexing="ij")
        vals, ok = self.sample(gl.ravel(), gm.ravel(), budget_s)
        return vals.reshape(gl.shape), float(ok.mean()) if ok.size else 0.0, lats, lons


class WorldCover(TileSource):
    name = "worldcover"
    tile_deg = 3
    fill = 80  # missing tile: open ocean
    OVERVIEW = 16
    pixel_deg = 3 / (36000 / 16)  # ~150 m

    def url(self, key):
        lat, lon = key
        tag = f"{'N' if lat >= 0 else 'S'}{abs(lat):02d}{'E' if lon >= 0 else 'W'}{abs(lon):03d}"
        return f"https://esa-worldcover.s3.eu-central-1.amazonaws.com/v200/2021/map/ESA_WorldCover_10m_2021_v200_{tag}_Map.tif"

    def _read(self, ds):
        from rasterio.enums import Resampling

        f = self.OVERVIEW
        data = ds.read(1, out_shape=(ds.height // f, ds.width // f), resampling=Resampling.nearest)
        t = ds.transform
        from rasterio import Affine

        return data.astype(np.uint8), Affine(t.a * f, t.b, t.c, t.d, t.e * f, t.f)


class CopernicusDem(TileSource):
    name = "dem90"
    tile_deg = 1
    fill = 0  # missing tile: sea level

    def url(self, key):
        lat, lon = key
        tag = f"{'N' if lat >= 0 else 'S'}{abs(lat):02d}_00_{'E' if lon >= 0 else 'W'}{abs(lon):03d}_00"
        return f"https://copernicus-dem-90m.s3.amazonaws.com/Copernicus_DSM_COG_30_{tag}_DEM/Copernicus_DSM_COG_30_{tag}_DEM.tif"

    def _read(self, ds):
        return np.round(ds.read(1)).astype(np.int16), ds.transform


class CopernicusDemCoarse(CopernicusDem):
    """The same DEM read at its 1/4 overview (~360 m): enough for slope at fire-spread scale, and
    ~10x smaller to cache (used where many fires need terrain, e.g. the learned spread model)."""
    name = "dem360"
    pixel_deg = 4 / 1200

    def _read(self, ds):
        from rasterio import Affine
        from rasterio.enums import Resampling

        f = 4
        data = ds.read(1, out_shape=(max(1, ds.height // f), max(1, ds.width // f)), resampling=Resampling.average)
        t = ds.transform
        return np.round(data).astype(np.int16), Affine(t.a * ds.width / data.shape[1], t.b, t.c, t.d, t.e * ds.height / data.shape[0], t.f)


_sources: dict[str, TileSource] = {}


def dem_coarse(cache_dir: Path | None = None) -> CopernicusDemCoarse:
    return _sources.setdefault("dem360", CopernicusDemCoarse(cache_dir or default_dir()))  # type: ignore[return-value]


def worldcover(cache_dir: Path | None = None) -> WorldCover:
    return _sources.setdefault("worldcover", WorldCover(cache_dir or default_dir()))  # type: ignore[return-value]


def dem(cache_dir: Path | None = None) -> CopernicusDem:
    return _sources.setdefault("dem90", CopernicusDem(cache_dir or default_dir()))  # type: ignore[return-value]


def landcover_shares(codes: np.ndarray, weights: np.ndarray | None = None) -> dict[str, float]:
    """{class name: share} of the given WorldCover codes (optionally weighted), largest first."""
    if not len(codes):
        return {}
    w = np.ones(len(codes)) if weights is None else np.asarray(weights, dtype=float)
    total = w.sum() or 1.0
    out = {LANDCOVER.get(int(c), "no_data"): float(w[codes == c].sum() / total) for c in np.unique(codes)}
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


def terrain(elev: np.ndarray, lats: np.ndarray, step_deg: float) -> tuple[np.ndarray, np.ndarray]:
    """Slope (degrees) and aspect (degrees clockwise from north, the direction the slope faces
    downhill) of an elevation grid whose rows run north -> south."""
    lat0 = float(np.mean(lats)) if len(lats) else 0.0
    dy_m = step_deg * 111_320.0
    dx_m = step_deg * 111_320.0 * max(math.cos(math.radians(lat0)), 0.05)
    z = elev.astype(float)
    dz_dn = -np.gradient(z, axis=0) / dy_m  # rows go south: flip to northward
    dz_de = np.gradient(z, axis=1) / dx_m
    slope = np.degrees(np.arctan(np.hypot(dz_de, dz_dn)))
    aspect = (np.degrees(np.arctan2(-dz_de, -dz_dn)) + 360) % 360  # downhill direction
    return slope, aspect
