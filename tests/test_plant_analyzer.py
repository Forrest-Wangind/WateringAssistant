"""AI 植物分析单元测试。"""
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock

import pytest

from ai.plant_analyzer import PlantAnalyzer
from hardware.camera import Camera


def test_analyzer_mock_report(mock_config: Dict[str, Any], temp_dirs: Dict[str, Path], mock_env):
    """无 API key 时走 mock 报告。"""
    analyzer = PlantAnalyzer(mock_config["ai"], str(temp_dirs["report_dir"]))
    # 先生成一张照片
    cam = Camera(str(temp_dirs["photo_dir"]), mock=True, warmup_sec=0)
    photo = cam.capture("garden")
    cam.close()

    report = analyzer.analyze(photo)
    assert report.photo_path == photo
    assert report.report_path.exists()
    assert "整体观感" in report.markdown
    assert "浇水建议" in report.markdown
    assert report.score == 4  # mock 报告固定 4/5


def test_analyzer_extract_score():
    """评分提取。"""
    from ai.plant_analyzer import PlantAnalyzer

    assert PlantAnalyzer._extract_score("综合健康评分：3 / 5") == 3
    assert PlantAnalyzer._extract_score("综合健康评分 5/5") == 5
    assert PlantAnalyzer._extract_score("没有评分") is None


def test_analyzer_with_mocked_anthropic(mock_config: Dict[str, Any], temp_dirs: Dict[str, Path], monkeypatch):
    """mock Anthropic 客户端，验证调用参数。"""
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_block = MagicMock()
    mock_block.type = "text"
    mock_block.text = "## 测试报告\n\n综合健康评分：5 / 5"
    mock_response.content = [mock_block]
    mock_client.messages.create.return_value = mock_response

    # 注入 API key 让它不走 mock 分支
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
    analyzer = PlantAnalyzer(mock_config["ai"], str(temp_dirs["report_dir"]))
    analyzer._client = mock_client

    cam = Camera(str(temp_dirs["photo_dir"]), mock=True, warmup_sec=0)
    photo = cam.capture()
    cam.close()

    report = analyzer.analyze(photo)
    assert report.score == 5
    assert "测试报告" in report.markdown

    # 验证 Anthropic API 被调用
    mock_client.messages.create.assert_called_once()
    call_kwargs = mock_client.messages.create.call_args.kwargs
    assert call_kwargs["model"] == "claude-opus-4-7"
    assert call_kwargs["max_tokens"] == 1500
    assert len(call_kwargs["messages"]) == 1
    assert call_kwargs["messages"][0]["role"] == "user"
    content = call_kwargs["messages"][0]["content"]
    assert any(c["type"] == "image" for c in content)
    assert any(c["type"] == "text" for c in content)
