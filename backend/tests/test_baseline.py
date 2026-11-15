"""Seasonal baseline: same days of earlier years, same satellites, same area."""

from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import Base, make_engine, make_session_factory
from app.main import create_app
from app.services import baseline as bl
from app.services.coverage import record_coverage
from app.services.store import upsert_detections
from benchmarks import scenarios as sc

BOX = (-64.0, -32.0, -62.0, -30.0)  # 2 x 2 deg = 16 cells of 0.5 deg
LAT, LON = -31.2, -63.3
FIRST = date(2026, 8, 1)  # sc.T0 is 2026-08-01 03:00 UTC
NOW = datetime(2026, 8, 9, 12, tzinfo=UTC)


def fill_archive(engine, per_year: dict[int, int], box=BOX, extra_source=None) -> None:
    """Archive counts for 1..7 Aug of each year, spread over the first 3 days, NOAA-20 and MODIS."""
    cy, cx = bl.cell_of(LAT, LON)
    cells = {}
    for year, n in per_year.items():
        d = date(year, 8, 1)
        for i, share in enumerate((0.5, 0.3, 0.2)):
            cells[("viirs_noaa20", bl.day_no(d) + i, cy, cx)] = [round(n * share), 0, 0.0]
            cells[("modis", bl.day_no(d) + i, cy, cx)] = [round(n * share / 4), 0, 0.0]
    with engine.begin() as conn:
        bl.write_cells(conn, cells)
        for grp in bl.GROUPS:
            bl.add_source(conn, grp, date(min(per_year), 1, 1), date(max(per_year), 12, 31), box, "test")
            if extra_source:
                bl.add_source(conn, grp, *extra_source, "test2")


@pytest.fixture
def live(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'live.db'}")
    Base.metadata.create_all(engine)
    with make_session_factory(engine)() as s:
        b = sc._Builder("seasonal", 3)
        for day in range(7):  # 40 NOAA-20 + 4 NOAA-21 + 10 Aqua pixels a day for a week
            b.front(f"F{day}", LAT, LON, 24 * day, "NOAA-20", 40)
            b.front(f"F{day}", LAT, LON, 24 * day + 0.8, "NOAA-21", 4)
            b.front(f"F{day}", LAT, LON, 24 * day + 1.6, "Aqua", 10)
        upsert_detections(s, b.dets)
        record_coverage(s, [FIRST + timedelta(days=i) for i in range(9)], (-180.0, -90.0, 180.0, 90.0), "VIIRS_NOAA20_NRT")
        s.commit()
        yield s


@pytest.fixture
def base(tmp_path):
    return bl.open_rw(tmp_path / "baseline.db")


def run(live, base, start=datetime(2026, 8, 1, tzinfo=UTC), end=datetime(2026, 8, 8, tzinfo=UTC), bbox=BOX):
    return bl.compare(live, base, bbox, start, end, now=NOW)


def test_well_above_every_past_season(live, base):
    fill_archive(base, {2023: 120, 2024: 160, 2025: 100})
    r = run(live, base)
    g = r["groups"]["viirs_noaa20"]
    assert r["available"] and r["headline"] == "viirs_noaa20" and r["days"] == {"first": "2026-08-01", "last": "2026-08-07", "count": 7}
    assert g["current"] == 280  # NOAA-20 only: the NOAA-21 pixels never count
    assert [y["year"] for y in g["years"]] == [2025, 2024, 2023] and g["median"] == 120 and g["rank"] == 1
    assert g["verdict"] == "well_above" and g["current_days"] == {"archive": 0, "live": 7, "missing": 0}
    assert sum(d["current"] for d in g["daily"]) == 280 and g["daily"][0]["median"] == 60  # 0.5 x 120
    assert r["groups"]["modis"]["current"] == 70


def test_within_range_and_below(live, base):
    fill_archive(base, {2023: 200, 2024: 320, 2025: 260})
    assert run(live, base)["groups"]["viirs_noaa20"]["verdict"] == "within_range"
    assert bl.verdict(100, [200, 320, 260]) == "well_below"
    assert bl.verdict(150, [200, 210, 300]) == "below"  # under every season, but not below 0.67 x typical
    assert bl.verdict(190, [200, 320, 260]) == "within_range"  # under the lowest by less than counting noise
    assert bl.verdict(5, [3, 8]) == "too_few_fires"
    assert bl.verdict(50, [40]) == "insufficient_seasons"


