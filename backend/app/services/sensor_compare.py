"""MODIS vs VIIRS: how the two sensors see the same fires.

FireLens stores detections, not swath footprints, so "VIIRS saw a fire that MODIS missed" could
simply mean MODIS wasn't imaging that place then. The comparison therefore only uses **coincident
scenes**: a detection counts only if the other sensor demonstrably imaged the same place within
`window` (it has at least one detection within SCENE_KM of the centre of the 0.1 deg cell holding the
detection). Inside a scene, a detection is **matched**
when the other sensor has a detection whose footprint is within link distance (the event engine's
pixel-touching rule: r_a + r_b + MATCH_TOLERANCE_KM).

Two facts shape the reading (measured on FireLens data, Nov 2026):
* Aqua has drifted to ~15:00-16:00 local solar time while the VIIRS satellites cross at ~13:30, so
  coincident MODIS/VIIRS views are typically ~100 min apart (73 % within 120 min). Fires change in
  that time: an unmatched detection reflects sensor differences *and* fire evolution.
* Terra (~08:30 / 20:30) never coincides with VIIRS (~13:30 / 01:30). It is left out of the scene
  comparison and still counts at the fire-event level.

To separate the two effects the same procedure runs on two identical sensors, VIIRS NOAA-20 vs
NOAA-21 (~50 min apart), as a **baseline**: their disagreement comes from time and geolocation only.

FRP is compared per **coincident fire cluster**: the matched pixels of one pass of each sensor,
joined into connected groups. Each side's FRP is summed over its pixels of that single pass (an
instantaneous whole-cluster fire power, comparable between sensors; never summed over time).

Subjects are the detections inside the period; detections up to one window outside it act only as
partners, so fires near the period edges are not counted as unmatched.
"""

import math
import statistics
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from typing import NamedTuple

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.ingest.clustering import KM_PER_DEG_SPHERE, _UnionFind, footprint_radius_km, haversine_km, lon_columns
from app.models import DetectionRow, EventRow
from app.services.coverage import WORLD, BBox

SCENE_KM = 50.0  # the other sensor imaged this place if it detected anything this close...
SCENE_CELL_DEG = 0.1  # ...to the centre of the ~11 km cell holding the detection
DEFAULT_WINDOW_MIN = 120  # ...within this many minutes
MATCH_TOLERANCE_KM = 1.0  # footprints within this distance of touching = same fire (event engine rule)
MAX_ROWS = 400_000
MAX_POINTS = 1500  # FRP scatter points returned
CONFIDENCE = ("high", "nominal", "low")


class Det(NamedTuple):
    id: str
    instrument: str
    satellite: str
    latitude: float
    longitude: float
    t: float  # epoch seconds (UTC)
    scan_km: float
    track_km: float
    frp: float
    confidence: str


class _Index:
    """Space-time hash: lat cells, wrapping lon cells, time buckets of `window` seconds."""

    def __init__(self, dets: list[Det], radius_km: float, window_s: float):
        self.radius_km = radius_km
        self.window = window_s
        self.bucket = window_s  # finer buckets were measured slower (more lookups than they save)
        self.cell = max(radius_km / KM_PER_DEG_SPHERE, 1e-3)
        self.n_lon = max(1, math.floor(360 / self.cell))
        self.cell_lon = 360 / self.n_lon
        self.grid: dict[tuple, list[Det]] = defaultdict(list)
        for d in dets:
            self.grid[self._key(d.latitude, d.longitude, d.t)].append(d)

    def _key(self, lat: float, lon: float, t: float) -> tuple[int, int, int]:
        return (math.floor(lat / self.cell), math.floor((lon + 180) / self.cell_lon) % self.n_lon,
                math.floor(t / self.bucket))

    def near(self, d: Det):
        """Candidates within the window of d and possibly within radius_km (callers test the distance)."""
        gy, gx, gt = self._key(d.latitude, d.longitude, d.t)
        cols = lon_columns(d.latitude, gx, self.radius_km, self.cell, self.cell_lon, self.n_lon)
        for dt in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for cx in cols:
                    for o in self.grid.get((gy + dy, cx, gt + dt), ()):
                        if abs(o.t - d.t) <= self.window:
                            yield o


