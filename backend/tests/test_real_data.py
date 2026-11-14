"""Real FIRMS data: normalization of genuine files, keyless live ingestion and the history backfill.

Fixtures are the first 60 rows of NASA FIRMS public South America 24h files (2026-11-13/14).
The network is replaced by httpx.MockTransport, so these tests run offline and never spend
FIRMS transactions.
"""

import asyncio
import time
from datetime import date
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.ingest.normalize import normalize_csv
from app.ingest.sources import get_source
from app.main import create_app
from app.services.history import BackfillRequest, plan_backfill

FIX = Path(__file__).parent / "fixtures"
J1 = (FIX / "firms_j1_south_america.csv").read_text(encoding="utf-8")
J2 = (FIX / "firms_j2_south_america.csv").read_text(encoding="utf-8")
MODIS = (FIX / "firms_modis_south_america.csv").read_text(encoding="utf-8")
EMPTY_SNPP = "latitude,longitude,bright_ti4,scan,track,acq_date,acq_time,satellite,confidence,version,bright_ti5,frp,daynight\n"
KEY = "test-map-key-123"


def test_real_files_normalize_without_rejections():
    for text, source, satellite in [(J1, "VIIRS_NOAA20_NRT", "NOAA-20"), (J2, "VIIRS_NOAA21_NRT", "NOAA-21"), (MODIS, "MODIS_NRT", None)]:
        r = normalize_csv(text, get_source(source))
        assert r.rows == 60 and not r.rejected
        assert all(d.processing_level == "NRT" for d in r.detections)
        if satellite:
            assert {d.satellite for d in r.detections} == {satellite}
        else:
            assert {d.satellite for d in r.detections} <= {"Terra", "Aqua"}
    # real VIIRS files spell confidence out ("nominal"), not l/n/h
    assert {d.confidence_raw for d in normalize_csv(J1, get_source("VIIRS_NOAA20_NRT")).detections} <= {"low", "nominal", "high"}


# --------------------------------------------------------------------- backfill planning

AVAIL = {
    "MODIS_SP": (date(2000, 11, 1), date(2026, 6, 30)),
    "MODIS_NRT": (date(2026, 7, 1), date(2026, 11, 14)),
    "VIIRS_SNPP_SP": (date(2012, 1, 20), date(2026, 6, 30)),
    "VIIRS_SNPP_NRT": (date(2026, 7, 1), date(2026, 11, 1)),  # S-NPP outage after Nov 1
    "VIIRS_NOAA20_NRT": (date(2026, 7, 1), date(2026, 11, 14)),
}


def test_plan_prefers_archive_then_nrt_and_respects_day_range():
    chunks, notes = plan_backfill(date(2026, 6, 28), date(2026, 7, 6), AVAIL, ["MODIS"], max_days=5)
    assert [(c.source, c.start.isoformat(), c.days) for c in chunks] == [
        ("MODIS_SP", "2026-06-28", 3),  # SP until its last available day
        ("MODIS_NRT", "2026-07-01", 5),  # then NRT, split at the 5-day limit
        ("MODIS_NRT", "2026-07-06", 1),
    ]
    assert notes == []


def test_plan_reports_gaps_and_unknown_products():
    # NOAA21 has neither SP nor NRT in this availability table
    chunks, notes = plan_backfill(date(2026, 10, 30), date(2026, 11, 3), AVAIL, ["SNPP", "NOAA21"], max_days=5)
    assert [(c.source, c.days) for c in chunks] == [("VIIRS_SNPP_NRT", 3)]
    assert any("SNPP: 2 day" in n for n in notes) and any("NOAA21: 5 day" in n for n in notes)


def test_backfill_request_validation():
    with pytest.raises(ValueError):
        BackfillRequest(start=date(2026, 1, 2), end=date(2026, 1, 1), country="ARG")
    with pytest.raises(ValueError):
        BackfillRequest(start=date(2026, 1, 1), end=date(2026, 1, 2))  # needs bbox or country
    with pytest.raises(ValueError):
        BackfillRequest(start=date(2026, 1, 1), end=date(2026, 1, 2), bbox="10,0,-10,5")  # west > east
    with pytest.raises(ValueError):
        BackfillRequest(start=date(2026, 1, 1), end=date(2026, 1, 2), country="XXX")  # no bbox known
    assert BackfillRequest(start=date(2026, 1, 1), end=date(2026, 1, 2), country="arg").country == "ARG"


# --------------------------------------------------------------------- mocked FIRMS

