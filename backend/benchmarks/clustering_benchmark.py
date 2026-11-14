"""Comparative benchmark: previous clustering rule vs v3 engine.

    python -m benchmarks.clustering_benchmark <dir with FIRMS CSVs> <scratch dir> [--world] [--migrate <v2 db>]

A. Synthetic scenarios (ground truth by construction): pairwise precision/recall, false merges/splits.
B. Real data, central Africa (15-30E, 0-12N, 7 days): both rules through the real store code:
   initial load, hourly refresh, full rebuild, incremental == full, structure, memory.
C. (--world) Real data worldwide with the v3 engine: the same steps.
D. (--migrate) Rehearsal of the production migration on a copy of a v2 database.

Real-data numbers are structural (no labels exist); only section A has ground truth.
Results are printed and written as JSON next to the scratch dir.
"""

import gc
import json
import math
import shutil
import sys
import time
import tracemalloc
from collections import Counter, defaultdict
from datetime import timedelta
from pathlib import Path

from sqlalchemy import func, select, text

from app.db import Base, make_engine, make_session_factory
from app.ingest.clustering import cluster_detections, haversine_km
from app.ingest.normalize import normalize_csv
from app.ingest.sources import get_source
from app.migrations import create_views, upgrade
from app.models import DetectionRow, EventHistoryRow, EventLineageRow, EventRow
from app.services import cells
from app.services.store import flag_static_sources, rebuild_all_events, rebuild_events, upsert_detections
from benchmarks import scenarios as sc
from benchmarks.reference import cluster_haversine_grid

OLD = dict(tolerance_km=1.0, max_gap_hours=24.0, active_hours=24.0, continuity_gap_hours=0.0, continuity_tolerance_km=0.0, archive_days=30.0)
NEW = {**OLD, "continuity_gap_hours": 72.0}
RESULTS: dict = {}


def log(msg: str) -> None:
    print(msg, flush=True)


# ------------------------------------------------------------------ A. synthetic


def synthetic() -> None:
    rows = []
    for scen in sc.all_scenarios():
        dets = [d for d in scen.detections if d.id not in scen.static_ids]
        truth = {d.id: scen.truth[d.id] for d in dets}
        row = {"scenario": scen.name, "detections": len(dets), "assumption": scen.assumption}
        for label, p in (("old", OLD), ("new", NEW)):
            groups = cluster_detections(dets, p["tolerance_km"], p["max_gap_hours"], p["continuity_gap_hours"], p["continuity_tolerance_km"])
            row[label] = sc.pairwise_scores(truth, {d.id: str(k) for k, g in enumerate(groups) for d in g})
        rows.append(row)
        log(f"{scen.name:34} old {row['old']} | new {row['new']}")
    tot = {k: {m: sum(r[k][m] for r in rows) for m in ("false_merges", "false_splits")} for k in ("old", "new")}
    log(f"totals: {tot}")
    RESULTS["synthetic"] = {"rows": rows, "totals": tot}


# ------------------------------------------------------------------ real data helpers


def load_csvs(csv_dir: Path, box=None):
    dets = []
    for src in ("VIIRS_NOAA20_NRT", "VIIRS_NOAA21_NRT", "MODIS_NRT"):
        dets += normalize_csv((csv_dir / f"{src}.csv").read_text(encoding="utf-8"), get_source(src)).detections
    if box:
        w, s, e, n = box
        dets = [d for d in dets if w <= d.longitude <= e and s <= d.latitude <= n]
    return dets


def fresh_db(path: Path):
    for f in path.parent.glob(path.name + "*"):
        f.unlink()
    engine = make_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    create_views(engine)
    return engine, make_session_factory(engine)


def ingest(factory, batch, params, now):
    with factory() as s:
        t = time.time()
        seeds = upsert_detections(s, batch)
        seeds += flag_static_sources(s)
        cells.refresh_cells(s, {cells.epoch_hour(x.t) for x in seeds})
        t_store = time.time() - t
        t = time.time()
        events = rebuild_events(s, seeds, now=now, **params)
        s.commit()
        return {"new_detections": len(seeds), "events_rebuilt": len(events), "store_s": round(t_store, 1), "rebuild_s": round(time.time() - t, 1)}


