"""企业机器人主动推送（单聊）。

为什么不用自定义机器人 webhook
------------------------------
自定义机器人 webhook 只能发到群里，而且 markdown 里的图片必须是**公网可访问**的 URL。
本项目部署在家庭内网、没有公网 IP，照片永远渲染不出来 —— 这正是"每日报告只能看到
一行文件名、看不到照片"的根因。

企业内部应用机器人单聊通道可以完全绕开公网：

  1. 取 accessToken    POST https://api.dingtalk.com/v1.0/oauth2/accessToken
  2. 上传图片           POST https://oapi.dingtalk.com/media/upload?type=image   -> media_id
  3. 发送消息           POST https://api.dingtalk.com/v1.0/robot/oToMessages/batchSend

钉钉官方文档《消息发送与接收类型》明确写道：``sampleImageMsg`` 的 ``photoURL``
「可填写图片的完整 URL 路径，**也可填写图片的 mediaId**」。
所以内网部署也能把照片真正送达，无需图床、无需 AgentId。

前置条件（缺一不可；缺失时显式报错，绝不静默降级）
  * ``DINGTALK_CLIENT_ID`` / ``DINGTALK_CLIENT_SECRET``：企业内部应用 Client ID / Secret
  * ``DINGTALK_ROBOT_CODE``：企业内部机器人编码（机器人 ID）
  * 开发者后台已申请「企业内机器人发送消息权限」
  * **用户必须先和机器人说过话**：单聊机器人不能凭空发起会话，否则钉钉返回
    ``robot.oto.notExist`` / ``session.notExisted``
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import httpx

logger = logging.getLogger(__name__)

TOKEN_URL = "https://api.dingtalk.com/v1.0/oauth2/accessToken"
LEGACY_TOKEN_URL = "https://oapi.dingtalk.com/gettoken"
UPLOAD_URL = "https://oapi.dingtalk.com/media/upload"
OTO_SEND_URL = "https://api.dingtalk.com/v1.0/robot/oToMessages/batchSend"

# 官方限制：单次最多 20 个 userId；图片最大 20MB（jpg/gif/png/bmp）
MAX_USERS_PER_CALL = 20
MAX_IMAGE_BYTES = 20 * 1024 * 1024
# 提前 5 分钟刷新，避免边界失效
_TOKEN_REFRESH_MARGIN = 300
# 钉钉 markdown 消息体过长会直接失败（invalidParameter.msgParam.tooLong），做保守截断
DEFAULT_MAX_MARKDOWN_CHARS = 3500

# 钉钉错误码 -> (分类, 可执行建议)
_ERROR_HINTS = (
    (
        ("robot.oto.notExist", "session.notExisted", "chatbotId.notAllow.sendOTO"),
        "no_session",
        "请先在钉钉客户端里找到该机器人并发送一条消息（例如「帮助」），"
        "建立单聊会话后机器人才能主动推送。",
    ),
    (
        ("invalidParameter.robotCode", "template.not.existed", "template.stopped"),
        "bad_config",
        "DINGTALK_ROBOT_CODE 不正确或机器人已停用，请到开发者后台核对机器人编码与状态。",
    ),
    (
        ("Forbidden", "noPermission", "notPermission", "invalidParameter.token"),
        "no_permission",
        "请到开发者后台为该应用申请「企业内机器人发送消息权限」，并确认已发布。",
    ),
    (
        ("tooLong",),
        "too_long",
        "报告内容超出钉钉限制，已尝试截断；可调小 config.yaml 的 ai.max_tokens。",
    ),
    (
        ("flowControl", "tooFast", "too.fast", "限流"),
        "rate_limited",
        "钉钉限流，稍后重试即可。",
    ),
)


class PushError(Exception):
    """推送失败。``kind`` 与项目其它硬件异常保持同一套分类口径。"""

    def __init__(self, kind: str, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.kind = kind
        self.hint = hint

    def as_dict(self) -> Dict[str, Any]:
        return {"errcode": -1, "errmsg": str(self), "ok": False,
                "error_kind": self.kind, "hint": self.hint}


def classify_api_error(code: str, message: str) -> PushError:
    """把钉钉返回的业务错误码翻译成带修复建议的 PushError。"""
    text = f"{code} {message}"
    for keys, kind, hint in _ERROR_HINTS:
        if any(k.lower() in text.lower() for k in keys):
            return PushError(kind, f"钉钉接口返回: {code} {message}".strip(), hint)
    return PushError("api_error", f"钉钉接口返回: {code} {message}".strip(),
                     "详见钉钉全局错误码文档，或运行 tools/dingtalk_diag.py 排查。")


def _ok(**extra: Any) -> Dict[str, Any]:
    return {"errcode": 0, "errmsg": "ok", "ok": True, **extra}


class RobotNotifier:
    """企业内部机器人单聊推送。所有网络失败都以 PushError 抛出，由调用方转成返回值。"""

    def __init__(self, cfg: Dict[str, Any]) -> None:
        self.cfg = cfg or {}
        self.client_id = os.environ.get(self.cfg.get("client_id_env", "DINGTALK_CLIENT_ID"), "")
        self.client_secret = os.environ.get(
            self.cfg.get("client_secret_env", "DINGTALK_CLIENT_SECRET"), "")
        self.robot_code = os.environ.get(
            self.cfg.get("robot_code_env", "DINGTALK_ROBOT_CODE"), "")
        self.timeout = float(self.cfg.get("push_timeout_sec", 15))
        self.max_markdown_chars = int(
            self.cfg.get("max_markdown_chars", DEFAULT_MAX_MARKDOWN_CHARS))

        self._lock = threading.Lock()
        self._token: Optional[str] = None
        self._token_expire_at = 0.0
        self._legacy_token: Optional[str] = None
        self._legacy_expire_at = 0.0

    # ---------- 可用性 ----------
    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret and self.robot_code)

    def missing_credentials(self) -> List[str]:
        missing = []
        if not self.client_id:
            missing.append(self.cfg.get("client_id_env", "DINGTALK_CLIENT_ID"))
        if not self.client_secret:
            missing.append(self.cfg.get("client_secret_env", "DINGTALK_CLIENT_SECRET"))
        if not self.robot_code:
            missing.append(self.cfg.get("robot_code_env", "DINGTALK_ROBOT_CODE"))
        return missing

    # ---------- token ----------
    def _fetch_token(self) -> str:
        """新版 accessToken（api.dingtalk.com）。"""
        try:
            r = httpx.post(
                TOKEN_URL,
                json={"appKey": self.client_id, "appSecret": self.client_secret},
                timeout=self.timeout,
            )
        except httpx.HTTPError as exc:
            raise PushError("unreachable", f"无法访问钉钉接口: {exc}",
                            "检查树莓派网络/代理是否允许访问 api.dingtalk.com。") from exc
        if r.status_code != 200:
            raise PushError("unreachable", f"取 accessToken 失败 HTTP {r.status_code}: {r.text[:200]}",
                            "检查树莓派网络是否可访问 api.dingtalk.com。")
        data = r.json()
        token = data.get("accessToken")
        if not token:
            raise PushError("bad_config", f"取 accessToken 失败: {json.dumps(data, ensure_ascii=False)[:200]}",
                            "核对 DINGTALK_CLIENT_ID / DINGTALK_CLIENT_SECRET 是否正确。")
        self._token = token
        self._token_expire_at = time.time() + float(data.get("expireIn", 7200))
        return token

    def access_token(self) -> str:
        with self._lock:
            if self._token and time.time() < self._token_expire_at - _TOKEN_REFRESH_MARGIN:
                return self._token
            return self._fetch_token()

    def _fetch_legacy_token(self) -> str:
        """旧版 token（oapi.dingtalk.com），专供 media/upload 使用。"""
        try:
            r = httpx.get(
                LEGACY_TOKEN_URL,
                params={"appkey": self.client_id, "appsecret": self.client_secret},
                timeout=self.timeout,
            )
        except httpx.HTTPError as exc:
            raise PushError("unreachable", f"无法访问钉钉接口: {exc}",
                            "检查树莓派网络/代理是否允许访问 oapi.dingtalk.com。") from exc
        if r.status_code != 200:
            raise PushError("unreachable", f"取旧版 token 失败 HTTP {r.status_code}: {r.text[:200]}")
        data = r.json()
        if int(data.get("errcode", -1)) != 0 or not data.get("access_token"):
            raise PushError("bad_config", f"取旧版 token 失败: {json.dumps(data, ensure_ascii=False)[:200]}",
                            "核对 DINGTALK_CLIENT_ID / DINGTALK_CLIENT_SECRET。")
        self._legacy_token = data["access_token"]
        self._legacy_expire_at = time.time() + float(data.get("expires_in", 7200))
        return self._legacy_token

    def legacy_access_token(self) -> str:
        with self._lock:
            if self._legacy_token and time.time() < self._legacy_expire_at - _TOKEN_REFRESH_MARGIN:
                return self._legacy_token
            return self._fetch_legacy_token()

    # ---------- 上传图片 ----------
    def upload_image(self, image_path: str | os.PathLike[str]) -> str:
        """上传图片，返回 media_id（可直接作为 sampleImageMsg 的 photoURL）。"""
        path = Path(image_path)
        if not path.exists():
            raise PushError("not_found", f"图片不存在: {path}", "先执行拍照再推送。")
        size = path.stat().st_size
        if size > MAX_IMAGE_BYTES:
            raise PushError("too_large", f"图片 {size / 1e6:.1f}MB 超过钉钉 20MB 上限",
                            "调小 config.yaml 的 camera.resolution 或 jpeg_quality。")
        token = self.legacy_access_token()
        try:
            with path.open("rb") as fp:
                r = httpx.post(
                    UPLOAD_URL,
                    # 官方提示：type 放在 query 上最稳（部分语言会因 body 参数报 40004）
                    params={"access_token": token, "type": "image"},
                    files={"media": (path.name, fp.read(), "image/jpeg")},
                    timeout=self.timeout,
                )
        except httpx.HTTPError as exc:
            raise PushError("unreachable", f"上传图片失败: {exc}",
                            "检查树莓派网络是否可访问 oapi.dingtalk.com。") from exc
        try:
            data = r.json()
        except ValueError as exc:
            raise PushError("api_error", f"上传图片返回非 JSON（HTTP {r.status_code}）: {r.text[:200]}") from exc
        if int(data.get("errcode", -1)) != 0:
            raise classify_api_error(str(data.get("errcode")), str(data.get("errmsg", "")))
        media_id = data.get("media_id")
        if not media_id:
            raise PushError("api_error", f"上传图片未返回 media_id: {json.dumps(data, ensure_ascii=False)[:200]}")
        logger.info("图片已上传钉钉媒体库 media_id=%s… 大小=%.1fKB", str(media_id)[:12], size / 1024)
        return str(media_id)

    # ---------- 发送 ----------
    def _send(self, user_ids: Iterable[str], msg_key: str, msg_param: str) -> Dict[str, Any]:
        if not self.configured:
            raise PushError(
                "not_configured",
                f"机器人推送未配置，缺少: {', '.join(self.missing_credentials())}",
                "在 .env 里补齐 DINGTALK_CLIENT_ID / DINGTALK_CLIENT_SECRET / DINGTALK_ROBOT_CODE。",
            )
        ids = [str(u).strip() for u in user_ids if str(u).strip()]
        if not ids:
            raise PushError(
                "no_recipient",
                "没有可推送的接收人（userId 列表为空）",
                "先在钉钉里给机器人发一条消息（会自动记录你的 userId），"
                "或在 config.yaml 的 push.user_ids 里直接填写。",
            )
        token = self.access_token()
        headers = {"x-acs-dingtalk-access-token": token, "Content-Type": "application/json"}

        sent, invalid, throttled = 0, [], []
        for i in range(0, len(ids), MAX_USERS_PER_CALL):
            batch = ids[i:i + MAX_USERS_PER_CALL]
            body = {"robotCode": self.robot_code, "userIds": batch,
                    "msgKey": msg_key, "msgParam": msg_param}
            try:
                r = httpx.post(OTO_SEND_URL, headers=headers, json=body, timeout=self.timeout)
            except httpx.HTTPError as exc:
                raise PushError("unreachable", f"发送消息失败: {exc}",
                                "检查树莓派网络是否可访问 api.dingtalk.com。") from exc

            if r.status_code == 401:
                self._token = None  # 令牌失效，下次强制刷新
                raise PushError("bad_config", "钉钉拒绝访问（401），accessToken 无效",
                                "核对 Client ID / Secret，并确认应用已发布。")
            if r.status_code >= 400:
                try:
                    err = r.json()
                except ValueError:
                    raise PushError("api_error", f"发送失败 HTTP {r.status_code}: {r.text[:200]}")
                code = str(err.get("code") or err.get("errcode") or r.status_code)
                message = str(err.get("message") or err.get("errmsg") or r.text[:200])
                raise classify_api_error(code, message)

            data = r.json() if r.content else {}
            invalid.extend(data.get("invalidStaffIdList") or [])
            throttled.extend(data.get("flowControlledStaffIdList") or [])
            sent += len(batch) - len(data.get("invalidStaffIdList") or [])

        result = _ok(msg_key=msg_key, sent=sent, invalid=invalid, throttled=throttled)
        if invalid:
            # 有接收人无效：不静默，明确回传
            result["warning"] = f"以下 userId 无效已跳过: {', '.join(invalid)}"
            logger.warning(result["warning"])
        if throttled:
            result["warning"] = f"以下 userId 被限流: {', '.join(throttled)}"
            logger.warning(result["warning"])
        return result

    def send_markdown(self, title: str, markdown: str, user_ids: Iterable[str]) -> Dict[str, Any]:
        text = markdown
        if len(text) > self.max_markdown_chars:
            text = text[: self.max_markdown_chars] + "\n\n…（内容过长已截断，完整报告见 data/reports/）"
            logger.warning("报告 %d 字超出限制，已截断到 %d 字", len(markdown), self.max_markdown_chars)
        param = json.dumps({"title": title, "text": text}, ensure_ascii=False)
        return self._safe(lambda: self._send(user_ids, "sampleMarkdown", param))

    def send_text(self, content: str, user_ids: Iterable[str]) -> Dict[str, Any]:
        param = json.dumps({"content": content}, ensure_ascii=False)
        return self._safe(lambda: self._send(user_ids, "sampleText", param))

    def send_image(self, image_path: str | os.PathLike[str],
                   user_ids: Iterable[str], title: str = "花园照片") -> Dict[str, Any]:
        """上传图片并发送 sampleImageMsg（photoURL 用 mediaId，无需公网 URL）。"""
        def _run() -> Dict[str, Any]:
            media_id = self.upload_image(image_path)
            param = json.dumps({"photoURL": media_id}, ensure_ascii=False)
            res = self._send(user_ids, "sampleImageMsg", param)
            res["media_id"] = media_id
            return res

        return self._safe(_run)

    @staticmethod
    def _safe(fn) -> Dict[str, Any]:
        """把 PushError 转成统一返回值，避免定时任务因推送失败而崩掉整个流程。"""
        try:
            return fn()
        except PushError as exc:
            logger.error("钉钉推送失败 [%s]: %s | 建议: %s", exc.kind, exc, exc.hint)
            return exc.as_dict()
        except Exception as exc:  # noqa: BLE001
            logger.exception("钉钉推送出现未预期异常")
            return PushError("unknown", f"未预期异常: {exc}", "查看日志堆栈。").as_dict()