class FakeFirms:
    """Answers like FIRMS: public files without a key; API calls only with the right MAP_KEY."""

    def __init__(self):
        self.calls: list[str] = []
        self.break_area = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append(path)
        if path.startswith("/data/active_fire/"):
            body = {"J1_": J1, "J2_": J2, "MODIS": MODIS, "SUOMI": EMPTY_SNPP}
            for prefix, text in body.items():
                if path.split("/")[-1].startswith(prefix):
                    return httpx.Response(200, text=text)
            return httpx.Response(404, text="Not Found")
        if path.startswith("/api/"):
            if KEY not in path:
                return httpx.Response(400, text="Invalid MAP_KEY.")
            if path.startswith("/api/data_availability/"):
                rows = "\n".join(f"{k},{a},{b}" for k, (a, b) in AVAIL.items())
                return httpx.Response(200, text=f"data_id,min_date,max_date\n{rows}\n")
            if path.startswith("/api/country/"):  # retired by FIRMS
                return httpx.Response(400, text="Invalid API call.")
            if self.break_area and path.startswith("/api/area/"):
                return httpx.Response(400, text="Invalid API call.")
            if path.startswith("/api/area/"):
                source = path.split("/")[5]  # /api/area/csv/{KEY}/{SOURCE}/...
                text = {"MODIS": MODIS, "VIIRS_NOAA20": J1, "VIIRS_NOAA21": J2, "VIIRS_SNPP": J1.replace("N20", "N")}[source.rsplit("_", 1)[0]]
                if source.endswith("_SP"):  # archive rows carry a version without "NRT"
                    text = text.replace("2.0NRT", "2.0").replace("6.1NRT", "6.1")
                return httpx.Response(200, text=text)
        if path.startswith("/mapserver/mapkey_status"):
            return httpx.Response(200, json={"transaction_limit": 5000, "current_transactions": 3})
        return httpx.Response(404, text="Not Found")


def make_client(tmp_path, **overrides):
    fake = FakeFirms()
    settings = Settings(
        _env_file=None,  # never pick up the developer's real keys from .env
        database_url=f"sqlite:///{tmp_path / 'live.db'}", demo_mode=False, ingest_interval_minutes=0,
        firms_request_pause_s=0, **overrides,
    )
    return TestClient(create_app(settings, http_transport=httpx.MockTransport(fake))), fake


def wait_until(predicate, timeout=20.0):
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.1)
    return False


def test_keyless_live_mode_seeds_from_public_files(tmp_path):
    client, fake = make_client(tmp_path, firms_map_key=None)
    with client:
        assert wait_until(lambda: client.get("/api/health").json()["detections"] > 0 and not client.get("/api/health").json()["seeding"])
        health = client.get("/api/health").json()
        assert health["data_source"] == "firms_public" and health["history_enabled"] is False
        assert health["detections"] == 180  # 3 satellites x 60 real rows; S-NPP file is empty
        assert any("South_America_7d" in c for c in fake.calls)  # seed pulls the 7-day files
        assert not any(c.startswith("/api/") for c in fake.calls)  # no key -> never calls the API
        # history needs the key: clear message, not a crash
        resp = client.post("/api/history/backfill", json={"start": "2026-07-01", "end": "2026-07-02", "country": "ARG"})
        assert resp.status_code == 400 and "MAP_KEY" in resp.json()["detail"]


def test_backfill_with_key_uses_archive_then_nrt(tmp_path):
    client, fake = make_client(tmp_path, firms_map_key=KEY)
    with client:
        wait_until(lambda: not client.get("/api/health").json()["seeding"])
        job = client.post(
            "/api/history/backfill",
            json={"start": "2026-06-29", "end": "2026-07-02", "bbox": "-74,-56,-53,-21", "platforms": ["MODIS", "NOAA20"]},
        )
        assert job.status_code == 202
        job_id = job.json()["id"]
        assert wait_until(lambda: client.get(f"/api/history/jobs/{job_id}").json()["status"] in ("done", "failed"))
        done = client.get(f"/api/history/jobs/{job_id}").json()
        assert done["status"] == "done", done["errors"]
        assert done["total_chunks"] == done["done_chunks"] == 3  # MODIS_SP + MODIS_NRT + VIIRS_NOAA20_NRT
        assert done["notes"] == ["NOAA20: 2 day(s) not available in FIRMS"]
        area_calls = [c for c in fake.calls if c.startswith("/api/area/")]
        assert any("/MODIS_SP/-74,-56,-53,-21/2/2026-06-29" in c for c in area_calls)
        assert any("/MODIS_NRT/-74,-56,-53,-21/2/2026-07-01" in c for c in area_calls)
        assert len(area_calls) == 3  # the startup seed used the free public files, not the API
        # the same MODIS pixels arrived as NRT (seed) and SP (archive): SP wins, nothing duplicated
        modis = client.get("/api/detections", params={"instrument": "MODIS", "limit": 1000}).json()
        assert len(modis) == 60
        avail = client.get("/api/history/availability").json()
        assert avail["products"]["MODIS_SP"]["max_date"] == "2026-06-30"
        assert avail["key_status"]["transaction_limit"] == 5000


