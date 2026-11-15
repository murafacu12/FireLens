"""Train and test the learned 24 h fire-spread model.

    python -m ml.train_spread          (after ml.spread_cases and ml.fetch_spread_weather)

* Time split: train on cases before TEST_FROM, test on the days from TEST_FROM (the model never
  sees the test days or their weather).
* Training rows: every target cell of each case plus NEG_PER_CASE random other cells of its grid.
  Model: scikit-learn HistGradientBoostingClassifier.
* Test: each test case's whole grid is scored; for a range of zone areas, the N best cells of the
  model and of a circle around the front (distance only) are compared: share of the next day's new
  detections inside (targets outside the grid count as misses for both). Paired bootstrap over
  fires (cases of one fire resampled together).
* Saves the model to data/training/spread/model.joblib and the report to report.json.
"""

import json
import logging
import sqlite3
import time
from datetime import UTC, datetime

import joblib
import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier

from app.services import spread_ml as sm
from ml.backtest_spread import ROOT
from ml.spread_cases import OUT
from ml.fetch_spread_weather import case_file

log = logging.getLogger("ml.train_spread")
TEST_FROM = datetime(2026, 11, 13, tzinfo=UTC).timestamp()
NEG_PER_CASE = 400
AREAS_KM2 = [1, 2, 5, 10, 20, 40]
SEED = 3


def other_fires():
    db = sqlite3.connect(f"file:{(ROOT / 'firelens.db').as_posix()}?mode=ro", uri=True)
    rows = db.execute("select latitude, longitude, cast(strftime('%s', acquired_at) as integer), event_id from detections "
                      "where static_kind is null and instrument = 'VIIRS'").fetchall()
    lat = np.array([r[0] for r in rows])
    lon = np.array([r[1] for r in rows])
    t = np.array([r[2] for r in rows], dtype=float)
    ev = np.array([r[3] or "" for r in rows])
    return lat, lon, t, ev


def case_inputs(c, O):
    front = np.array(c["front"])
    lat0, lon0 = front[:, 0].mean(), front[:, 1].mean()
    L = sm.layers_for(lat0, lon0)
    w, s, e, n = L.grid.bbox()
    lat, lon, t, ev = O
    m = (t < c["T"]) & (t >= c["T"] - 30 * 86400) & (lat >= s) & (lat <= n) & (lon >= w) & (lon <= e) & (ev != c["event_id"])
    others = np.column_stack([lat[m], lon[m]])
    hourly = json.loads(case_file(c).read_text())
    day = sm.day_weather(hourly, c["T"])
    history = np.array(c["history"]).reshape(-1, 2)
    burned = np.vstack([np.array(c["prior"]).reshape(-1, 2), history])  # everything before the front
    X = sm.features(L, front, history, burned, others, day)
    tcells, inside = sm.target_cells(L, np.array(c["targets"]))
    return L, X, tcells, inside


def capture(order: np.ndarray, n: int, tcells: np.ndarray, inside: float) -> float:
    chosen = np.zeros(order.size, bool)
    chosen[order[:n]] = True
    return float(chosen[tcells].mean()) * inside if len(tcells) else 0.0


