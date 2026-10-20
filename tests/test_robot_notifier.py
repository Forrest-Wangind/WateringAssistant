"""企业机器人推送模块单元测试。

不触网：全部通过替换 httpx 的 post/get 来验证请求构造与错误分类。
"""
import json
from typing import Any, Dict, List

import pytest

import _bootstrap  # noqa: F401  必须在导入项目模块之前执行
from dingtalk import robot_notifier as rn
from dingtalk.robot_notifier import PushError, RobotNotifier, classify_api_error


class FakeResp:
    def __init__(self, status_code: int = 200, payload: Any = None, text: str = ""):
        self.status_code = status_code
        self._payload = payload
        self.text = text or (json.dumps(payload) if payload is not None else "")
        self.content = self.text.encode() or b"{}"

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


@pytest.fixture
def robot(mock_config: Dict[str, Any], mock_env, monkeypatch) -> RobotNotifier:
    monkeypatch.setenv("DINGTALK_CLIENT_ID", "cid")
    monkeypatch.setenv("DINGTALK_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("DINGTALK_ROBOT_CODE", "rcode")
    return RobotNotifier(mock_config["dingtalk"])


def install_http(monkeypatch, handler) -> List[Dict[str, Any]]:
    """替换 httpx.post / httpx.get，记录所有请求。"""
    calls: List[Dict[str, Any]] = []

    def fake_post(url, **kw):
        calls.append({"method": "POST", "url": url, **kw})
        return handler(url, kw)

    def fake_get(url, **kw):
        calls.append({"method": "GET", "url": url, **kw})
        return handler(url, kw)

    monkeypatch.setattr(rn.httpx, "post", fake_post)
    monkeypatch.setattr(rn.httpx, "get", fake_get)
    return calls


TOKEN_RESP = FakeResp(200, {"accessToken": "tok-123", "expireIn": 7200})
LEGACY_RESP = FakeResp(200, {"errcode": 0, "access_token": "legacy-456", "expires_in": 7200})


def happy_handler(calls_seen):
    def handler(url, kw):
        if rn.TOKEN_URL in url:
            return TOKEN_RESP
        if rn.LEGACY_TOKEN_URL in url:
            return LEGACY_RESP
        if rn.UPLOAD_URL in url:
            return FakeResp(200, {"errcode": 0, "errmsg": "ok", "media_id": "@lADPfake"})
        if rn.OTO_SEND_URL in url:
            return FakeResp(200, {"processQueryKey": "pqk1", "invalidStaffIdList": []})
        raise AssertionError(f"未预期的请求: {url}")
    return handler


# ---------- 配置 ----------
def test_configured_and_missing_credentials(mock_config, mock_env, monkeypatch):
    monkeypatch.delenv("DINGTALK_CLIENT_ID", raising=False)
    r = RobotNotifier(mock_config["dingtalk"])
    assert r.configured is False
    assert "DINGTALK_CLIENT_ID" in r.missing_credentials()

    monkeypatch.setenv("DINGTALK_CLIENT_ID", "cid")
    monkeypatch.setenv("DINGTALK_CLIENT_SECRET", "s")
    monkeypatch.setenv("DINGTALK_ROBOT_CODE", "rc")
    assert RobotNotifier(mock_config["dingtalk"]).configured is True


def test_send_without_credentials_raises_not_configured(mock_config, mock_env):
    r = RobotNotifier(mock_config["dingtalk"])
    res = r.send_text("hi", ["u1"])
    assert res["ok"] is False
    assert res["error_kind"] == "not_configured"


def test_send_without_recipients(robot):
    res = robot.send_text("hi", [])
    assert res["ok"] is False
    assert res["error_kind"] == "no_recipient"
    assert "钉钉" in res["hint"] or "user" in res["hint"].lower()


# ---------- token ----------
def test_token_cached(robot, monkeypatch):
    calls = install_http(monkeypatch, happy_handler(None))
    assert robot.access_token() == "tok-123"
    assert robot.access_token() == "tok-123"
    token_calls = [c for c in calls if rn.TOKEN_URL in c["url"]]
    assert len(token_calls) == 1, "token 应被缓存，不应重复请求"


def test_token_network_error_is_unreachable(robot, monkeypatch):
    def handler(url, kw):
        raise rn.httpx.ConnectError("boom")
    install_http(monkeypatch, handler)
    with pytest.raises(PushError) as ei:
        robot.access_token()
    assert ei.value.kind == "unreachable"


def test_token_bad_credentials(robot, monkeypatch):
    install_http(monkeypatch, lambda url, kw: FakeResp(400, {"code": "invalidParameter", "message": "bad appkey"}))
    res = robot.send_text("hi", ["u1"])
    assert res["ok"] is False
    assert res["error_kind"] in ("unreachable", "bad_config")


# ---------- 上传图片 ----------
def test_upload_image_success(robot, monkeypatch, tmp_path):
    calls = install_http(monkeypatch, happy_handler(None))
    img = tmp_path / "garden.jpg"
    img.write_bytes(b"\xff\xd8\xff" + b"x" * 100)
    media_id = robot.upload_image(img)
    assert media_id == "@lADPfake"

    upload = [c for c in calls if rn.UPLOAD_URL in c["url"]][0]
    assert upload["params"]["type"] == "image"      # type 必须在 query 上
    assert upload["params"]["access_token"] == "legacy-456"  # 用旧版 token
    assert "media" in upload["files"]


def test_upload_image_missing_file(robot):
    res = robot.send_image("C:/definitely/not/here.jpg", ["u1"])
    assert res["ok"] is False
    assert res["error_kind"] == "not_found"


def test_upload_image_too_large(robot, monkeypatch, tmp_path):
    img = tmp_path / "big.jpg"
    img.write_bytes(b"x" * 16)
    monkeypatch.setattr(rn, "MAX_IMAGE_BYTES", 8)
    with pytest.raises(PushError) as ei:
        robot.upload_image(img)
    assert ei.value.kind == "too_large"


# ---------- 发送 ----------
def test_send_image_uses_media_id_as_photo_url(robot, monkeypatch, tmp_path):
    calls = install_http(monkeypatch, happy_handler(None))
    img = tmp_path / "garden.jpg"
    img.write_bytes(b"\xff\xd8\xff" + b"x" * 50)

    res = robot.send_image(img, ["u1", "u2"], title="今日花园")
    assert res["ok"] is True
    assert res["sent"] == 2

    send = [c for c in calls if rn.OTO_SEND_URL in c["url"]][0]
    assert send["json"]["msgKey"] == "sampleImageMsg"
    assert send["json"]["robotCode"] == "rcode"
    assert send["json"]["userIds"] == ["u1", "u2"]
    # 关键：photoURL 直接用 mediaId，无需公网 URL
    assert json.loads(send["json"]["msgParam"]) == {"photoURL": "@lADPfake"}
    assert send["headers"]["x-acs-dingtalk-access-token"] == "tok-123"


def test_send_markdown_payload(robot, monkeypatch):
    calls = install_http(monkeypatch, happy_handler(None))
    res = robot.send_markdown("🌿 花园生长报告", "# 报告\n\n内容", ["u1"])
    assert res["ok"] is True
    send = [c for c in calls if rn.OTO_SEND_URL in c["url"]][0]
    assert send["json"]["msgKey"] == "sampleMarkdown"
    param = json.loads(send["json"]["msgParam"])
    assert param["title"] == "🌿 花园生长报告"
    assert "内容" in param["text"]


def test_send_markdown_truncates_long_content(robot, monkeypatch):
    calls = install_http(monkeypatch, happy_handler(None))
    robot.max_markdown_chars = 100
    res = robot.send_markdown("t", "长" * 500, ["u1"])
    assert res["ok"] is True
    send = [c for c in calls if rn.OTO_SEND_URL in c["url"]][0]
    text = json.loads(send["json"]["msgParam"])["text"]
    assert len(text) < 200
    assert "截断" in text


def test_send_batches_over_20_users(robot, monkeypatch):
    calls = install_http(monkeypatch, happy_handler(None))
    users = [f"u{i}" for i in range(45)]
    res = robot.send_markdown("t", "body", users)
    assert res["ok"] is True
    sends = [c for c in calls if rn.OTO_SEND_URL in c["url"]]
    assert len(sends) == 3                      # 20 + 20 + 5
    assert [len(s["json"]["userIds"]) for s in sends] == [20, 20, 5]
    assert res["sent"] == 45


def test_invalid_user_ids_reported_not_silent(robot, monkeypatch):
    def handler(url, kw):
        if rn.TOKEN_URL in url:
            return TOKEN_RESP
        return FakeResp(200, {"processQueryKey": "p", "invalidStaffIdList": ["ghost"]})
    install_http(monkeypatch, handler)
    res = robot.send_markdown("t", "body", ["u1", "ghost"])
    assert res["ok"] is True
    assert res["invalid"] == ["ghost"]
    assert "ghost" in res["warning"]


# ---------- 错误分类 ----------
@pytest.mark.parametrize("code,expect", [
    ("robot.oto.notExist", "no_session"),
    ("session.notExisted", "no_session"),
    ("invalidParameter.robotCode.empty", "bad_config"),
    ("Forbidden.NoPermission", "no_permission"),
    ("invalidParameter.msgParam.tooLong", "too_long"),
])
def test_classify_api_error(code, expect):
    err = classify_api_error(code, "message")
    assert err.kind == expect
    assert err.hint, "每类错误都必须给出可执行的修复建议"


def test_send_api_error_returns_hint(robot, monkeypatch):
    def handler(url, kw):
        if rn.TOKEN_URL in url:
            return TOKEN_RESP
        return FakeResp(400, {"code": "robot.oto.notExist", "message": "no oto session"})
    install_http(monkeypatch, handler)
    res = robot.send_markdown("t", "body", ["u1"])
    assert res["ok"] is False
    assert res["error_kind"] == "no_session"
    assert "机器人" in res["hint"]


def test_401_clears_token_for_retry(robot, monkeypatch):
    def handler(url, kw):
        if rn.TOKEN_URL in url:
            return TOKEN_RESP
        return FakeResp(401, {"code": "invalidParameter.token", "message": "x"})
    install_http(monkeypatch, handler)
    res = robot.send_text("hi", ["u1"])
    assert res["ok"] is False
    assert robot._token is None, "401 后应清空 token 以便下次强制刷新"


def test_unexpected_exception_is_contained(robot, monkeypatch):
    """任何意外异常都不能让定时任务崩掉。"""
    def boom(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(rn.httpx, "post", boom)
    res = robot.send_text("hi", ["u1"])
    assert res["ok"] is False
    assert res["error_kind"] in ("unknown", "unreachable")


if __name__ == "__main__":
    _bootstrap.run(__file__)
