"""Synthetic FIRMS data for development without a MAP_KEY.

Produces CSV text in the exact MODIS / VIIRS FIRMS column layout so demo data goes
through the same normalization and clustering code as real data. Clearly synthetic:
fires are placed around fixed hotspots with plausible (not real) values.
"""

import csv
import io
import math
import random
from datetime import datetime, timedelta

MODIS_COLUMNS = [
    "latitude", "longitude", "brightness", "scan", "track", "acq_date", "acq_time",
    "satellite", "instrument", "confidence", "version", "bright_t31", "frp", "daynight",
]
VIIRS_COLUMNS = [
    "latitude", "longitude", "bright_ti4", "scan", "track", "acq_date", "acq_time",
    "satellite", "instrument", "confidence", "version", "bright_ti5", "frp", "daynight",
]

# name, lat, lon, starts N days ago, duration days, max spread km, intensity (detections/overpass)
HOTSPOTS = [
    ("Sierras de Córdoba, AR", -31.35, -64.55, 12, 6, 14, 18),
    ("Delta del Paraná, AR", -33.05, -60.25, 25, 18, 25, 10),
    ("Corrientes wetlands, AR", -28.4, -57.6, 8, 8, 20, 12),
    ("Patagonia andina, AR", -42.0, -71.5, 4, 5, 10, 8),
    ("Rondônia, BR", -9.2, -62.8, 20, 20, 30, 22),
    ("Pantanal, BR", -18.0, -57.0, 15, 12, 35, 16),
    ("Chaco, PY", -21.5, -60.5, 6, 6, 18, 10),
    ("Northern California, US", 39.75, -121.6, 10, 9, 22, 20),
    ("Alberta, CA", 56.5, -112.0, 18, 14, 28, 14),
    ("New South Wales, AU", -33.6, 150.3, 3, 4, 15, 18),
    ("Sakha, RU", 62.0, 129.5, 22, 16, 40, 20),
    ("Central Portugal", 40.1, -8.1, 9, 3, 9, 12),
    ("Angola miombo", -12.5, 18.5, 28, 28, 60, 30),
    ("Kalimantan, ID", -1.8, 113.9, 14, 10, 20, 12),
]

# Approximate local solar overpass times (hours) per platform.
OVERPASSES = {
    ("MODIS", "T"): (10.5, 22.5),
    ("MODIS", "A"): (13.5, 1.5),
    ("VIIRS", "N"): (13.4, 1.4),
    ("VIIRS", "N20"): (12.6, 0.6),
    ("VIIRS", "N21"): (13.0, 1.0),
}
SOURCE_PLATFORMS = {
    "MODIS_NRT": [("MODIS", "T"), ("MODIS", "A")],
    "VIIRS_SNPP_NRT": [("VIIRS", "N")],
    "VIIRS_NOAA20_NRT": [("VIIRS", "N20")],
    "VIIRS_NOAA21_NRT": [("VIIRS", "N21")],
}


def _row(instrument: str, sat: str, lat: float, lon: float, when: datetime, rng: random.Random, frp: float):
    local_h = (when.hour + when.minute / 60 + lon / 15) % 24
    night = local_h < 6 or local_h >= 18
    if instrument == "MODIS":
        scan = round(rng.uniform(1.0, 4.4), 1)
        track = round(min(2.0, 0.9 + scan * 0.25), 1)
        conf = max(0, min(100, int(rng.gauss(68, 20))))
        return {
            "latitude": f"{lat:.4f}", "longitude": f"{lon:.4f}",
            "brightness": f"{rng.uniform(305, 420):.1f}", "scan": scan, "track": track,
            "acq_date": when.strftime("%Y-%m-%d"), "acq_time": when.strftime("%H%M").lstrip("0") or "0",
            "satellite": sat, "instrument": "MODIS", "confidence": conf, "version": "6.1NRT",
            "bright_t31": f"{rng.uniform(285, 310):.1f}", "frp": f"{frp:.1f}",
            "daynight": "N" if night else "D",
        }
    scan = round(rng.uniform(0.32, 0.75), 2)
    track = round(min(0.78, 0.33 + scan * 0.6), 2)
    ti4 = min(367.0, rng.uniform(300, 380))
    return {
        "latitude": f"{lat:.5f}", "longitude": f"{lon:.5f}",
        "bright_ti4": f"{ti4:.2f}", "scan": scan, "track": track,
        "acq_date": when.strftime("%Y-%m-%d"), "acq_time": when.strftime("%H%M").lstrip("0") or "0",
        "satellite": sat, "instrument": "VIIRS",
        "confidence": rng.choices(["l", "n", "h"], weights=[1, 6, 3])[0], "version": "2.0NRT",
        "bright_ti5": f"{rng.uniform(280, 305):.2f}", "frp": f"{frp / 4:.2f}",
        "daynight": "N" if night else "D",
    }


def generate_demo_csv(source: str, now: datetime, days: int = 30, seed: int = 42) -> str:
    platforms = SOURCE_PLATFORMS.get(source)
    if not platforms:
        raise ValueError(f"demo data not available for {source}")
    instrument = platforms[0][0]
    rng = random.Random(f"{seed}-{source}")
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=MODIS_COLUMNS if instrument == "MODIS" else VIIRS_COLUMNS)
    writer.writeheader()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)

    for name, hlat, hlon, start_ago, duration, spread_km, intensity in HOTSPOTS:
        if start_ago > days:
            continue
        hrng = random.Random(f"{seed}-{name}")  # same fire geometry for every sensor
        heading = hrng.uniform(0, 2 * math.pi)
        ignition = today - timedelta(days=start_ago)
        for day_i in range(min(duration, start_ago + 1)):
            day = ignition + timedelta(days=day_i)
            for inst, sat in platforms:
                for local_h in OVERPASSES[(inst, sat)]:
                    utc_h = (local_h - hlon / 15) % 24
                    when = day + timedelta(hours=utc_h + rng.uniform(-0.4, 0.4))
                    if when > now or when < ignition:
                        continue
                    progress = min((when - ignition).total_seconds() / (duration * 86400), 0.999)
                    growth = math.sin(math.pi * progress)  # ramps up then dies down
                    radius = max(0.5, spread_km * math.sqrt(progress))
                    n = int(intensity * growth * (4 if inst == "VIIRS" else 1) * rng.uniform(0.6, 1.2))
                    if local_h < 6:  # fewer night detections
                        n //= 2
                    # Active pixels sit along a contiguous fire front: an arc downwind of ignition.
                    width = 0.9
                    for k in range(n):
                        ang = heading - width + 2 * width * (k + rng.random()) / max(n, 1)
                        dist = radius + rng.gauss(0, 0.25)
                        dlat = (dist * math.cos(ang)) / 110.574
                        dlon = (dist * math.sin(ang)) / (111.32 * math.cos(math.radians(hlat)))
                        frp = rng.lognormvariate(math.log(25 + 60 * growth), 0.8)
                        writer.writerow(_row(inst, sat, hlat + dlat, hlon + dlon, when, rng, frp))

    # Scattered small agricultural burns across the main land masses.
    for _ in range(40 * days // 30):
        lat, lon = rng.uniform(-35, 50), rng.uniform(-100, 140)
        when = today - timedelta(days=rng.uniform(0, days - 1)) + timedelta(hours=rng.uniform(0, 24))
        inst, sat = rng.choice(platforms)
        if when > now:
            continue
        writer.writerow(_row(inst, sat, lat, lon, when, rng, rng.uniform(2, 20)))
    return out.getvalue()
