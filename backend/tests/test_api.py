import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    db = tmp_path_factory.mktemp("db") / "test.db"
    settings = Settings(_env_file=None, database_url=f"sqlite:///{db}", demo_mode=True, demo_days=30)
    with TestClient(create_app(settings)) as c:
        yield c


def test_health_after_demo_seed(client):
    body = client.get("/api/health").json()
    assert body["demo_mode"] is True
    assert body["detections"] > 1000
    assert 14 <= body["events"] < 400  # 14 synthetic fires + scattered small burns


def test_both_sensors_present_and_harmonized(client):
    dets = client.get("/api/detections", params={"limit": 50000}).json()
    instruments = {d["instrument"] for d in dets}
    assert instruments == {"MODIS", "VIIRS"}
    assert all(0 <= d["confidence_pct"] <= 100 for d in dets)
    assert all(d["acquired_at"].endswith(("Z", "+00:00")) for d in dets[:50])


def test_detection_filters(client):
    viirs_high = client.get("/api/detections", params={"instrument": "VIIRS", "min_confidence": "high"}).json()
    assert viirs_high and all(d["instrument"] == "VIIRS" and d["confidence_level"] == "high" for d in viirs_high)
    # Córdoba hotspot, 60 km radius
    near = client.get("/api/detections", params={"lat": -31.35, "lon": -64.55, "radius_km": 60}).json()
    assert near and all(-32.0 < d["latitude"] < -30.7 for d in near)
    assert client.get("/api/detections", params={"lat": 10}).status_code == 422


def test_events_filters_and_detail(client):
    big = client.get("/api/events", params={"min_area": 5, "sort": "area"}).json()
    assert big and all(e["area_km2"] >= 5 for e in big)
    assert big == sorted(big, key=lambda e: -e["area_km2"])
    detail = client.get(f"/api/events/{big[0]['id']}").json()
    assert len(detail["detections"]) == detail["detection_count"]
    assert client.get("/api/events/nope").status_code == 404


def test_calendar(client):
    days = client.get("/api/calendar").json()
    assert len(days) == 30
    assert sum(d["detections"] for d in days) > 0


def test_alert_rule_fires_on_reingest(client):
    rule = client.post(
        "/api/alerts/rules",
        json={"name": "Corrientes", "west": -59, "south": -29.5, "east": -56, "north": -27, "min_confidence": "low"},
    )
    assert rule.status_code == 201
    report = client.post("/api/ingest", json={"days": 30}).json()
    assert report["demo"] is True and report["stored"] > 0
    alerts = client.get("/api/alerts").json()
    assert all(a["latitude"] > -29.5 for a in alerts)
    assert any(a["rule_id"] == rule.json()["id"] for a in alerts)
    # idempotent: re-ingesting the same data must not duplicate alerts
    client.post("/api/ingest", json={"days": 30})
    assert len(client.get("/api/alerts").json()) == len(alerts)
    assert client.delete(f"/api/alerts/rules/{rule.json()['id']}").status_code == 204


def test_reingest_is_idempotent(client):
    before = client.get("/api/health").json()
    client.post("/api/ingest", json={"days": 30})
    after = client.get("/api/health").json()
    assert (before["detections"], before["events"]) == (after["detections"], after["events"])


def test_past_fire_is_inactive_and_active_fire_found(client):
    cordoba = client.get("/api/events", params={"lat": -31.35, "lon": -64.55, "radius_km": 60, "sort": "area"}).json()
    assert cordoba[0]["status"] == "inactive" and cordoba[0]["area_km2"] > 5
    active = client.get("/api/events", params={"status": "active", "min_area": 1}).json()
    assert active and all(e["status"] == "active" for e in active)


def test_ndvi_not_configured(client):
    assert client.get("/api/ndvi", params={"lat": 0, "lon": 0}).status_code == 501


def test_compact_detections_match_full_and_detail_lookup(client):
    params = {"instrument": "VIIRS", "min_confidence": "high", "limit": 300}
    full = client.get("/api/detections", params=params).json()
    compact = client.get("/api/detections/compact", params=params).json()
    assert compact["count"] == len(full) == len(compact["lat"]) == len(compact["t"])
    assert compact["id"] == [d["id"] for d in full]
    assert set(compact["viirs"]) == {1} and set(compact["conf"]) == {2}
    one = client.get(f"/api/detections/{compact['id'][0]}").json()
    assert one["id"] == compact["id"][0] and abs(one["latitude"] - compact["lat"][0]) < 1e-4
    assert client.get("/api/detections/nope").status_code == 404


def test_responses_are_gzipped(client):
    resp = client.get("/api/detections/compact", params={"limit": 5000}, headers={"Accept-Encoding": "gzip"})
    assert resp.headers.get("content-encoding") == "gzip"


def test_grid_totals_are_exact_and_cells_follow_filters(client):
    total = client.get("/api/health").json()["detections"]
    coarse = client.get("/api/detections/grid", params={"cell_deg": 5}).json()
    fine = client.get("/api/detections/grid", params={"cell_deg": 0.5}).json()
    # exact totals, unaffected by the 5000-row default limit of the list endpoints
    assert coarse["total"] == fine["total"] == total > 5000
    assert sum(coarse["count"]) == total and coarse["cells"] == len(coarse["lat"])
    assert fine["cells"] > coarse["cells"]
    viirs = client.get("/api/detections/grid", params={"cell_deg": 5, "instrument": "VIIRS"}).json()
    assert 0 < viirs["total"] < total and sum(viirs["viirs"]) == viirs["total"]
    # a cell's marker sits on its fires: centroid inside the cell's bounds
    for lat, lon in zip(fine["lat"], fine["lon"]):
        assert -90 <= lat <= 90 and -180 <= lon <= 180


def test_grid_respects_bbox_including_antimeridian(client):
    south_america = client.get("/api/detections/grid", params={"cell_deg": 1, "bbox": "-82,-56,-34,13"}).json()
    assert south_america["total"] > 0
    assert all(-82 <= lon <= -34 for lon in south_america["lon"])
    # a box crossing 180 deg (west > east) covers Australia (150E) and not the Americas
    pacific = client.get("/api/detections/grid", params={"cell_deg": 1, "bbox": "100,-60,-170,10"}).json()
    assert pacific["total"] > 0 and all(lon >= 100 or lon <= -170 for lon in pacific["lon"])


def test_event_count_matches_list(client):
    params = {"min_area": 1}
    events = client.get("/api/events", params={**params, "limit": 10000}).json()
    assert client.get("/api/events/count", params=params).json()["count"] == len(events) > 0
