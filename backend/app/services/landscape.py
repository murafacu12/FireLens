"""What is burning: fire detections against land cover (ESA WorldCover 2021).

Each detection is classed by the WorldCover pixel (~150 m, see geodata) under its centre. A VIIRS
pixel is 375 m and a MODIS pixel 1 km, so on a mixed landscape the class is the one at the hottest
spot's best location estimate, not a statement about the whole pixel; over many detections the
shares are what matters. WorldCover is a 2021 map: land converted since then (new clearings,
which often burn) shows its 2021 class.
"""

from datetime import UTC, datetime

import numpy as np
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models import DetectionRow
from app.services import geodata
from app.services.coverage import WORLD, BBox

# what each class means for fire, in plain words
FIRE_MEANING = {
    "tree_cover": "forest and woodland fires (also land clearing)",
    "shrubland": "scrub fires",
    "grassland": "grass and pasture fires",
    "cropland": "mostly agricultural burning (stubble, residue)",
    "built_up": "fires in or next to towns, or industrial heat",
    "wetland": "wetland and peat fires",
    "mangroves": "mangrove fires",
    "bare": "sparse vegetation",
    "water": "on the shore or a sensor location error",
}


def _naive(dt: datetime) -> datetime:
    return dt.astimezone(UTC).replace(tzinfo=None) if dt.tzinfo else dt


def _lon_ranges(w: float, e: float) -> list[tuple[float, float]]:
    return [(w, e)] if w <= e else [(w, 180.0), (-180.0, e)]


def detections_by_landcover(session: Session, bbox: BBox | None, start: datetime, end: datetime, budget_s: float = 8.0) -> dict:
    """Detections of the area and period by land-cover class, per sensor. `coverage` is the share of
    detections whose land-cover tile was available (the rest are being downloaded)."""
    bbox = bbox or WORLD
    w, s, e, n = bbox
    stmt = select(DetectionRow.latitude, DetectionRow.longitude, DetectionRow.instrument).where(
        DetectionRow.acquired_at >= _naive(start), DetectionRow.acquired_at < _naive(end), DetectionRow.static_kind.is_(None))
    if bbox != WORLD:
        stmt = stmt.where(DetectionRow.latitude >= s, DetectionRow.latitude <= n,
                          or_(*[(DetectionRow.longitude >= a) & (DetectionRow.longitude <= b) for a, b in _lon_ranges(w, e)]))
    rows = session.connection().execute(stmt).all()
    out = {"bbox": list(bbox), "detections": len(rows), "coverage": 1.0, "classes": [],
           "source": "ESA WorldCover 2021 (10 m, read at ~150 m)"}
    if not rows:
        return out
    lat, lon, ins = (np.asarray(x) for x in zip(*rows))
    codes, ok = geodata.worldcover().sample(lat.astype(float), lon.astype(float), budget_s)
    out["coverage"] = round(float(ok.mean()), 4)
    codes, viirs = codes[ok], (ins == "VIIRS")[ok]
    total = len(codes)
    classes = []
    for code in np.unique(codes):
        m = codes == code
        name = geodata.LANDCOVER.get(int(code), "no_data")
        classes.append({
            "class": name, "label": geodata.LANDCOVER_LABEL[name], "meaning": FIRE_MEANING.get(name),
            "detections": int(m.sum()), "share": round(float(m.sum()) / total, 4) if total else 0.0,
            "viirs": int((m & viirs).sum()), "modis": int((m & ~viirs).sum()),
        })
    out["classes"] = sorted(classes, key=lambda c: -c["detections"])
    return out
