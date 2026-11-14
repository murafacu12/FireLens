"""Controlled synthetic fire scenarios with ground truth by construction.

These are NOT real fires. They exercise the clustering rules on situations whose answer is known
because we built them (one or several fires, exact positions and times, realistic sensor pixels and
geolocation jitter). Metrics computed on them are reported separately from real-data metrics.

Where the "truth" is a modelling assumption rather than a fact (e.g. that a fire re-detected on the
same pixels after 60 h is the same fire), the scenario says so in `assumption`.
"""

import math
import random
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from app.schemas import FireDetection

T0 = datetime(2026, 8, 1, 3, 0, tzinfo=UTC)
NOMINAL = {"VIIRS": 0.375, "MODIS": 1.0}
SATELLITE_INSTRUMENT = {"NOAA-20": "VIIRS", "NOAA-21": "VIIRS", "Suomi NPP": "VIIRS", "Aqua": "MODIS", "Terra": "MODIS"}
JITTER_KM = {"VIIRS": 0.12, "MODIS": 0.35}  # 1-sigma geolocation error used for the synthetic pixels


@dataclass
class Scenario:
    name: str
    description: str
    detections: list[FireDetection] = field(default_factory=list)
    truth: dict[str, str] = field(default_factory=dict)  # detection id -> true fire label
    assumption: str = ""
    static_ids: set[str] = field(default_factory=set)  # detections that are not fires (e.g. a gas flare)


class _Builder:
    def __init__(self, name: str, seed: int) -> None:
        self.rng = random.Random(seed)
        self.n = 0
        self.name = name
        self.dets: list[FireDetection] = []
        self.truth: dict[str, str] = {}

    def pixel(self, fire: str, lat: float, lon: float, hours: float, satellite: str, frp: float = 20.0,
              scan: float | None = None, track: float | None = None, conf: str = "nominal") -> FireDetection:
        ins = SATELLITE_INSTRUMENT[satellite]
        j = JITTER_KM[ins]
        lat += self.rng.gauss(0, j) / 110.574
        lon = (lon + self.rng.gauss(0, j) / (111.320 * math.cos(math.radians(lat))) + 180) % 360 - 180
        s = scan or NOMINAL[ins]
        t = track or NOMINAL[ins]
        self.n += 1
        pct = {"low": 20, "nominal": 55, "high": 90}[conf]
        d = FireDetection(
            id=f"{self.name}-{self.n:04d}", source="SYNTHETIC", instrument=ins, satellite=satellite, processing_level="NRT",
            latitude=round(lat, 5), longitude=round(lon, 5), acquired_at=T0 + timedelta(hours=hours),
            daynight="D", brightness_k=330.0, brightness_secondary_k=295.0, frp_mw=round(frp, 1),
            scan_km=s, track_km=t, pixel_area_km2=round(s * t, 4), nominal_resolution_m=int(NOMINAL[ins] * 1000),
            confidence_raw=conf[0] if ins == "VIIRS" else str(pct), confidence_pct=pct, confidence_level=conf,
        )
        self.dets.append(d)
        self.truth[d.id] = fire
        return d

    def front(self, fire: str, lat: float, lon: float, hours: float, satellite: str, n: int, spacing_km: float | None = None,
              frp: float = 20.0, **kw) -> None:
        """n pixels along a north-south line (a fire front) centred on lat, lon."""
        ins = SATELLITE_INSTRUMENT[satellite]
        step = (spacing_km or NOMINAL[ins]) / 110.574
        for k in range(n):
            self.pixel(fire, lat + (k - (n - 1) / 2) * step, lon, hours, satellite, frp * (0.6 + 0.8 * self.rng.random()), **kw)


def km_east(lat: float, lon: float, km: float) -> float:
    return (lon + km / (111.320 * math.cos(math.radians(lat))) + 180) % 360 - 180


def _scenario(name, description, b: _Builder, assumption="", static_ids=None) -> Scenario:
    return Scenario(name, description, b.dets, b.truth, assumption, static_ids or set())


def multi_satellite(lat=-10.0, lon=-55.0) -> Scenario:
    b = _Builder("s1", 1)
    for day in range(2):
        h = 24 * day
        b.front("A", lat, lon, h + 0.0, "NOAA-20", 5)
        b.front("A", lat, lon, h + 0.8, "Suomi NPP", 5)
        b.front("A", lat, lon, h - 0.8 + 24 * (day == 0), "NOAA-21", 4)
        b.front("A", lat, lon, h + 0.1, "Aqua", 2)
        b.front("A", lat, lon, h - 3.0 + 24 * (day == 0), "Terra", 2)
        b.front("A", lat, lon, h + 12.0, "NOAA-20", 3)
    return _scenario("1_multi_satellite", "One fire seen by all five satellites over two days", b)


def close_fires_viirs(lat=-10.0, lon=-55.0) -> Scenario:
    b = _Builder("s2", 2)
    other = km_east(lat, lon, 3.0)
    for h in (0, 12, 24, 36):
        b.front("A", lat, lon, h, "NOAA-20", 4)
        b.front("B", lat, other, h + 0.5, "Suomi NPP", 4)
    return _scenario("2_close_fires_viirs", "Two independent fires 3 km apart, seen by VIIRS", b)


def close_fires_modis_edge(lat=-10.0, lon=-55.0) -> Scenario:
    b = _Builder("s2b", 3)
    other = km_east(lat, lon, 4.0)
    for h in (0, 24):
        b.pixel("A", lat, lon, h, "Terra", scan=4.0, track=1.8)
        b.pixel("B", lat, other, h, "Terra", scan=4.0, track=1.8)
    return _scenario("2b_close_fires_modis_swath_edge",
                     "Two fires 4 km apart seen only by MODIS near the swath edge (4 x 1.8 km pixels)", b)


