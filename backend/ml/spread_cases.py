"""Training/test cases for the learned fire-spread model, from FireLens's own history.

    python -m ml.spread_cases            -> data/training/spread/cases.json

A case is one fire at one moment T (a VIIRS pass):
* front    its VIIRS detections in the 12 h before T,
* history  its VIIRS detections 12-36 h before T (to see which way it was moving),
* targets  its VIIRS detections in the 24 h after T on NEW ground (> 0.75 km from every detection
           of the fire up to T): where it actually spread.
Several moments per fire (at most MAX_PER_EVENT, at least 12 h apart). Reads production read-only.
"""

import json
import logging
import sqlite3
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np

from ml.backtest_spread import NEW_GROUND_KM, ROOT, km

log = logging.getLogger("ml.spread_cases")
OUT = ROOT / "data" / "training" / "spread"
MAX_PER_EVENT = 5
MIN_TARGETS = 3
FRONT_H, HISTORY_H, AHEAD_H = 12, 36, 24


def main() -> None:
    db = sqlite3.connect(f"file:{(ROOT / 'firelens.db').as_posix()}?mode=ro", uri=True)
    now = datetime.now(UTC).timestamp()
    rows = db.execute("""select d.event_id, d.latitude, d.longitude, d.acquired_at from detections d
                         join fire_events e on e.id = d.event_id
                         where d.instrument = 'VIIRS' and d.static_kind is null and e.viirs_count >= 10 and e.pass_count >= 3
                         order by d.event_id, d.acquired_at""")
    by_event: dict[str, list] = defaultdict(list)
    for eid, lat, lon, t in rows:
        by_event[eid].append((lat, lon, datetime.fromisoformat(t).replace(tzinfo=UTC).timestamp()))
    log.info("%d events with enough VIIRS detections", len(by_event))
    cases = []
    for eid, dets in by_event.items():
        a = np.array(dets)
        lat, lon, t = a[:, 0], a[:, 1], a[:, 2]
        last_T, n = -1e18, 0
        for T in np.unique(t):
            if n >= MAX_PER_EVENT or T + AHEAD_H * 3600 > now - 6 * 3600:
                break
            if T - last_T < 12 * 3600:
                continue
            front = (t > T - FRONT_H * 3600) & (t <= T)
            nxt = (t > T) & (t <= T + AHEAD_H * 3600)
            if front.sum() < 1 or nxt.sum() < MIN_TARGETS:
                continue
            prior = t <= T
            d = km(lat[nxt], lon[nxt], lat[prior], lon[prior]).min(axis=1)
            new = np.flatnonzero(nxt)[d > NEW_GROUND_KM]
            if len(new) < MIN_TARGETS:
                continue
            hist = (t > T - HISTORY_H * 3600) & (t <= T - FRONT_H * 3600)
            cases.append({
                "event_id": eid, "T": float(T),
                "front": [[float(lat[i]), float(lon[i])] for i in np.flatnonzero(front)],
                "history": [[float(lat[i]), float(lon[i])] for i in np.flatnonzero(hist)],
                "prior": [[float(lat[i]), float(lon[i])] for i in np.flatnonzero(prior & ~front & ~hist)][-2000:],
                "targets": [[float(lat[i]), float(lon[i])] for i in new],
            })
            last_T, n = T, n + 1
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "cases.json").write_text(json.dumps(cases))
    days = {datetime.fromtimestamp(c["T"], UTC).date() for c in cases}
    log.info("%d cases from %d fires over %d days -> %s", len(cases), len({c['event_id'] for c in cases}), len(days), OUT / "cases.json")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s")
    main()
