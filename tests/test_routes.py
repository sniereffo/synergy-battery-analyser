import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from synergy_analyser.main import app, DATA_DIR
from synergy_analyser.chartdata import parse_halfhourly_response

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def test_health_endpoint(client):
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_rate_schedules_endpoint(client):
    data = client.get("/api/rate-schedules").json()
    assert "EV2 TARIFF" in data
    assert data["EV2 TARIFF"]["rates"]["PEAK"] == 53.8446


def test_auth_status_when_not_logged_in(client):
    data = client.get("/api/auth/status").json()
    assert data == {"logged_in": False, "services": []}


def test_analyse_validates_date_range(client):
    response = client.post(
        "/api/analyse",
        json={
            "start_date": "2026-08-02",
            "end_date": "2026-08-01",
            "battery_capacity_kwh": 15.0,
        },
    )
    assert response.status_code == 422


def _poll_job(client, timeout=10.0):
    """Poll the analysis job until it finishes; return the final job state."""
    import time
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get("/api/analyse/progress").json()
        if job["status"] in ("done", "error"):
            return job
        time.sleep(0.05)
    raise AssertionError("job did not finish in time")


def test_analyse_without_login_reports_error(client):
    started = client.post(
        "/api/analyse",
        json={
            "service_id": "001X",
            "start_date": "2026-08-02",
            "end_date": "2026-08-02",
            "battery_capacity_kwh": 15.0,
        },
    )
    assert started.status_code == 200
    job = _poll_job(client)
    assert job["status"] == "error"
    assert "logged in" in job["error"].lower()


def test_analyse_from_cached_file(client, tmp_path, monkeypatch):
    # Write a cached intervals file in the shape the live fetch would produce.
    raw = json.loads((FIXTURES / "chartdata_halfhourly_day.json").read_text())
    intervals = parse_halfhourly_response(raw)
    payload = {
        "tariff_label": "EV2 TARIFF",
        "intervals": [{**i, "timestamp": i["timestamp"].isoformat()} for i in intervals],
    }
    DATA_DIR.mkdir(exist_ok=True)
    cache = DATA_DIR / "2026-08-02_to_2026-08-02.json"
    cache.write_text(json.dumps(payload))
    try:
        started = client.post(
            "/api/analyse",
            json={
                "start_date": "2026-08-02",
                "end_date": "2026-08-02",
                "battery_capacity_kwh": 15.0,
                "cached_file": cache.name,
                "rate_overrides": {"PEAK": 53.8446},
            },
        )
        assert started.status_code == 200
        job = _poll_job(client)
        assert job["status"] == "done", job.get("error")
        data = job["result"]
        assert data["plan"]["name"] == "EV2 TARIFF"
        assert len(data["intervals"]) == 48
        # Consumption total for 2 Aug = sum of the per-period buckets.
        assert round(data["summary"]["total_consumption_kwh"], 2) == round(
            3.209 + 1.596 + 0.986 + 3.195 + 8.311, 2
        )
        assert data["summary"]["total_export_kwh"] == 15.81
        # Explicit override flows through.
        assert data["plan"]["rates"]["Peak"] == 53.8446
    finally:
        cache.unlink(missing_ok=True)
