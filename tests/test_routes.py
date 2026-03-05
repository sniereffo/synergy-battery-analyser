import pytest
from fastapi.testclient import TestClient
from synergy_analyser.main import app


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def test_health_endpoint(client):
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_rate_schedules_endpoint(client):
    response = client.get("/api/rate-schedules")
    assert response.status_code == 200
    data = response.json()
    assert "Home Plan (A1)" in data
    assert "EV Add on" in data
    assert "Midday Saver" in data


def test_login_without_token_returns_400(client):
    """POST /api/auth/login without requesting a token first returns 400."""
    response = client.post("/api/auth/login", json={"token": "123456"})
    assert response.status_code == 400


def test_analyse_validates_date_range(client):
    """POST /api/analyse with end_date before start_date returns 422."""
    response = client.post(
        "/api/analyse",
        json={
            "start_date": "2025-04-30",
            "end_date": "2025-04-01",
            "battery_capacity_kwh": 15.0,
        },
    )
    assert response.status_code == 422
