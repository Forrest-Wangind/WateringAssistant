"""业务编排集成测试。"""
from typing import Any, Dict

import pytest

import _bootstrap  # noqa: F401  必须在导入项目模块之前执行：把仓库根目录加入 sys.path
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
    """测试环境没有 AI Key —— 报告必须被显式标注为降级，而不是冒充真实分析。"""
    assistant = WateringAssistant(mock_config)
    report = assistant.take_photo_and_report(push=False)
    assert report.photo_path.exists()
    assert report.report_path.exists()
    # 无 Key → 占位报告：评分置空、抬头带 MOCK 横幅（假分数会顺着钉钉标题传出去）
    assert report.degraded is True
    assert report.score is None
    assert "MOCK" in report.markdown
    assistant.shutdown()


# ---------- 每日照片 + 报告推送 ----------
def _spy(bot, calls):
    """记录推送调用顺序。"""
    bot.send_image = lambda path, title="花园照片": (
        calls.append(("image", str(path), title)) or {"ok": True})
    bot.send_text = lambda content, at_mobiles=None: (
        calls.append(("text", content)) or {"ok": True})
    bot.send_markdown = lambda title, md: (
        calls.append(("markdown", title, md)) or {"ok": True})


def test_daily_push_sends_photo_before_report(mock_config: Dict[str, Any], mock_env):
    """照片必须先于报告送达 —— AI 分析要几十秒，用户先看到画面。"""
    assistant = WateringAssistant(mock_config)
    calls = []
    _spy(assistant.bot, calls)
    assistant.take_photo_and_report(push=True)

    kinds = [c[0] for c in calls]
    assert kinds == ["image", "text", "markdown"]
    assert calls[0][1].endswith(".jpg")
    assert "花园生长报告" in calls[-1][2]
    assistant.shutdown()


def test_push_false_sends_nothing(mock_config: Dict[str, Any], mock_env):
    assistant = WateringAssistant(mock_config)
    calls = []
    _spy(assistant.bot, calls)
    assistant.take_photo_and_report(push=False)
    assert calls == []
    assistant.shutdown()


def test_photo_push_failure_is_surfaced_in_report(mock_config: Dict[str, Any], mock_env):
    """照片没送达这件事必须出现在报告抬头，不能悄悄吞掉。"""
    assistant = WateringAssistant(mock_config)
    calls = []
    _spy(assistant.bot, calls)
    assistant.bot.send_image = lambda path, title="花园照片": {
        "ok": False, "error_kind": "no_session",
        "errmsg": "no oto session", "hint": "先给机器人发一条消息",
    }
    report = assistant.take_photo_and_report(push=True)

    assert "推送失败:no_session" in report.markdown
    assert "先给机器人发一条消息" in report.markdown
    assistant.shutdown()


def test_send_photo_can_be_disabled(mock_config: Dict[str, Any], mock_env):
    mock_config["push"]["send_photo"] = False
    assistant = WateringAssistant(mock_config)
    calls = []
    _spy(assistant.bot, calls)
    assistant.take_photo_and_report(push=True)
    assert [c[0] for c in calls] == ["text", "markdown"]
    assistant.shutdown()


def test_subscribe_command_registers_sender(mock_config: Dict[str, Any], mock_env, monkeypatch):
    monkeypatch.setenv("DINGTALK_CLIENT_ID", "cid")
    monkeypatch.setenv("DINGTALK_CLIENT_SECRET", "cs")
    monkeypatch.setenv("DINGTALK_ROBOT_CODE", "rc")
    assistant = WateringAssistant(mock_config)
    assert assistant.bot.channel == "robot"

    reply = assistant.handle_command("订阅", sender_id="u_abc")
    assert "已开启每日推送" in reply
    assert "13:00" in reply

    # 重复订阅不产生重复项
    assistant.handle_command("订阅", sender_id="u_abc")
    assert assistant.bot.recipients() == ["u_abc"]
    assistant.shutdown()


def test_subscribe_without_channel_says_so(mock_config: Dict[str, Any], mock_env):
    """通道没配好时，订阅也不能报"成功"，否则用户以为能收到。"""
    assistant = WateringAssistant(mock_config)
    reply = assistant.handle_command("订阅", sender_id="u_abc")
    assert "已记录你的账号" in reply
    assert "尚未配置" in reply
    assert assistant.bot.recipients() == ["u_abc"]
    assistant.shutdown()


def test_status_shows_push_channel_warning(mock_config: Dict[str, Any], mock_env):
    assistant = WateringAssistant(mock_config)
    txt = assistant.status_text()
    assert "推送" in txt
    assert "未配置" in txt
    assert "发不出去" in txt
    assistant.shutdown()


def test_notify_failure_pushes_message(mock_config: Dict[str, Any], mock_env):
    """定时任务失败要把原因告诉用户，否则用户只会觉得"今天没收到报告"。"""
    assistant = WateringAssistant(mock_config)
    calls = []
    _spy(assistant.bot, calls)
    assistant.notify_failure("每日照片/报告任务失败", RuntimeError("相机不可用"))
    assert len(calls) == 1
    assert "相机不可用" in calls[0][1]
    assistant.shutdown()


if __name__ == "__main__":
    _bootstrap.run(__file__)