def _lon_ranges(w: float, e: float) -> list[tuple[float, float]]:
    return [(w, e)] if w <= e else [(w, 180.0), (-180.0, e)]


def _in_box(col_lat, col_lon, bbox: BBox):
    w, s, e, n = bbox
    return (col_lat >= s) & (col_lat <= n) & or_(*[(col_lon >= a) & (col_lon <= b) for a, b in _lon_ranges(w, e)])


def _naive_utc(dt: datetime) -> datetime:
    return dt.astimezone(UTC).replace(tzinfo=None) if dt.tzinfo else dt


def count_rows(session: Session, bbox: BBox, t0: datetime, t1: datetime) -> int:
    return session.scalar(select(func.count()).select_from(DetectionRow).where(
        DetectionRow.acquired_at >= t0, DetectionRow.acquired_at < t1, DetectionRow.static_kind.is_(None),
        _in_box(DetectionRow.latitude, DetectionRow.longitude, bbox))) or 0


def load(session: Session, bbox: BBox, t0: datetime, t1: datetime) -> list[Det]:
    stmt = select(
        DetectionRow.id, DetectionRow.instrument, DetectionRow.satellite, DetectionRow.latitude, DetectionRow.longitude,
        DetectionRow.acquired_at, DetectionRow.scan_km, DetectionRow.track_km, DetectionRow.frp_mw, DetectionRow.confidence_level,
    ).where(DetectionRow.acquired_at >= t0, DetectionRow.acquired_at < t1, DetectionRow.static_kind.is_(None),
            _in_box(DetectionRow.latitude, DetectionRow.longitude, bbox))
    return [Det(r[0], r[1], r[2], r[3], r[4], r[5].replace(tzinfo=UTC).timestamp(), r[6], r[7], r[8] or 0.0, r[9])
            for r in session.execute(stmt)]


PASS_GAP_S = 20 * 60  # FIRMS stamps each 5-6 min granule; one overpass = times less than this apart


def overpasses(dets: list[Det]) -> dict[tuple[str, float], float]:
    """(satellite, granule time) -> start time of its overpass (granules < PASS_GAP_S apart joined)."""
    out: dict[tuple[str, float], float] = {}
    by_sat: dict[str, set[float]] = defaultdict(set)
    for d in dets:
        by_sat[d.satellite].add(d.t)
    for sat, times in by_sat.items():
        start = prev = None
        for t in sorted(times):
            if prev is None or t - prev > PASS_GAP_S:
                start = t
            out[(sat, t)] = start
            prev = t
    return out


def _median(xs) -> float | None:
    return round(statistics.median(xs), 2) if xs else None


