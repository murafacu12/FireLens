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

import numpy as np
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components
from scipy.spatial import cKDTree
from sqlalchemy import Integer, cast, func, or_, select
from sqlalchemy.orm import Session

from app.ingest.clustering import EARTH_RADIUS_KM
from app.models import DetectionRow, EventRow
from app.services.coverage import WORLD, BBox

SCENE_KM = 50.0  # the other sensor imaged this place if it detected anything this close...
SCENE_CELL_DEG = 0.1  # ...to the centre of the ~11 km cell holding the detection
DEFAULT_WINDOW_MIN = 120  # ...within this many minutes
MATCH_TOLERANCE_KM = 1.0  # footprints within this distance of touching = same fire (event engine rule)
MAX_ROWS = 400_000
MAX_POINTS = 1500  # FRP scatter points returned (MODIS vs VIIRS only; the baseline returns none)
CONFIDENCE = ("high", "nominal", "low")


# ---------------------------------------------------------------------------- geometry
# Distances are tested as chords between points on the sphere: the chord is a monotonic function of
# the great-circle distance (chord = 2R sin(d / 2R)), so "chord <= chord(L)" selects exactly the
# pairs with haversine distance <= L, as the event engine does.


def _xyz(lat: np.ndarray, lon: np.ndarray) -> np.ndarray:
    p, l = np.radians(lat), np.radians(lon)
    c = np.cos(p)
    return EARTH_RADIUS_KM * np.column_stack([c * np.cos(l), c * np.sin(l), np.sin(p)])


def _chord(km):
    return 2 * EARTH_RADIUS_KM * np.sin(np.minimum(np.asarray(km, dtype=float) / (2 * EARTH_RADIUS_KM), np.pi / 2))


class Dets:
    """Detections as column arrays (one row per detection)."""

    def __init__(self, sat: np.ndarray, lat: np.ndarray, lon: np.ndarray, t: np.ndarray, radius: np.ndarray,
                 frp: np.ndarray, conf: np.ndarray):
        self.sat, self.lat, self.lon, self.t, self.radius, self.frp, self.conf = sat, lat, lon, t, radius, frp, conf
        self.xyz = _xyz(lat, lon) if len(lat) else np.empty((0, 3))

    def __len__(self) -> int:
        return len(self.t)

    def take(self, mask: np.ndarray) -> "Dets":
        return Dets(self.sat[mask], self.lat[mask], self.lon[mask], self.t[mask], self.radius[mask], self.frp[mask], self.conf[mask])


_EMPTY = (np.empty(0, dtype=np.intp), np.empty(0, dtype=np.intp), np.empty(0))


def _pairs(a_xyz: np.ndarray, a_t: np.ndarray, b_xyz: np.ndarray, b_t: np.ndarray, max_km: float, window_s: float):
    """Every (i, j) with |t_a[i] - t_b[j]| <= window_s and great-circle distance <= max_km, and its chord.

    Time is a 4th coordinate scaled so that the window has the length of the distance limit: a pair
    meeting both limits is then within sqrt(2) x that length in 4-D, so a KD-tree ball query on it
    returns every qualifying pair (plus a few extra that the exact tests below remove)."""
    if not len(a_t) or not len(b_t):
        return _EMPTY
    c = float(_chord(max_km))
    t_ref = min(a_t.min(), b_t.min())
    scale = c / window_s
    pa = np.column_stack([a_xyz, (a_t - t_ref) * scale])
    pb = np.column_stack([b_xyz, (b_t - t_ref) * scale])
    m = cKDTree(pa).sparse_distance_matrix(cKDTree(pb), c * math.sqrt(2) + 1e-6, output_type="ndarray")
    ia, ib = m["i"].astype(np.intp), m["j"].astype(np.intp)
    chord = np.linalg.norm(a_xyz[ia] - b_xyz[ib], axis=1)
    ok = (np.abs(a_t[ia] - b_t[ib]) <= window_s) & (chord <= c)
    return ia[ok], ib[ok], chord[ok]


