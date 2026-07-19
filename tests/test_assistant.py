"""业务编排集成测试。"""
from typing import Any, Dict

import pytest

from assistant import WateringAssistant


def test_assistant_init(mock_config: Dict[str, Any], mock_env):
    assistant = WateringAssistant(mock_config)
    assert assistant.valve is not None
    assert assistant.camera is not None
    assert assistant.analyzer is not None
    assert assistant.bot is not None
    assistant.shutdown()


def test_handle_command_help(mock_config: Dict[str, Any], mock_env):
    assistant = WateringAssistant(mock_config)
    reply = assistant.handle_command("帮助")
    assert "浇水" in reply
    assert "报告" in reply
    assistant.shutdown()


def test_handle_command_status(mock_config: Dict[str, Any], mock_env):
    assistant = WateringAssistant(mock_config)
    reply = assistant.handle_command("状态")
    assert "热带植物" in reply
    assert "多肉植物" in reply
    assistant.shutdown()


def test_handle_command_photo(mock_config: Dict[str, Any], mock_env):
    assistant = WateringAssistant(mock_config)
    reply = assistant.handle_command("拍照")
    assert "已拍照" in reply
    assistant.shutdown()


def test_handle_command_report(mock_config: Dict[str, Any], mock_env):
    assistant = WateringAssistant(mock_config)
    reply = assistant.handle_command("报告")
    assert "花园生长报告" in reply
    assert "整体观感" in reply
    assistant.shutdown()


def test_handle_command_water_by_name(mock_config: Dict[str, Any], mock_env):
    assistant = WateringAssistant(mock_config)
    reply = assistant.handle_command("浇水 多肉")
    assert "已为" in reply
    assert "多肉植物" in reply
    assistant.shutdown()


def test_handle_command_water_by_channel(mock_config: Dict[str, Any], mock_env):
    assistant = WateringAssistant(mock_config)
    reply = assistant.handle_command("浇水 通道3 30")
    assert "已为" in reply
    assert "中生植物" in reply
    assert "30.0s" in reply
    assistant.shutdown()


def test_handle_command_water_unknown_target(mock_config: Dict[str, Any], mock_env):
    assistant = WateringAssistant(mock_config)
    reply = assistant.handle_command("浇水 不存在的植物")
    assert "找不到" in reply
    assistant.shutdown()


def test_handle_command_unknown(mock_config: Dict[str, Any], mock_env):
    assistant = WateringAssistant(mock_config)
    reply = assistant.handle_command("随便说点啥")
    assert "无法识别" in reply
    assistant.shutdown()


def test_handle_command_whitelist(mock_config: Dict[str, Any], mock_env):
    """白名单限制。"""
    mock_config["dingtalk"]["allowed_users"] = ["user123"]
    assistant = WateringAssistant(mock_config)
    reply = assistant.handle_command("浇水 多肉", sender_id="user999")
    assert "没有权限" in reply

    reply = assistant.handle_command("浇水 多肉", sender_id="user123")
    assert "已为" in reply
    assistant.shutdown()


def test_water_channel_direct(mock_config: Dict[str, Any], mock_env):
    """直接调用 water_channel。"""
    assistant = WateringAssistant(mock_config)
    result = assistant.water_channel(1, duration_sec=30, notify=False)
    assert result["channel_id"] == 1
    assert result["duration_sec"] == 30
    assistant.shutdown()


def test_take_photo_and_report(mock_config: Dict[str, Any], mock_env):
    assistant = WateringAssistant(mock_config)
    report = assistant.take_photo_and_report(push=False)
    assert report.photo_path.exists()
    assert report.report_path.exists()
    assert report.score is not None
    assistant.shutdown()