def main(limit: int | None = None) -> None:
    rng = np.random.default_rng(SEED)
    cases = [c for c in json.loads((OUT / "cases.json").read_text()) if case_file(c).is_file()]
    train = [c for c in cases if c["T"] < TEST_FROM][:limit]
    test = [c for c in cases if c["T"] >= TEST_FROM][:limit]
    log.info("%d cases: %d train (before 13 Nov), %d test", len(cases), len(train), len(test))
    O = other_fires()
    Xs, ys = [], []
    t0 = time.time()
    for i, c in enumerate(train, 1):
        try:
            L, X, tcells, _ = case_inputs(c, O)
        except Exception as exc:
            log.warning("train case %s skipped: %s", c["event_id"], exc)
            continue
        if L.coverage < 0.99 or not len(tcells):
            continue
        pos = np.unique(tcells)
        neg = rng.choice(X.shape[0], NEG_PER_CASE, replace=False)
        neg = neg[~np.isin(neg, pos)]
        Xs.append(np.vstack([X[pos], X[neg]]))
        ys.append(np.r_[np.ones(len(pos)), np.zeros(len(neg))])
        if i % 200 == 0:
            log.info("train features %d/%d (%.0f min)", i, len(train), (time.time() - t0) / 60)
    X, y = np.vstack(Xs), np.concatenate(ys)
    log.info("training on %d rows (%d positive)", len(y), int(y.sum()))
    model = HistGradientBoostingClassifier(max_iter=400, learning_rate=0.05, max_leaf_nodes=31, l2_regularization=1.0,
                                           early_stopping=True, validation_fraction=0.15, random_state=SEED)
    model.fit(X, y)
    joblib.dump({"model": model, "features": sm.FEATURES, "trained_on": len(train)}, OUT / "model.joblib")

    rows = []
    for i, c in enumerate(test, 1):
        try:
            L, X, tcells, inside = case_inputs(c, O)
        except Exception as exc:
            log.warning("test case %s skipped: %s", c["event_id"], exc)
            continue
        if L.coverage < 0.99:
            continue
        score = model.predict_proba(X)[:, 1]
        d = X[:, sm.FEATURES.index("d_front_km")]
        d_burned = np.minimum(d, X[:, sm.FEATURES.index("d_prior_km")])
        cell_km2 = L.grid.dx_m * L.grid.dy_m / 1e6
        order_m = np.argsort(-score + 1e-9 * d, kind="stable")
        order_c = np.argsort(d, kind="stable")
        # fair baseline: the circle without the ground the fire already burned (targets are new
        # ground > 0.75 km from it by definition), i.e. a ring around the burned area
        order_r = np.argsort(np.where(d_burned < 0.75, 1e6 + d, d), kind="stable")
        r = {"event_id": c["event_id"], "T": c["T"], "targets": len(c["targets"])}
        for a in AREAS_KM2:
            n = int(round(a / cell_km2))
            r[f"model_{a}"] = capture(order_m, n, tcells, inside)
            r[f"circle_{a}"] = capture(order_c, n, tcells, inside)
            r[f"ring_{a}"] = capture(order_r, n, tcells, inside)
        rows.append(r)
        if i % 200 == 0:
            log.info("test %d/%d", i, len(test))
    # paired bootstrap over fires
    events = np.array([r["event_id"] for r in rows])
    uniq = np.unique(events)
    idx_by_ev = {e: np.flatnonzero(events == e) for e in uniq}
    report = {"train_cases": len(train), "test_cases": len(rows), "test_fires": len(uniq), "areas": {}}
    print(f"\n{len(rows)} test cases from {len(uniq)} fires (13-14 Nov); trained on {len(train)} cases before 13 Nov")
    print(f"{'area km2':>9} {'model':>7} {'circle':>7} {'ring':>7}   model - ring (95% CI, bootstrap over fires)")
    for a in AREAS_KM2:
        m = np.array([r[f"model_{a}"] for r in rows])
        cc = np.array([r[f"circle_{a}"] for r in rows])
        rg = np.array([r[f"ring_{a}"] for r in rows])
        boots = []
        for _ in range(2000):
            pick = np.concatenate([idx_by_ev[e] for e in rng.choice(uniq, len(uniq))])
            boots.append((m[pick] - rg[pick]).mean())
        lo, hi = np.percentile(boots, [2.5, 97.5])
        report["areas"][a] = {"model": float(m.mean()), "circle": float(cc.mean()), "ring": float(rg.mean()),
                              "diff_vs_ring": float((m - rg).mean()), "ci": [float(lo), float(hi)]}
        print(f"{a:>9} {m.mean():>7.3f} {cc.mean():>7.3f} {rg.mean():>7.3f}   {(m - rg).mean():+.3f} ({lo:+.3f}..{hi:+.3f})")
    # what the model learned: permutation-free view, the features it splits on most is not exposed by
    # HistGradientBoosting; report the mean score by distance band instead (sanity check)
    (OUT / "report.json").write_text(json.dumps(report, indent=1))
    (OUT / "test_rows.json").write_text(json.dumps(rows))


if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None, help="at most this many train and test cases (smoke test)")
    main(ap.parse_args().limit)
