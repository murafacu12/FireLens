"""Train and evaluate the fire-danger model on a held-out year.

    python -m ml.train            (builds the dataset if needed, trains, evaluates, writes the report)

Model: gradient-boosted trees (scikit-learn HistGradientBoostingClassifier), probabilities calibrated
by isotonic regression on cross-year predictions (fit on one training year, predict the other).
Evaluated on the held-out year against three baselines:
* climatology: how often this cell burned in this month in the training years;
* persistence: logistic regression on recent fire in the cell and around it;
* the app's current heuristic score (baseline-hdw-v2: hot-dry-windy index, dryness, activity).
"Beats the baseline" = the 95 % week-block bootstrap interval of the improvement in average
precision AND in ROC AUC is above zero against every baseline.
"""

import json
import logging
import time
from pathlib import Path

import joblib
import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.inspection import permutation_importance
from sklearn.isotonic import IsotonicRegression
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score, brier_score_loss, log_loss, roc_auc_score

from ml import dataset
from ml.download_firms import REGION, training_db

log = logging.getLogger("ml.train")
MODEL_VERSION = "fire-danger-arg-v1"
ARTIFACT_DIR = Path(__file__).resolve().parents[1] / "app" / "ml_models"
BOOTSTRAP = 300
SEED = 0


def gbm() -> HistGradientBoostingClassifier:
    return HistGradientBoostingClassifier(
        learning_rate=0.06, max_iter=400, max_leaf_nodes=31, min_samples_leaf=200, l2_regularization=1.0,
        early_stopping=True, validation_fraction=0.1, n_iter_no_change=30, random_state=SEED)


