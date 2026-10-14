import pytest
from fastapi.testclient import TestClient
from pyapp.main import app

client = TestClient(app)

def test_health_endpoint():
    res = client.get("/api/health")
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "ok"
    assert "services" in data

def test_gemini_health_endpoint():
    res = client.get("/api/health/gemini")
    assert res.status_code == 200
    data = res.json()
    assert data["status"] in ["ok", "disabled", "not_configured"]

def test_stats_endpoint():
    res = client.get("/api/stats")
    assert res.status_code == 200
    data = res.json()
    assert "totalVideos" in data
    assert "totalTrades" in data

def test_web_routes():
    routes = [
        "/",
        "/videos",
        "/trades",
        "/charts",
        "/backtesting",
        "/playbook",
        "/patterns",
        "/chat",
        "/knowledge",
        "/processing",
        "/settings"
    ]
    for r in routes:
        res = client.get(r)
        assert res.status_code == 200, f"Failed on route {r}"