def compare(a_subj: list[Det], a_all: list[Det], b_subj: list[Det], b_all: list[Det], window_min: float,
            label_a: str, label_b: str) -> dict:
    """Coincident-scene comparison of two detection sets (see module docstring)."""
    window = window_min * 60
    pass_of = overpasses(a_all + b_all)
    radii = [footprint_radius_km(d) for d in a_all + b_all]
    max_link = 2 * max(radii or [1.0]) + MATCH_TOLERANCE_KM
    scene_idx = {"a": _Index(a_all, SCENE_KM, window), "b": _Index(b_all, SCENE_KM, window)}
    link_idx = {"a": _Index(a_all, max_link, window), "b": _Index(b_all, max_link, window)}

    cache: dict[tuple, bool] = {}

    def in_scene(d: Det, other: str) -> bool:
        # A scene is a place, not a pixel: every detection in the same SCENE_CELL_DEG cell at the
        # same time shares one answer, measured from the cell centre (cost scales with places).
        cy, cx = math.floor(d.latitude / SCENE_CELL_DEG), math.floor(d.longitude / SCENE_CELL_DEG)
        key = (other, cy, cx, d.t)
        if key not in cache:
            lat, lon = (cy + 0.5) * SCENE_CELL_DEG, (cx + 0.5) * SCENE_CELL_DEG
            centre = d._replace(latitude=lat, longitude=lon)
            cache[key] = any(haversine_km(lat, lon, o.latitude, o.longitude) <= SCENE_KM for o in scene_idx[other].near(centre))
        return cache[key]

    def partners(d: Det, other: str, first_only: bool) -> list[Det]:
        rd = footprint_radius_km(d)
        out = []
        for o in link_idx[other].near(d):
            if haversine_km(d.latitude, d.longitude, o.latitude, o.longitude) <= rd + footprint_radius_km(o) + MATCH_TOLERANCE_KM:
                out.append(o)
                if first_only:
                    break
        return out

    def side(label: str, subj: list[Det], other: str, first_only: bool) -> tuple[dict, list[tuple[Det, list[Det]]]]:
        scene = [d for d in subj if in_scene(d, other)]
        found = [(d, partners(d, other, first_only)) for d in scene]
        matched = {d.id for d, p in found if p}
        by_conf = {}
        for c in CONFIDENCE:
            sc = [d for d in scene if d.confidence == c]
            mc = sum(1 for d in sc if d.id in matched)
            by_conf[c] = {"in_scenes": len(sc), "matched": mc, "matched_pct": round(100 * mc / len(sc), 1) if sc else None}
        return {
            "label": label, "detections": len(subj), "in_scenes": len(scene), "matched": len(matched),
            "matched_pct": round(100 * len(matched) / len(scene), 1) if scene else None,
            "by_confidence": by_conf,
            "median_frp_matched_mw": _median([d.frp for d in scene if d.id in matched]),
            "median_frp_unmatched_mw": _median([d.frp for d in scene if d.id not in matched]),
            "by_satellite": dict(Counter(d.satellite for d in scene)),
        }, found

    side_a, found_a = side(label_a, a_subj, "b", first_only=False)  # every partner: needed for FRP clusters
    side_b, _ = side(label_b, b_subj, "a", first_only=True)  # only whether one exists

    # coincident fire clusters: per (pass of a, pass of b), connected groups of matched pixels
    by_passes: dict[tuple, list[tuple[Det, Det]]] = defaultdict(list)
    for d, ps in found_a:
        for o in ps:
            by_passes[((d.satellite, pass_of[(d.satellite, d.t)]), (o.satellite, pass_of[(o.satellite, o.t)]))].append((d, o))
    clusters = []
    for (pa, pb), plist in by_passes.items():
        nodes: dict[tuple, Det] = {}
        for p, q in plist:
            nodes.setdefault(("a", p.id), p)
            nodes.setdefault(("b", q.id), q)
        keys = list(nodes)
        pos = {k: i for i, k in enumerate(keys)}
        uf = _UnionFind(len(keys))
        for p, q in plist:
            uf.union(pos[("a", p.id)], pos[("b", q.id)])
        groups: dict[int, list] = defaultdict(list)
        for k in keys:
            groups[uf.find(pos[k])].append(k)
        for g in groups.values():
            da = [nodes[k] for k in g if k[0] == "a"]
            db = [nodes[k] for k in g if k[0] == "b"]
            clusters.append({
                "frp_a": round(sum(d.frp for d in da), 1), "frp_b": round(sum(d.frp for d in db), 1),
                "pixels_a": len(da), "pixels_b": len(db), "minutes_apart": round(abs(sum(d.t for d in db) / len(db) - sum(d.t for d in da) / len(da)) / 60),
                "sat_a": pa[0], "sat_b": pb[0],
                "lat": round(sum(d.latitude for d in da) / len(da), 3), "lon": round(sum(d.longitude for d in da) / len(da), 3),
            })
    ratios = [c["frp_b"] / c["frp_a"] for c in clusters if c["frp_a"] > 0 and c["frp_b"] > 0]
    q = statistics.quantiles(ratios, n=4) if len(ratios) >= 4 else None
    pass_pairs: dict[tuple, list[float]] = defaultdict(list)
    for pa, pb in by_passes:
        pass_pairs[(pa[0], pb[0])].append(abs(pb[1] - pa[1]) / 60)
    return {
        "a": side_a,
        "b": side_b,
        "satellite_pairs": [{"a": sa, "b": sb, "coincident_pass_pairs": len(v), "median_minutes_apart": round(statistics.median(v))}
                            for (sa, sb), v in sorted(pass_pairs.items(), key=lambda kv: -len(kv[1]))],
        "clusters": {
            "count": len(clusters),
            "median_frp_ratio_b_over_a": _median(ratios),
            "ratio_p25": round(q[0], 2) if q else None, "ratio_p75": round(q[2], 2) if q else None,
            "median_minutes_apart": _median([c["minutes_apart"] for c in clusters]),
            "points": sorted(clusters, key=lambda c: -(c["frp_a"] + c["frp_b"]))[:MAX_POINTS],
        },
    }