def _in_scene(d: Dets, other: Dets, window_s: float) -> np.ndarray:
    """Whether the other sensor imaged each detection's place at that time. A scene is a place, not
    a pixel: every detection in the same SCENE_CELL_DEG cell at the same time shares one answer,
    measured from the cell centre (cost scales with places, not pixels)."""
    if not len(d) or not len(other):
        return np.zeros(len(d), dtype=bool)
    cy = np.floor(d.lat / SCENE_CELL_DEG).astype(np.int64) + 1000
    cx = np.floor(d.lon / SCENE_CELL_DEG).astype(np.int64) + 2000
    # one int64 per (cell, time): 12 bits lat cell, 13 bits lon cell, 38 bits of whole seconds
    key = (((cy << 13) | cx) << 38) | (d.t.astype(np.int64) - int(d.t.min()))
    first, inv = np.unique(key, return_index=True, return_inverse=True)[1:]
    lat_c = (np.floor(d.lat[first] / SCENE_CELL_DEG) + 0.5) * SCENE_CELL_DEG
    lon_c = (np.floor(d.lon[first] / SCENE_CELL_DEG) + 0.5) * SCENE_CELL_DEG
    hit_places, _, _ = _pairs(_xyz(lat_c, lon_c), d.t[first], other.xyz, other.t, SCENE_KM, window_s)
    hit = np.zeros(len(first), dtype=bool)
    hit[hit_places] = True
    return hit[inv]


def _matches(d: Dets, rows: np.ndarray, other: Dets, window_s: float):
    """(i, j): detection rows[...] of d whose footprint is within link distance of other[j]
    (r_i + r_j + MATCH_TOLERANCE_KM, the event engine's pixel-touching rule) inside the window."""
    if not len(rows) or not len(other):
        return _EMPTY[:2]
    max_link = float(d.radius[rows].max() + other.radius.max() + MATCH_TOLERANCE_KM)
    ia, ib, chord = _pairs(d.xyz[rows], d.t[rows], other.xyz, other.t, max_link, window_s)
    i = rows[ia]
    ok = chord <= _chord(d.radius[i] + other.radius[ib] + MATCH_TOLERANCE_KM)
    return i[ok], ib[ok]


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


def load(session: Session, bbox: BBox, t0: datetime, t1: datetime) -> tuple[Dets, np.ndarray]:
    """Detections in the area and period, and the instrument of each."""
    # epoch seconds computed by SQLite: parsing hundreds of thousands of datetimes in Python cost more
    # than the whole comparison (FIRMS times are whole minutes, so whole seconds lose nothing)
    epoch = cast(func.strftime("%s", DetectionRow.acquired_at), Integer)
    stmt = select(
        DetectionRow.instrument, DetectionRow.satellite, DetectionRow.latitude, DetectionRow.longitude, epoch,
        DetectionRow.scan_km, DetectionRow.track_km, func.coalesce(DetectionRow.frp_mw, 0.0), DetectionRow.confidence_level,
    ).where(DetectionRow.acquired_at >= t0, DetectionRow.acquired_at < t1, DetectionRow.static_kind.is_(None),
            _in_box(DetectionRow.latitude, DetectionRow.longitude, bbox))
    rows = session.connection().execute(stmt).all()
    ins, sat, lat, lon, when, scan, track, frp, conf = zip(*rows) if rows else ([],) * 9
    f = lambda xs: np.asarray(xs, dtype=float)  # noqa: E731
    t = f(when)
    # radius: half-diagonal of the pixel, as the event engine's footprint_radius_km
    return (Dets(np.asarray(sat, dtype=str), f(lat), f(lon), t, 0.5 * np.hypot(f(scan), f(track)), f(frp), np.asarray(conf, dtype=str)),
            np.asarray(ins, dtype=str))


PASS_GAP_S = 20 * 60  # FIRMS stamps each 5-6 min granule; one overpass = times less than this apart


