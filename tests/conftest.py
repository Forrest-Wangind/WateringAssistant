"""pytest fixtures 与共享配置。"""
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, Generator

import pytest
import yaml


@pytest.fixture
def temp_dirs() -> Generator[Dict[str, Path], None, None]:
    """临时目录：photo_dir / report_dir / data_dir。"""
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        dirs = {
            "data_dir": base / "data",
            "photo_dir": base / "data" / "photos",
            "report_dir": base / "data" / "reports",
        }
        for d in dirs.values():
            d.mkdir(parents=True, exist_ok=True)
        yield dirs


@pytest.fixture
def mock_config(temp_dirs: Dict[str, Path]) -> Dict[str, Any]:
    """标准 mock 配置：4 通道 + mock_hardware=True + 临时目录。"""
    return {
        "system": {
            "log_level": "WARNING",
            "data_dir": str(temp_dirs["data_dir"]),
            "photo_dir": str(temp_dirs["photo_dir"]),
            "report_dir": str(temp_dirs["report_dir"]),
            "timezone": "Asia/Shanghai",
            "mock_hardware": True,
        },
        "channels": [
            {
                "id": 1,
                "name": "热带植物",
                "pin": 17,
                "bucket": "桶A",
                "default_duration_sec": 45,
                "schedule_cron": "0 7 * * *",
            },
            {
                "id": 2,
                "name": "多肉植物",
                "pin": 27,
                "bucket": "桶B",
                "default_duration_sec": 7,
                "schedule_cron": "0 8 */7 * *",
            },
            {
                "id": 3,
                "name": "中生植物",
                "pin": 22,
                "bucket": "桶C",
                "default_duration_sec": 28,
                "schedule_cron": "0 7 */2 * *",
            },
            {
                "id": 4,
                "name": "水生植物",
                "pin": 23,
                "bucket": "桶D",
                "default_duration_sec": 83,
                "schedule_cron": "0 9 */3 * *",
            },
        ],
        "safety": {
            "max_single_run_sec": 180,
            "cooldown_sec": 30,
            "global_lock": True,
        },
        "camera": {
            "resolution": [1920, 1080],
            "warmup_sec": 0.1,
        },
        "ai": {
            "provider": "anthropic",
            "model": "claude-opus-4-7",
            "api_key_env": "ANTHROPIC_API_KEY",
            "max_tokens": 1500,
            "prompt": "请分析植物生长状况",
        },
        "dingtalk": {
            "webhook_env": "DINGTALK_WEBHOOK",
            "secret_env": "DINGTALK_SECRET",
            "client_id_env": "DINGTALK_CLIENT_ID",
            "client_secret_env": "DINGTALK_CLIENT_SECRET",
            "allowed_users": [],
        },
        "web": {
            "host": "0.0.0.0",
            "port": 8080,
        },
    }


@pytest.fixture
def mock_env(monkeypatch) -> None:
    """清空敏感环境变量，避免测试意外调用真实 API。"""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("DINGTALK_WEBHOOK", raising=False)
    monkeypatch.delenv("DINGTALK_SECRET", raising=False)
    monkeypatch.delenv("DINGTALK_CLIENT_ID", raising=False)
    monkeypatch.delenv("DINGTALK_CLIENT_SECRET", raising=False)


@pytest.fixture
def config_file(tmp_path: Path, mock_config: Dict[str, Any]) -> Path:
    """写临时 config.yaml，返回路径。"""
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.dump(mock_config), encoding="utf-8")
    return cfg_path