def event_composition(session: Session, bbox: BBox, t0: datetime, t1: datetime) -> dict:
    """Fire events started in the period: which sensors saw them, and which saw them first."""
    where = (EventRow.start_at >= t0, EventRow.start_at < t1, _in_box(EventRow.latitude, EventRow.longitude, bbox))
    comp: Counter = Counter()
    for v, m in session.execute(select(EventRow.viirs_count, EventRow.modis_count).where(*where)):
        comp["both" if v and m else "viirs_only" if v else "modis_only"] += 1
    firsts: dict[str, dict[str, datetime]] = defaultdict(dict)
    stmt = (select(DetectionRow.event_id, DetectionRow.instrument, func.min(DetectionRow.acquired_at))
            .join(EventRow, EventRow.id == DetectionRow.event_id).where(*where)
            .group_by(DetectionRow.event_id, DetectionRow.instrument))
    for eid, ins, t in session.execute(stmt):
        firsts[eid][ins] = t
    lags, first = [], Counter()
    for f in firsts.values():
        if "VIIRS" in f and "MODIS" in f:
            lag = (f["MODIS"] - f["VIIRS"]).total_seconds() / 3600
            lags.append(lag)
            first["viirs" if lag > 0 else "modis" if lag < 0 else "same_time"] += 1
    return {
        "started": sum(comp.values()), "both": comp["both"], "viirs_only": comp["viirs_only"], "modis_only": comp["modis_only"],
        "first_seen_by": dict(first), "median_modis_minus_viirs_hours": _median(lags),
    }


def analyze(session: Session, bbox: BBox | None, start: datetime, end: datetime, window_min: float = DEFAULT_WINDOW_MIN) -> dict:
    bbox = bbox or WORLD
    t0, t1 = _naive_utc(start), _naive_utc(end)
    margin = timedelta(minutes=window_min)
    dets = load(session, bbox, t0 - margin, t1 + margin)
    lo, hi = t0.replace(tzinfo=UTC).timestamp(), t1.replace(tzinfo=UTC).timestamp()

    def split(pred) -> tuple[list[Det], list[Det]]:
        every = [d for d in dets if pred(d)]
        return [d for d in every if lo <= d.t < hi], every

    aqua_s, aqua_all = split(lambda d: d.instrument == "MODIS" and d.satellite != "Terra")
    viirs_s, viirs_all = split(lambda d: d.instrument == "VIIRS")
    n20_s, n20_all = split(lambda d: d.satellite == "NOAA-20")
    n21_s, n21_all = split(lambda d: d.satellite == "NOAA-21")
    return {
        "bbox": list(bbox), "start": t0.replace(tzinfo=UTC), "end": t1.replace(tzinfo=UTC),
        "window_minutes": window_min, "scene_km": SCENE_KM, "match_tolerance_km": MATCH_TOLERANCE_KM,
        "terra_detections_excluded": sum(1 for d in dets if d.satellite == "Terra" and lo <= d.t < hi),
        "modis_vs_viirs": compare(aqua_s, aqua_all, viirs_s, viirs_all, window_min, "MODIS (Aqua)", "VIIRS"),
        "baseline_viirs_vs_viirs": compare(n20_s, n20_all, n21_s, n21_all, window_min, "VIIRS NOAA-20", "VIIRS NOAA-21")
        if n20_s and n21_s else None,
        "events": event_composition(session, bbox, t0, t1),
    }
