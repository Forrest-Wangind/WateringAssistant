"""Web API 测试。"""
from typing import Any, Dict

from fastapi.testclient import TestClient

import _bootstrap  # noqa: F401  必须在导入项目模块之前执行：把仓库根目录加入 sys.path
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
    # 测试环境无 AI Key → 报告降级：score 为 null，markdown 带 MOCK 横幅
    assert data["score"] is None
    assert "MOCK" in data["markdown"]
    assert data["degraded"] is True
    assistant.shutdown()


def test_api_report_ai_unavailable_returns_503(mock_config: Dict[str, Any], mock_env,
                                               monkeypatch):
    """AI 不可用（未开降级）时要显式报错，而不是返回一份占位报告。"""
    from ai.errors import AIError

    assistant = WateringAssistant(mock_config)

    def boom(_photo):
        raise AIError("未设置环境变量 DEEPSEEK_API_KEY", kind="no_api_key")

    monkeypatch.setattr(assistant.analyzer, "analyze", boom)
    app = create_app(assistant)
    client = TestClient(app)
    r = client.post("/api/report")

    assert r.status_code == 503
    detail = r.json()["detail"]
    assert detail["kind"] == "no_api_key"
    assert detail["hint"]
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


if __name__ == "__main__":
    _bootstrap.run(__file__)