def test_firms_errors_never_leak_the_key(tmp_path):
    from app.ingest.firms_client import FirmsClient, FirmsError

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(400, text="Invalid MAP_KEY."))) as http:
            client = FirmsClient(http, "secret-key-xyz", "https://firms.example")
            with pytest.raises(FirmsError) as exc:
                await client.fetch_area_csv("MODIS_NRT", "world", 1)
            assert "secret-key-xyz" not in str(exc.value)

    asyncio.run(run())


def run_job(client, body):
    job_id = client.post("/api/history/backfill", json=body).json()["id"]
    assert wait_until(lambda: client.get(f"/api/history/jobs/{job_id}").json()["status"] in ("done", "failed"))
    return client.get(f"/api/history/jobs/{job_id}").json()


def test_country_backfill_goes_through_area_api(tmp_path):
    client, fake = make_client(tmp_path, firms_map_key=KEY)
    with client:
        wait_until(lambda: not client.get("/api/health").json()["seeding"])
        job = run_job(client, {"start": "2026-06-29", "end": "2026-06-30", "country": "ARG", "platforms": ["MODIS"]})
        assert job["status"] == "done" and job["failed_chunks"] == 0
        assert any("/MODIS_SP/-73.6,-55.1,-53.6,-21.8/2/2026-06-29" in c for c in fake.calls)
        assert not any(c.startswith("/api/country/") for c in fake.calls)
        assert any(c["code"] == "ARG" for c in client.get("/api/history/countries").json())


def test_job_fails_when_every_request_fails(tmp_path):
    client, fake = make_client(tmp_path, firms_map_key=KEY)
    with client:
        wait_until(lambda: not client.get("/api/health").json()["seeding"])
        fake.break_area = True
        job = run_job(client, {"start": "2026-06-29", "end": "2026-06-30", "bbox": "-60,-30,-55,-25", "platforms": ["MODIS"]})
        assert job["status"] == "failed" and job["failed_chunks"] == job["total_chunks"] == 1
        assert "Invalid API call" in job["errors"][0]


# --------------------------------------------------------------------- coverage

def test_covered_fraction_geometry():
    from app.services.coverage import covered_fraction

    south_america = (-95, -60, -30, 15)
    assert covered_fraction((-70, -40, -60, -30), [south_america]) == 1.0  # Argentina view inside
    assert covered_fraction((10, 40, 20, 50), [south_america]) == 0.0  # Europe view
    assert 0.4 < covered_fraction((-40, -10, -20, 10), [south_america]) < 0.6  # straddles the coast
    # a view crossing the antimeridian is split, not misread as a 340-degree-wide box
    assert covered_fraction((170, -10, -170, 10), [(160, -20, 180, 20)]) == pytest.approx(0.5)


def test_public_refresh_records_what_it_covered(tmp_path):
    client, _ = make_client(tmp_path, firms_map_key=None)
    with client:
        assert wait_until(lambda: client.get("/api/health").json()["detections"] > 0 and not client.get("/api/health").json()["seeding"])
        health = client.get("/api/health").json()
        newest = max(
            d.acquired_at
            for text, src in [(J1, "VIIRS_NOAA20_NRT"), (J2, "VIIRS_NOAA21_NRT"), (MODIS, "MODIS_NRT")]
            for d in normalize_csv(text, get_source(src)).detections
        )
        assert health["latest_detection_at"] == newest.isoformat()  # freshness = newest stored observation
        today = time.strftime("%Y-%m-%d", time.gmtime())
        argentina = client.get("/api/history/coverage", params={"start": today, "end": today, "bbox": "-73.6,-55.1,-53.6,-21.8"}).json()
        assert argentina["days"][0]["status"] == "loaded" and argentina["missing"] == []
        europe = client.get("/api/history/coverage", params={"start": today, "end": today, "bbox": "-10,35,30,60"}).json()
        assert europe["days"][0]["status"] == "missing" and europe["can_load"] is False
        older = client.get("/api/history/coverage", params={"start": "2026-06-01", "end": "2026-06-02", "bbox": "-73.6,-55.1,-53.6,-21.8"}).json()
        assert older["missing"] == ["2026-06-01", "2026-06-02"]


def test_backfill_marks_its_days_loaded(tmp_path):
    client, _ = make_client(tmp_path, firms_map_key=KEY)
    with client:
        wait_until(lambda: not client.get("/api/health").json()["seeding"])
        params = {"start": "2026-06-29", "end": "2026-06-30", "bbox": "-60,-30,-55,-25"}
        assert client.get("/api/history/coverage", params=params).json()["missing"] == ["2026-06-29", "2026-06-30"]
        job = run_job(client, {**params, "platforms": ["MODIS"]})
        assert job["status"] == "done"
        after = client.get("/api/history/coverage", params=params).json()
        assert after["missing"] == [] and after["can_load"] is True
        # a much bigger view around it is only partially loaded
        wide = client.get("/api/history/coverage", params={**params, "bbox": "-80,-50,-40,-10"}).json()
        assert {d["status"] for d in wide["days"]} == {"partial"}
