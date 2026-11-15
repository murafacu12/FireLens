import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


@pytest.fixture(scope="module")
def locked(tmp_path_factory):
    db = tmp_path_factory.mktemp("admin") / "t.db"
    settings = Settings(_env_file=None, database_url=f"sqlite:///{db}", demo_mode=True, demo_days=3, admin_token="s3cret")
    with TestClient(create_app(settings)) as c:
        yield c


RULE = {"name": "zone", "west": -70, "south": -40, "east": -60, "north": -30, "min_confidence": "nominal", "min_area_km2": 0}


def test_health_reports_lock(locked):
    assert locked.get("/api/health").json()["admin_locked"] is True


def test_reads_stay_open(locked):
    assert locked.get("/api/events?limit=1").status_code == 200
    assert locked.get("/api/alerts/rules").status_code == 200


@pytest.mark.parametrize("method,path,body", [
    ("post", "/api/ingest", {}),
    ("post", "/api/alerts/rules", RULE),
    ("delete", "/api/alerts/rules/1", None),
    ("post", "/api/history/backfill", {"start": "2026-01-01", "end": "2026-01-02", "bbox": [-70, -40, -60, -30]}),
    ("delete", "/api/history/jobs/x", None),
])
def test_writes_need_the_key(locked, method, path, body):
    kw = {"json": body} if body is not None else {}
    assert getattr(locked, method)(path, **kw).status_code == 401
    assert getattr(locked, method)(path, headers={"X-Admin-Token": "wrong"}, **kw).status_code == 401


def test_right_key_passes(locked):
    r = locked.post("/api/alerts/rules", json=RULE, headers={"X-Admin-Token": "s3cret"})
    assert r.status_code == 201
    assert locked.delete(f"/api/alerts/rules/{r.json()['id']}", headers={"X-Admin-Token": "s3cret"}).status_code == 204


def test_open_without_a_key(tmp_path):
    settings = Settings(_env_file=None, database_url=f"sqlite:///{tmp_path / 'o.db'}", demo_mode=True, demo_days=3)
    with TestClient(create_app(settings)) as c:
        assert c.get("/api/health").json()["admin_locked"] is False
        assert c.post("/api/alerts/rules", json=RULE).status_code == 201