def new_burn_beside_old_scar(lat=-10.0, lon=-55.0) -> Scenario:
    b = _Builder("s2c", 4)
    for h in (0, 12, 24):
        b.front("A", lat, lon, h, "NOAA-20", 3)
    other = km_east(lat, lon, 1.5)
    for h in (72, 84):
        b.front("B", lat, other, h, "NOAA-20", 3)
    return _scenario("2c_new_burn_beside_old_scar",
                     "A fire burns out; 48 h later a separate burn starts 1.5 km away", b)


def expanding(lat=-10.0, lon=-55.0, name="3_expanding") -> Scenario:
    b = _Builder("s3" + name[-4:], 5)
    for k in range(8):  # front moves 1.2 km east and lengthens every 12 h
        h = 12 * k
        b.front("A", lat, km_east(lat, lon, 1.2 * k), h, "NOAA-20" if k % 2 == 0 else "Suomi NPP", 3 + k, frp=15 + 10 * k)
    return _scenario(name, "One fire whose front advances ~2.4 km/day for 4 days", b)


def intermittent(lat=-10.0, lon=-55.0) -> Scenario:
    b = _Builder("s4", 6)
    for h in (0, 12, 42, 82):  # 30 h and 40 h without detections (cloud, missed passes)
        b.front("A", lat, lon, h, "NOAA-20", 3)
    return _scenario("4_intermittent", "One fire on the same pixels, unseen for 30 h then 40 h", b,
                     assumption="detections on the same pixels after 30-40 h belong to the same fire")


def reactivation(lat=-10.0, lon=-55.0) -> Scenario:
    b = _Builder("s5", 7)
    for h in (0, 12, 24):
        b.front("A", lat, lon, h, "NOAA-20", 4)
    for h in (84, 96):  # quiet 60 h, then burning again on the same ground
        b.front("A", lat, lon, h, "NOAA-20", 3)
    return _scenario("5_reactivation", "A fire goes quiet for 60 h and flares up again on the same pixels", b,
                     assumption="a flare-up on the same pixels within 72 h is the same fire, reactivated")


def isolated(lat=-10.0, lon=-55.0) -> Scenario:
    b = _Builder("s6", 8)
    for k in range(15):
        b.pixel(f"I{k}", lat + 0.5 * (k // 5), km_east(lat, lon, 50 * (k % 5)), 7 * k, "NOAA-21", frp=3)
    return _scenario("6_isolated", "15 single detections 50 km apart", b)


def merging_fronts(lat=-10.0, lon=-55.0) -> Scenario:
    b = _Builder("s8", 9)
    for k in range(5):  # two fronts 6 km apart close in on each other by 1.5 km per 12 h each
        h = 12 * k
        b.front("A", lat, km_east(lat, lon, 0.75 * k), h, "NOAA-20", 4)
        b.front("A", lat, km_east(lat, lon, 6.0 - 0.75 * k), h + 0.3, "NOAA-20", 4)
    return _scenario("8_merging_fronts", "Two fronts that burn into each other on day 2", b,
                     assumption="once joined, both fronts are one fire complex")


def split_by_flare(lat=-10.0, lon=-55.0) -> Scenario:
    b = _Builder("s9", 10)
    for h in (0, 12):
        b.front("A", lat, lon, h, "NOAA-20", 3)
        b.front("B", lat, km_east(lat, lon, 2.4), h, "NOAA-20", 3)  # 2.4 km: separate on their own
    bridge = b.pixel("FLARE", lat, km_east(lat, lon, 1.2), 6, "NOAA-20")  # links A and B until flagged
    return _scenario("9_split_by_flare", "Two fires bridged by one pixel that turns out to be a gas flare", b,
                     static_ids={bridge.id})


def all_scenarios() -> list[Scenario]:
    return [
        multi_satellite(), close_fires_viirs(), close_fires_modis_edge(), new_burn_beside_old_scar(),
        expanding(), intermittent(), reactivation(), isolated(), merging_fronts(), split_by_flare(),
        expanding(65.0, 25.0, name="10_expanding_at_65N"),
        expanding(-17.0, 179.97, name="11_expanding_across_180"),
    ]


# ------------------------------------------------------------------ metrics


def pairwise_scores(truth: dict[str, str], predicted: dict[str, str]) -> dict:
    """Pairwise precision/recall over detections, plus false merges/splits counted on clusters.

    precision = share of predicted same-event pairs that are truly the same fire
    recall    = share of true same-fire pairs that were put in the same event
    false merges = predicted events that contain more than one true fire
    false splits = true fires spread over more than one predicted event
    """
    ids = sorted(truth)
    tp = fp = fn = 0
    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            a, b = ids[i], ids[j]
            same_t = truth[a] == truth[b]
            same_p = predicted[a] == predicted[b]
            tp += same_t and same_p
            fp += same_p and not same_t
            fn += same_t and not same_p
    by_pred: dict[str, set] = {}
    by_true: dict[str, set] = {}
    for d in ids:
        by_pred.setdefault(predicted[d], set()).add(truth[d])
        by_true.setdefault(truth[d], set()).add(predicted[d])
    return {
        "events": len(by_pred),
        "true_fires": len(by_true),
        "precision": round(tp / (tp + fp), 3) if tp + fp else 1.0,
        "recall": round(tp / (tp + fn), 3) if tp + fn else 1.0,
        "false_merges": sum(1 for s in by_pred.values() if len(s) > 1),
        "false_splits": sum(1 for s in by_true.values() if len(s) > 1),
    }