def metrics(y: np.ndarray, p: np.ndarray, clim: np.ndarray) -> dict:
    order = np.argsort(-p)
    top = order[: max(1, len(p) // 10)]
    b = brier_score_loss(y, p)
    b_clim = brier_score_loss(y, clim)
    return {
        "roc_auc": round(float(roc_auc_score(y, p)), 4),
        "average_precision": round(float(average_precision_score(y, p)), 4),
        "brier": round(float(b), 5),
        "brier_skill_vs_climatology": round(float(1 - b / b_clim), 4),
        "log_loss": round(float(log_loss(y, np.clip(p, 1e-6, 1 - 1e-6))), 4),
        "fires_in_top_10pct": round(float(y[top].sum() / max(1, y.sum())), 4),
    }


def reliability(y: np.ndarray, p: np.ndarray, bins: int = 10) -> list[dict]:
    edges = np.quantile(p, np.linspace(0, 1, bins + 1))
    out = []
    for i in range(bins):
        sel = (p >= edges[i]) & (p <= edges[i + 1]) if i == bins - 1 else (p >= edges[i]) & (p < edges[i + 1])
        if sel.any():
            out.append({"predicted": round(float(p[sel].mean()), 4), "observed": round(float(y[sel].mean()), 4), "n": int(sel.sum())})
    return out


def bootstrap_delta(y, p_model, p_base, week, rng) -> dict:
    """95 % interval of model - baseline for AP and AUC, resampling whole weeks (fires cluster in time)."""
    weeks = np.unique(week)
    idx_by_week = {w: np.flatnonzero(week == w) for w in weeks}
    d_ap, d_auc = [], []
    for _ in range(BOOTSTRAP):
        pick = rng.choice(weeks, size=len(weeks), replace=True)
        idx = np.concatenate([idx_by_week[w] for w in pick])
        yy = y[idx]
        if yy.min() == yy.max():
            continue
        d_ap.append(average_precision_score(yy, p_model[idx]) - average_precision_score(yy, p_base[idx]))
        d_auc.append(roc_auc_score(yy, p_model[idx]) - roc_auc_score(yy, p_base[idx]))
    q = lambda xs: [round(float(np.percentile(xs, 2.5)), 4), round(float(np.percentile(xs, 97.5)), 4)]  # noqa: E731
    return {"delta_average_precision_95ci": q(d_ap), "delta_roc_auc_95ci": q(d_auc)}


def main() -> None:
    t0 = time.time()
    ds = dataset.build(REGION)
    X, y, meta, base = ds["X"], ds["y"], ds["meta"], ds["baseline"]
    feats = ds["features"]
    year = meta[:, 3]
    train, test = year < dataset.TEST_YEAR, year == dataset.TEST_YEAR
    log.info("dataset: %d rows (%d cells), fire rate %.4f; train %d, test %d (%.0f s)",
             len(y), ds["cells"], y.mean(), train.sum(), test.sum(), time.time() - t0)

    # out-of-fold predictions across the two training years -> isotonic calibration
    oof = np.zeros(len(y), np.float32)
    for k in (0, 1):
        fit, pred = year == k, year == 1 - k
        m = gbm().fit(X[fit], y[fit])
        oof[pred] = m.predict_proba(X[pred])[:, 1]
    iso = IsotonicRegression(out_of_bounds="clip", y_min=0, y_max=1).fit(oof[train], y[train])
    model = gbm().fit(X[train], y[train])
    p_model = iso.predict(model.predict_proba(X[test])[:, 1])
    log.info("model trained (%d iterations), %.0f s", model.n_iter_, time.time() - t0)

    # baselines
    ci = feats.index("climatology")
    p_clim = np.clip(X[test, ci], 1e-6, 1)
    pers_cols = [feats.index(c) for c in ("fire_issue_day", "fire_7d", "neighbours_7d", "lead_days")]
    pers = LogisticRegression(max_iter=1000).fit(X[train][:, pers_cols], y[train])
    p_pers = pers.predict_proba(X[test][:, pers_cols])[:, 1]
    heur = IsotonicRegression(out_of_bounds="clip", y_min=0, y_max=1).fit(base[train], y[train])
    p_heur = heur.predict(base[test])

    yt = y[test]
    days = np.array(ds["days"])
    week = meta[test, 0] // 7
    rng = np.random.default_rng(SEED)
    results = {name: metrics(yt, p, p_clim) for name, p in
               (("model", p_model), ("current_score", p_heur), ("persistence", p_pers), ("climatology", p_clim))}
    beats = {}
    for name, p in (("current_score", p_heur), ("persistence", p_pers), ("climatology", p_clim)):
        beats[name] = bootstrap_delta(yt, p_model, p, week, rng)
    passed = all(v["delta_average_precision_95ci"][0] > 0 and v["delta_roc_auc_95ci"][0] > 0 for v in beats.values())

    lead = X[test, feats.index("lead_days")].astype(int)
    by_lead = {int(L): {"model": metrics(yt[lead == L], p_model[lead == L], p_clim[lead == L]),
                        "current_score": metrics(yt[lead == L], p_heur[lead == L], p_clim[lead == L])} for L in range(1, 8)}
    months = np.array([int(days[i][5:7]) for i in meta[test, 0]])
    by_month = {}
    for m in range(1, 13):
        sel = months == m
        if sel.any() and 0 < yt[sel].sum() < sel.sum():
            by_month[m] = {"fire_rate": round(float(yt[sel].mean()), 4),
                           "model_ap": round(float(average_precision_score(yt[sel], p_model[sel])), 4),
                           "current_score_ap": round(float(average_precision_score(yt[sel], p_heur[sel])), 4)}

    sub = rng.choice(np.flatnonzero(test), size=min(150_000, int(test.sum())), replace=False)
    imp = permutation_importance(model, X[sub], y[sub], scoring="average_precision", n_repeats=3, random_state=SEED)
    importance = sorted(({"feature": f, "ap_drop": round(float(m), 4)} for f, m in zip(feats, imp.importances_mean)),
                        key=lambda r: -r["ap_drop"])

    report = {
        "model_version": MODEL_VERSION, "region": REGION, "training_db": str(training_db(REGION)),
        "label": "VIIRS NOAA-20 fire detection (nominal/high, non-static) in a 0.5 deg cell on the target day",
        "years": {"train": ["2023-11-15", "2025-11-14"], "test": ["2025-11-15", "2026-11-14"]},
        "rows": {"total": int(len(y)), "train": int(train.sum()), "test": int(test.sum()), "cells": int(ds["cells"]),
                 "test_fire_rate": round(float(yt.mean()), 4)},
        "held_out_year": results, "improvement_over": beats, "beats_all_baselines": passed,
        "by_lead_days": by_lead, "by_month": by_month,
        "reliability_model": reliability(yt, p_model), "reliability_current_score": reliability(yt, p_heur),
        "feature_importance": importance, "features": feats, "gbm_iterations": int(model.n_iter_),
        "seconds": round(time.time() - t0),
    }
    out = training_db(REGION).parent / f"{MODEL_VERSION}_report.json"
    out.write_text(json.dumps(report, indent=2))
    ARTIFACT_DIR.mkdir(exist_ok=True)
    joblib.dump({"version": MODEL_VERSION, "model": model, "calibration": iso, "features": feats,
                 "grid": ds["grid"], "domain": ds["domain"], "climatology": ds["climatology"][: dataset.TEST_YEAR].mean(axis=0),
                 "region": REGION, "beats_all_baselines": passed, "held_out": results["model"]},
                ARTIFACT_DIR / f"{MODEL_VERSION}.joblib", compress=3)
    log.info("held-out year: %s", json.dumps(results))
    log.info("improvement 95%% CI: %s | beats all baselines: %s", json.dumps(beats), passed)
    log.info("report: %s (%.0f s)", out, time.time() - t0)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s")
    main()