def partition(s) -> set:
    groups = defaultdict(set)
    for did, eid in s.execute(select(DetectionRow.id, DetectionRow.event_id).where(DetectionRow.event_id.is_not(None))):
        groups[eid].add(did)
    return {frozenset(g) for g in groups.values()}


def structure(s) -> dict:
    rows = s.execute(select(EventRow.latitude, EventRow.longitude, EventRow.start_at, EventRow.end_at, EventRow.extent_km,
                            EventRow.evidence, EventRow.lifecycle_state, EventRow.west, EventRow.east, EventRow.south, EventRow.north)).all()
    restart = _restart_share([(r.latitude, r.longitude, r.start_at, r.end_at) for r in rows])
    return {
        "events": len(rows),
        "evidence": dict(Counter(r.evidence for r in rows)),
        "lifecycle": dict(Counter(r.lifecycle_state for r in rows)),
        "widest_km": round(max((r.extent_km for r in rows), default=0), 1),
        "wider_than_20km": sum(1 for r in rows if r.extent_km > 20),
        "restart_within_3km_24_96h": restart,
    }


def _restart_share(evs) -> float:
    cell = defaultdict(list)
    for e in evs:
        cell[(math.floor(e[0] / 0.05), math.floor(e[1] / 0.05))].append(e)
    hits = 0
    for e in evs:
        gy, gx = math.floor(e[0] / 0.05), math.floor(e[1] / 0.05)
        hits += any(o is not e and timedelta(hours=24) < e[2] - o[3] <= timedelta(hours=96) and haversine_km(e[0], e[1], o[0], o[1]) < 3
                    for dy in (-1, 0, 1) for dx in (-1, 0, 1) for o in cell.get((gy + dy, gx + dx), ()))
    return round(hits / max(1, len(evs)), 3)


def real_run(label: str, dets, scratch: Path, params: dict, measure_memory: bool) -> dict:
    newest = max(d.acquired_at for d in dets)
    initial = [d for d in dets if d.acquired_at < newest - timedelta(hours=2)]
    refresh = [d for d in dets if d.acquired_at >= newest - timedelta(hours=24)]  # the hourly 24 h file
    now = newest + timedelta(hours=1)
    _, factory = fresh_db(scratch / f"bench_{label}.db")
    out = {"detections": len(dets)}
    log(f"[{label}] initial load ({len(initial):,})")
    out["initial"] = ingest(factory, initial, params, now)
    log(f"[{label}] {out['initial']}")
    out["hourly"] = ingest(factory, refresh, params, now)
    with factory() as s:
        out["hourly"]["history_rows"] = s.scalar(select(func.count()).select_from(EventHistoryRow))
        incremental = partition(s)
        inc_ids = set(s.scalars(select(EventRow.id)))
        out["structure"] = structure(s)
        t = time.time()
        rebuild_all_events(s, **params)
        out["full_rebuild_s"] = round(time.time() - t, 1)
        out["incremental_equals_full"] = partition(s) == incremental
        out["incremental_ids_equal_full"] = set(s.scalars(select(EventRow.id))) == inc_ids
        out["lineage_after_full_rebuild"] = s.scalar(select(func.count()).select_from(EventLineageRow))
        s.rollback()
    log(f"[{label}] hourly {out['hourly']} | full {out['full_rebuild_s']}s | equal {out['incremental_equals_full']}")
    log(f"[{label}] structure {out['structure']}")
    if measure_memory:  # separate pass: tracemalloc slows Python down, so it never overlaps a timing
        _, factory = fresh_db(scratch / f"bench_{label}_mem.db")
        ingest(factory, initial, params, now)
        gc.collect()
        tracemalloc.start()
        ingest(factory, refresh, params, now)
        out["hourly_peak_mb"] = round(tracemalloc.get_traced_memory()[1] / 1e6, 1)
        tracemalloc.reset_peak()
        with factory() as s:
            rebuild_all_events(s, **params)
            out["full_rebuild_peak_mb"] = round(tracemalloc.get_traced_memory()[1] / 1e6, 1)
            s.rollback()
        tracemalloc.stop()
        log(f"[{label}] memory: hourly peak {out['hourly_peak_mb']} MB, full rebuild peak {out['full_rebuild_peak_mb']} MB")
    return out