def overpasses(d: Dets) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Overpass of each detection: its satellite's granules < PASS_GAP_S apart are one pass.
    Returns (pass id per detection, satellite of each pass, start time of each pass)."""
    pass_id = np.empty(len(d), dtype=np.int64)
    sats, starts = [], []
    for sat in np.unique(d.sat):
        idx = np.flatnonzero(d.sat == sat)
        order = idx[np.argsort(d.t[idx], kind="stable")]
        ts = d.t[order]
        new = np.ones(len(ts), dtype=bool)
        new[1:] = np.diff(ts) > PASS_GAP_S
        pass_id[order] = len(starts) + np.cumsum(new) - 1
        starts.extend(ts[new].tolist())
        sats.extend([str(sat)] * int(new.sum()))
    return pass_id, np.asarray(sats, dtype=str), np.asarray(starts, dtype=float)


def _median(xs) -> float | None:
    return round(float(statistics.median(xs)), 2) if len(xs) else None


def _side(label: str, d: Dets, subj: np.ndarray, other: Dets, window_s: float) -> tuple[dict, tuple[np.ndarray, np.ndarray]]:
    rows = np.flatnonzero(subj)
    scene = np.zeros(len(d), dtype=bool)
    scene[rows] = _in_scene(d.take(rows), other, window_s)
    i, j = _matches(d, np.flatnonzero(scene), other, window_s)
    matched = np.zeros(len(d), dtype=bool)
    matched[i] = True
    by_conf = {}
    for c in CONFIDENCE:
        sc = scene & (d.conf == c)
        n, mc = int(sc.sum()), int((sc & matched).sum())
        by_conf[c] = {"in_scenes": n, "matched": mc, "matched_pct": round(100 * mc / n, 1) if n else None}
    n_scene, n_matched = int(scene.sum()), int(matched.sum())
    return {
        "label": label, "detections": len(rows), "in_scenes": n_scene, "matched": n_matched,
        "matched_pct": round(100 * n_matched / n_scene, 1) if n_scene else None,
        "by_confidence": by_conf,
        "median_frp_matched_mw": _median(d.frp[matched]),
        "median_frp_unmatched_mw": _median(d.frp[scene & ~matched]),
        "by_satellite": dict(Counter(d.sat[scene].tolist())),
    }, (i, j)


def compare(a: Dets, a_subj: np.ndarray, b: Dets, b_subj: np.ndarray, window_min: float,
            label_a: str, label_b: str, max_points: int = MAX_POINTS) -> dict:
    """Coincident-scene comparison of two detection sets (see module docstring). a_subj / b_subj mark
    the detections inside the period; the others only act as partners."""
    window = window_min * 60
    side_a, (ia, ib) = _side(label_a, a, a_subj, b, window)  # every partner: needed for FRP clusters
    side_b, _ = _side(label_b, b, b_subj, a, window)

    # coincident fire clusters: per (pass of a, pass of b), connected groups of matched pixels
    clusters: list[dict] = []
    pass_pairs: dict[tuple, list[float]] = defaultdict(list)
    if len(ia):
        pass_a, sat_a, start_a = overpasses(a)
        pass_b, sat_b, start_b = overpasses(b)
        nb_pass = len(start_b)
        pp_keys, pp = np.unique(pass_a[ia] * nb_pass + pass_b[ib], return_inverse=True)  # pass pairs
        # graph nodes are (pass pair, pixel): a pixel matched in two pass pairs belongs to a cluster in each
        a_keys, a_node = np.unique(pp * len(a) + ia, return_inverse=True)
        b_keys, b_node = np.unique(pp * len(b) + ib, return_inverse=True)
        na = len(a_keys)
        n = na + len(b_keys)
        graph = coo_matrix((np.ones(len(ia)), (a_node, na + b_node)), shape=(n, n))
        k, comp = connected_components(graph, directed=False)
        ca, cb = comp[:na], comp[na:]
        ra, rb = a_keys % len(a), b_keys % len(b)  # detection rows

        def per(c, w):
            return np.bincount(c, weights=w, minlength=k)

        px_a, px_b = per(ca, None), per(cb, None)
        # FRP is reported to 0.01 MW: summed as whole hundredths it is exact (and independent of the
        # order of the pixels), then rounded half up to 0.1 MW
        frp_a = np.floor(per(ca, np.round(a.frp[ra] * 100)) / 10 + 0.5) / 10
        frp_b = np.floor(per(cb, np.round(b.frp[rb] * 100)) / 10 + 0.5) / 10
        t_a, t_b = per(ca, a.t[ra]) / px_a, per(cb, b.t[rb]) / px_b
        lat_a, lon_a = per(ca, a.lat[ra]) / px_a, per(ca, a.lon[ra]) / px_a
        comp_pp = np.empty(k, dtype=np.int64)
        comp_pp[ca] = pp_keys[a_keys // len(a)]
        cpa, cpb = comp_pp // nb_pass, comp_pp % nb_pass  # each cluster's pass of a and of b
        for c in range(k):
            clusters.append({
                "frp_a": round(float(frp_a[c]), 1), "frp_b": round(float(frp_b[c]), 1),
                "pixels_a": int(px_a[c]), "pixels_b": int(px_b[c]), "minutes_apart": round(abs(float(t_b[c] - t_a[c])) / 60),
                "sat_a": str(sat_a[cpa[c]]), "sat_b": str(sat_b[cpb[c]]),
                "lat": round(float(lat_a[c]), 3), "lon": round(float(lon_a[c]), 3),
            })
        for pa, pb in zip(*np.divmod(pp_keys, nb_pass)):
            pass_pairs[(str(sat_a[pa]), str(sat_b[pb]))].append(abs(float(start_b[pb] - start_a[pa])) / 60)
    ratios = [c["frp_b"] / c["frp_a"] for c in clusters if c["frp_a"] > 0 and c["frp_b"] > 0]
    q = statistics.quantiles(ratios, n=4) if len(ratios) >= 4 else None
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
            "points": sorted(clusters, key=lambda c: -(c["frp_a"] + c["frp_b"]))[:max_points],
        },
    }


def event_composition(session: Session, bbox: BBox, t0: datetime, t1: datetime) -> dict:
    """Fire events started in the period: which sensors saw them, and which saw them first."""
    where = (EventRow.start_at >= t0, EventRow.start_at < t1, _in_box(EventRow.latitude, EventRow.longitude, bbox))
    comp: Counter = Counter()
    for v, m in session.execute(select(EventRow.viirs_count, EventRow.modis_count).where(*where)):
        comp["both" if v and m else "viirs_only" if v else "modis_only"] += 1
    firsts: dict[str, dict[str, datetime]] = defaultdict(dict)
    # which sensor saw it first only means something for events both saw
    stmt = (select(DetectionRow.event_id, DetectionRow.instrument, func.min(DetectionRow.acquired_at))
            .join(EventRow, EventRow.id == DetectionRow.event_id)
            .where(*where, EventRow.viirs_count > 0, EventRow.modis_count > 0)
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
    dets, instrument = load(session, bbox, t0 - margin, t1 + margin)
    lo, hi = t0.replace(tzinfo=UTC).timestamp(), t1.replace(tzinfo=UTC).timestamp()
    inside = (dets.t >= lo) & (dets.t < hi)

    def split(mask: np.ndarray) -> tuple[Dets, np.ndarray]:
        return dets.take(mask), inside[mask]

    aqua, aqua_s = split((instrument == "MODIS") & (dets.sat != "Terra"))  # Terra never coincides with VIIRS
    viirs, viirs_s = split(instrument == "VIIRS")
    n20, n20_s = split(dets.sat == "NOAA-20")
    n21, n21_s = split(dets.sat == "NOAA-21")
    return {
        "bbox": list(bbox), "start": t0.replace(tzinfo=UTC), "end": t1.replace(tzinfo=UTC),
        "window_minutes": window_min, "scene_km": SCENE_KM, "match_tolerance_km": MATCH_TOLERANCE_KM,
        "terra_detections_excluded": int(((dets.sat == "Terra") & inside).sum()),
        "modis_vs_viirs": compare(aqua, aqua_s, viirs, viirs_s, window_min, "MODIS (Aqua)", "VIIRS"),
        "baseline_viirs_vs_viirs": compare(n20, n20_s, n21, n21_s, window_min, "VIIRS NOAA-20", "VIIRS NOAA-21", max_points=0)
        if n20_s.any() and n21_s.any() else None,
        "events": event_composition(session, bbox, t0, t1),
    }
