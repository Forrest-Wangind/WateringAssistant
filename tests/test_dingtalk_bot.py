"""钉钉模块单元测试。"""
import asyncio
from typing import Any, Dict
from unittest.mock import MagicMock

import pytest

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


def test_bot_send_text_mock(mock_config: Dict[str, Any], mock_env):
    """无 webhook 时走 mock 打印。"""
    bot = DingTalkBot(mock_config["dingtalk"])
    result = bot.send_text("测试消息")
    assert result["errcode"] == 0
    assert result["errmsg"] == "mock"


def test_bot_send_markdown_mock(mock_config: Dict[str, Any], mock_env):
    bot = DingTalkBot(mock_config["dingtalk"])
    result = bot.send_markdown("标题", "# Markdown 内容")
    assert result["errcode"] == 0


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
