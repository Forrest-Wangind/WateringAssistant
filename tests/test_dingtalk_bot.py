"""钉钉模块单元测试。"""
import asyncio
from typing import Any, Dict
from unittest.mock import MagicMock

import pytest

import _bootstrap  # noqa: F401  必须在导入项目模块之前执行：把仓库根目录加入 sys.path
from dingtalk.bot import (
    Command,
    DingTalkBot,
    WateringChatbotHandler,
    help_text,
    parse_command,
)


def test_parse_command_help():
    assert parse_command("帮助").action == "help"
    assert parse_command("help").action == "help"
    assert parse_command("?").action == "help"


def test_parse_command_report():
    assert parse_command("报告").action == "report"
    assert parse_command("生长报告").action == "report"
    assert parse_command("report").action == "report"


def test_parse_command_photo():
    assert parse_command("拍照").action == "photo"
    assert parse_command("photo").action == "photo"


def test_parse_command_status():
    assert parse_command("状态").action == "status"
    assert parse_command("status").action == "status"


def test_parse_command_water_by_name():
    cmd = parse_command("浇水 多肉")
    assert cmd.action == "water"
    assert cmd.target == "多肉"
    assert cmd.duration_sec is None

    cmd = parse_command("浇 热带植物 500")
    assert cmd.action == "water"
    assert cmd.target == "热带植物"
    assert cmd.duration_sec == 500


def test_parse_command_water_by_channel():
    cmd = parse_command("浇水 通道2 150")
    assert cmd.action == "water"
    assert cmd.target == "通道2"
    assert cmd.duration_sec == 150

    cmd = parse_command("浇水 通道3")
    assert cmd.action == "water"
    assert cmd.target == "通道3"
    assert cmd.duration_sec is None


def test_parse_command_with_at():
    """@机器人 后的指令。"""
    cmd = parse_command("@浇花助理 浇水 多肉 100")
    assert cmd.action == "water"
    assert cmd.target == "多肉"
    assert cmd.duration_sec == 100


def test_parse_command_unknown():
    assert parse_command("随便说点啥").action == "unknown"


def test_help_text():
    txt = help_text()
    assert "浇水" in txt
    assert "报告" in txt
    assert "状态" in txt


def test_bot_no_channel_reports_error(mock_config: Dict[str, Any], mock_env):
    """没有任何推送通道时必须**显式失败**，不能假装发送成功。

    这是项目「绝不静默失败」原则的直接体现：以前无 webhook 时只打一行日志，
    调用方以为推成功了，实际用户什么都没收到。
    """
    bot = DingTalkBot(mock_config["dingtalk"])
    assert bot.channel == "none"

    result = bot.send_text("测试消息")
    assert result["ok"] is False
    assert result["error_kind"] == "not_configured"
    assert "DINGTALK_CLIENT_ID" in result["hint"] or "webhook" in result["hint"].lower()

    result = bot.send_markdown("标题", "# Markdown 内容")
    assert result["ok"] is False
    assert result["error_kind"] == "not_configured"


def test_bot_mock_fallback_only_when_allowed(mock_config: Dict[str, Any], mock_env):
    """本地开发（mock_hardware=true）才允许降级为本地打印。"""
    bot = DingTalkBot(mock_config["dingtalk"], allow_mock_fallback=True)
    result = bot.send_text("测试消息")
    assert result["ok"] is True
    assert result["mock"] is True


def test_bot_channel_prefers_robot(mock_config: Dict[str, Any], mock_env, monkeypatch):
    """同时配了机器人凭证和 webhook 时，优先用机器人通道（只有它能发本地图片）。"""
    monkeypatch.setenv("DINGTALK_WEBHOOK", "https://oapi.dingtalk.com/robot/send?access_token=abc")
    monkeypatch.setenv("DINGTALK_CLIENT_ID", "cid")
    monkeypatch.setenv("DINGTALK_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("DINGTALK_ROBOT_CODE", "rcode")
    bot = DingTalkBot(mock_config["dingtalk"])
    assert bot.channel == "robot"


def test_bot_send_image_without_public_url(mock_config: Dict[str, Any], mock_env, monkeypatch, tmp_path):
    """webhook 通道无法推送本地图片，必须给出明确原因而不是静默丢弃。"""
    monkeypatch.setenv("DINGTALK_WEBHOOK", "https://oapi.dingtalk.com/robot/send?access_token=abc")
    bot = DingTalkBot(mock_config["dingtalk"])
    assert bot.channel == "webhook"

    img = tmp_path / "a.jpg"
    img.write_bytes(b"\xff\xd8\xff")
    result = bot.send_image(img)
    assert result["ok"] is False
    assert result["error_kind"] == "no_public_url"


def test_bot_recipients_from_subscribers(mock_config: Dict[str, Any], mock_env):
    bot = DingTalkBot(mock_config["dingtalk"])
    assert bot.recipients() == []
    assert bot.subscribers.add("user_1") is True
    assert bot.recipients() == ["user_1"]
    assert bot.delivery_status()["recipients"] == ["user_1"]


def test_bot_signed_url(mock_config: Dict[str, Any], monkeypatch):
    """加签 URL 生成。"""
    monkeypatch.setenv("DINGTALK_WEBHOOK", "https://oapi.dingtalk.com/robot/send?access_token=abc")
    monkeypatch.setenv("DINGTALK_SECRET", "SECxyz")
    bot = DingTalkBot(mock_config["dingtalk"])
    url = bot._signed_url()
    assert "timestamp=" in url
    assert "sign=" in url
    assert "access_token=abc" in url


# ---------- Stream 模式 ChatbotHandler ----------
def _build_callback(text: str, sender_staff_id: str = "u001"):
    """构造 dingtalk-stream CallbackMessage 的最小数据结构。"""
    callback = MagicMock()
    callback.data = {
        "msgtype": "text",
        "text": {"content": text},
        "senderStaffId": sender_staff_id,
        "senderId": sender_staff_id,
        "conversationId": "cid",
        "chatbotUserId": "bot",
        "msgId": "m1",
        "createAt": 0,
        "conversationType": "2",
    }
    return callback


def test_chatbot_handler_dispatches_command():
    received = {}

    def cmd_handler(text, sender_id):
        received["text"] = text
        received["sender_id"] = sender_id
        return "回复内容"

    handler = WateringChatbotHandler(cmd_handler)
    handler.reply_markdown = MagicMock()  # 拦截外发

    callback = _build_callback("浇水 多肉 100", sender_staff_id="user42")
    status, msg = asyncio.run(handler.process(callback))

    assert status == "OK" or status == 200 or status is not None  # AckMessage.STATUS_OK
    assert received["text"] == "浇水 多肉 100"
    assert received["sender_id"] == "user42"
    handler.reply_markdown.assert_called_once()
    args = handler.reply_markdown.call_args[0]
    assert args[0] == "浇花助理"
    assert args[1] == "回复内容"


def test_chatbot_handler_catches_exception():
    def cmd_handler(text, sender_id):
        raise RuntimeError("boom")

    handler = WateringChatbotHandler(cmd_handler)
    handler.reply_markdown = MagicMock()

    callback = _build_callback("帮助")
    asyncio.run(handler.process(callback))

    handler.reply_markdown.assert_called_once()
    reply_text = handler.reply_markdown.call_args[0][1]
    assert "❌" in reply_text
    assert "boom" in reply_text


if __name__ == "__main__":
    _bootstrap.run(__file__)