def test_years_without_archive_are_left_out_not_zero(live, base):
    fill_archive(base, {2024: 120, 2025: 100})  # 2023 and earlier: not downloaded
    g = run(live, base)["groups"]["viirs_noaa20"]
    assert g["seasons"] == 2 and {y["year"] for y in g["years"]} == {2024, 2025}


def test_area_cut_to_what_every_year_covers(live, base):
    fill_archive(base, {2024: 120, 2025: 100}, box=(-64.0, -32.0, -63.0, -30.0))  # west half of BOX
    g = run(live, base)["groups"]["viirs_noaa20"]
    assert g["area"] == [[-64.0, -32.0, -63.0, -30.0]] and g["area_fraction"] == 0.5
    assert g["current"] == 280  # the fire is in the west half
    fill_archive(base, {2024: 120}, box=(-64.0, -32.0, -63.5, -31.5))
    r = run(live, base, bbox=(-64.0, -32.0, -60.0, -28.0))
    assert r["groups"]["viirs_noaa20"]["verdict"] == "area_not_covered" and not r["available"]


def test_current_days_from_archive_where_it_covers_them(live, base):
    # the archive also holds the current year for 1..3 Aug: those days come from it, the rest from live
    fill_archive(base, {2024: 120, 2025: 100, 2026: 1000}, extra_source=None)
    g = run(live, base)["groups"]["viirs_noaa20"]
    assert g["current_days"] == {"archive": 7, "live": 0, "missing": 0}  # 2026 source spans the whole year
    assert g["current"] == 1000  # every day from the archive (its 3 days of data; the other 4 have none)
    assert {y["year"] for y in g["years"]} == {2024, 2025}


def test_incomplete_and_short_periods(live, base):
    fill_archive(base, {2024: 120, 2025: 100})
    r = run(live, base, end=datetime(2026, 8, 12, tzinfo=UTC))  # ends after NOW - 6 h: cut to whole past days
    assert r["days"]["last"] == "2026-08-08"
    r = run(live, base, start=datetime(2026, 8, 7, 5, tzinfo=UTC), end=datetime(2026, 8, 8, 5, tzinfo=UTC))
    assert not r["available"] and r["reason"] == "period_too_short"
    assert bl.compare(live, None, BOX, datetime(2026, 8, 1, tzinfo=UTC), datetime(2026, 8, 8, tzinfo=UTC), now=NOW)["reason"] == "no_baseline"
    # a day the live database has not loaded, outside the archive: not comparable
    r = run(live, base, start=datetime(2026, 8, 3, tzinfo=UTC), end=datetime(2026, 8, 10, 0, tzinfo=UTC))
    g = r["groups"]["viirs_noaa20"]
    assert g["current_days"]["missing"] == 0  # 3..8 Aug loaded (coverage recorded through 9 Aug)


def test_live_detections_on_archive_static_sites_left_out(live, base):
    # the archive labelled a gas flare where the live fire is: live counts must leave it out,
    # as the past seasons do (by label), even though the live database never flagged it
    fill_archive(base, {2024: 120, 2025: 100})
    before = run(live, base)["groups"]["viirs_noaa20"]["current"]
    with base.begin() as conn:
        assert bl.write_static_sites(conn, [(LAT, LON, "industrial")]) == 1
    after = run(live, base)["groups"]["viirs_noaa20"]["current"]
    assert before == 280 and 0 <= after < before  # the front's pixels within ~1.5 km of the site are gone


def test_feb_29_and_full_days():
    assert bl._shift_years(date(2024, 2, 29), 1) == date(2023, 2, 28)
    start, end = datetime(2026, 3, 1, 13, tzinfo=UTC), datetime(2026, 3, 5, 0, tzinfo=UTC)
    assert bl.full_days(start, end, datetime(2026, 3, 9, tzinfo=UTC)) == (date(2026, 3, 2), date(2026, 3, 4))
    assert bl.full_days(start, end, datetime(2026, 3, 5, 3, tzinfo=UTC)) == (date(2026, 3, 2), date(2026, 3, 3))


def test_api_without_baseline(tmp_path):
    db = tmp_path / "a.db"
    settings = Settings(_env_file=None, database_url=f"sqlite:///{db}", demo_mode=True, demo_days=3,
                        baseline_db_path=str(tmp_path / "missing.db"))
    with TestClient(create_app(settings)) as c:
        r = c.get("/api/analysis/baseline", params={"hours": 24 * 7}).json()
        assert r["available"] is False and r["reason"] == "no_baseline"
        assert c.get("/api/analysis/baseline").status_code == 422