def migrate_rehearsal(v2_db: Path, scratch: Path) -> dict:
    copy = scratch / "migration_rehearsal.db"
    for f in scratch.glob("migration_rehearsal.db*"):
        f.unlink()
    shutil.copy(v2_db, copy)
    engine = make_engine(f"sqlite:///{copy}")
    with engine.begin() as conn:  # the copy must be a v2 database (no v3 columns yet)
        cols = {r[1] for r in conn.execute(text("PRAGMA table_info(fire_events)"))}
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)

    class S:  # production defaults
        cluster_tolerance_km, cluster_max_gap_hours, event_active_hours = 1.0, 24.0, 24.0
        cluster_continuity_gap_hours, cluster_continuity_tolerance_km, event_archive_days = 72.0, 0.0, 30.0

    t = time.time()
    upgrade(engine, factory, S())
    took = round(time.time() - t, 1)
    with factory() as s:
        legacy = s.execute(text("SELECT count(*) FROM fire_events_legacy_v2")).scalar()
        out = {
            "v2_had_new_columns": "lifecycle_state" in cols,
            "migration_s": took,
            "legacy_events_kept": legacy,
            "events_after": s.scalar(select(func.count()).select_from(EventRow)),
            "lineage": dict(s.execute(select(EventLineageRow.kind, func.count()).group_by(EventLineageRow.kind)).all()),
            "history_rows": s.scalar(select(func.count()).select_from(EventHistoryRow)),
        }
        # every v2 id still resolves to a live event (or was all non-fire)
        from app.services.queries import resolve_event_id

        sample = [r[0] for r in s.execute(text("SELECT id FROM fire_events_legacy_v2 ORDER BY id LIMIT 2000"))]
        out["v2_ids_resolving_sample_2000"] = sum(1 for i in sample if resolve_event_id(s, i))
    log(f"[migration] {out}")
    return out


def equivalence(label: str, dets) -> dict:
    """Same partition from the chord test (production) and the haversine predicate (reference)."""
    out = {}
    for rule, p in (("old", OLD), ("new", NEW)):
        args = (p["tolerance_km"], p["max_gap_hours"], p["continuity_gap_hours"], p["continuity_tolerance_km"])
        t = time.time()
        chord = {frozenset(d.id for d in g) for g in cluster_detections(dets, *args)}
        t_chord = time.time() - t
        t = time.time()
        hav = set(cluster_haversine_grid(dets, *args))
        out[rule] = {"events": len(chord), "identical": chord == hav, "chord_s": round(t_chord, 1),
                     "haversine_s": round(time.time() - t, 1), "differing_events": len(chord ^ hav)}
    log(f"[equivalence {label}] {out}")
    return out


def main() -> None:
    csv_dir, scratch = Path(sys.argv[1]), Path(sys.argv[2])
    log("== A. synthetic scenarios (ground truth by construction)")
    synthetic()
    log("== B. central Africa, both rules")
    africa = load_csvs(csv_dir, (15, 0, 30, 12))
    RESULTS["africa"] = {"old": real_run("africa_old", africa, scratch, OLD, True),
                         "new": real_run("africa_new", africa, scratch, NEW, True)}
    log("== E. chord vs haversine membership")
    eq = {"synthetic": {}}
    for scen in sc.all_scenarios():
        eq["synthetic"][scen.name] = equivalence(scen.name, scen.detections)
    eq["africa"] = equivalence("africa", africa)
    del africa
    RESULTS["equivalence"] = eq
    if "--world" in sys.argv:
        log("== C. worldwide, both rules")
        world = load_csvs(csv_dir)
        RESULTS["world"] = {"old": real_run("world_old", world, scratch, OLD, False),
                            "new": real_run("world_new", world, scratch, NEW, False)}
        RESULTS["equivalence"]["world"] = equivalence("world", world)
        del world
    if "--migrate" in sys.argv:
        log("== D. migration at worldwide scale, on a copy of a v2 benchmark database")
        RESULTS["migration"] = migrate_rehearsal(Path(sys.argv[sys.argv.index("--migrate") + 1]), scratch)
    (scratch / "clustering_benchmark.json").write_text(json.dumps(RESULTS, indent=2, default=str))
    log("done")


if __name__ == "__main__":
    main()
