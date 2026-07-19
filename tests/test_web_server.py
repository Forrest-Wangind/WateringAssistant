"""Web API 测试。"""
from typing import Any, Dict

from fastapi.testclient import TestClient

from assistant import WateringAssistant
from web.server import create_app


def test_health(mock_config: Dict[str, Any], mock_env):
    assistant = WateringAssistant(mock_config)
    app = create_app(assistant)
    client = TestClient(app)
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assistant.shutdown()


def test_api_status(mock_config: Dict[str, Any], mock_env):
    assistant = WateringAssistant(mock_config)
    app = create_app(assistant)
    client = TestClient(app)
    r = client.get("/api/status")
    assert r.status_code == 200
    data = r.json()
    assert "channels" in data
    assert len(data["channels"]) == 4
    assistant.shutdown()


def test_api_water(mock_config: Dict[str, Any], mock_env):
    assistant = WateringAssistant(mock_config)
    app = create_app(assistant)
    client = TestClient(app)
    r = client.post("/api/water", json={"channel_id": 2, "duration_sec": 10})
    assert r.status_code == 200
    data = r.json()
    assert data["channel_id"] == 2
    assert data["duration_sec"] == 10
    assistant.shutdown()


def test_api_water_invalid_channel(mock_config: Dict[str, Any], mock_env):
    assistant = WateringAssistant(mock_config)
    app = create_app(assistant)
    client = TestClient(app)
    r = client.post("/api/water", json={"channel_id": 99})
    assert r.status_code == 400
    assert "未知通道" in r.json()["detail"]
    assistant.shutdown()


def test_api_report(mock_config: Dict[str, Any], mock_env):
    assistant = WateringAssistant(mock_config)
    app = create_app(assistant)
    client = TestClient(app)
    r = client.post("/api/report")
    assert r.status_code == 200
    data = r.json()
    assert "photo" in data
    assert "report" in data
    assert "markdown" in data
    assert data["score"] is not None
    assistant.shutdown()


def test_api_photo(mock_config: Dict[str, Any], mock_env):
    assistant = WateringAssistant(mock_config)
    app = create_app(assistant)
    client = TestClient(app)
    r = client.post("/api/photo")
    assert r.status_code == 200
    data = r.json()
    assert "photo" in data
    assistant.shutdown()
