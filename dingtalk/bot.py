"""钉钉双向通信。

* RobotNotifier: 企业内部机器人单聊主动推送（见 robot_notifier.py）。
* SubscriberStore: 记录要从哪里推给谁（见 subscribers.py）。
* DingTalkBot: 主动推送的**路由器** —— 机器人通道优先、webhook 备选、都没有则显式报错。
* parse_command: 解析钉钉消息中的用户指令。
* WateringChatbotHandler: dingtalk-stream 的 ChatbotHandler 实现，
  通过 Stream 模式接收 @机器人 消息，无需公网回调地址。
* start_stream_client: 在后台线程启动 Stream 客户端。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import threading
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import dingtalk_stream
import httpx
from dingtalk_stream import AckMessage, ChatbotHandler, ChatbotMessage

from dingtalk.robot_notifier import PushError, RobotNotifier
from dingtalk.subscribers import SubscriberStore

logger = logging.getLogger(__name__)


@dataclass
class Command:
    raw: str
    action: str           # water / report / photo / status / help
    target: Optional[str] = None
    volume_ml: Optional[int] = None  # 保留兼容旧命令 (已弃用，请用 duration_sec)
    duration_sec: Optional[int] = None
    sender_id: Optional[str] = None


class DingTalkBot:
    """主动推送路由器。

    通道优先级：

    1. ``robot``  —— 企业内部机器人单聊。**唯一在没有公网 IP 的环境下也能发照片的通道**
       （图片走 media/upload 拿 mediaId，官方允许 mediaId 直接当 photoURL 用）。
    2. ``webhook`` —— 自定义机器人（只能发群）。markdown 里的图片必须是公网 URL，
       需要额外配置 ``dingtalk.image_public_base_url``。
    3. ``none``   —— 没有任何可用通道。``allow_mock_fallback=True``（本地开发）时只打印
       日志并明确标注；否则**返回错误并告警**，绝不假装发送成功。
    """

    def __init__(self, cfg: Dict[str, Any],
                 subscribers: Optional[SubscriberStore] = None,
                 allow_mock_fallback: bool = False) -> None:
        self.cfg = cfg or {}
        self.webhook = os.environ.get(self.cfg.get("webhook_env", "DINGTALK_WEBHOOK"), "")
        self.secret = os.environ.get(self.cfg.get("secret_env", "DINGTALK_SECRET"), "")
        self.robot = RobotNotifier(self.cfg)
        self.subscribers = subscribers or SubscriberStore(
            self.cfg.get("subscribers_file", "./data/subscribers.json"),
            preset=self.cfg.get("user_ids"),
        )
        self.allow_mock_fallback = allow_mock_fallback
        self.last_error: Optional[Dict[str, Any]] = None

        if not self.webhook:
            logger.info("未设置钉钉 webhook（DINGTALK_WEBHOOK）—— 群推送通道不可用")
        if not self.robot.configured:
            logger.warning("企业机器人推送未配置，缺少: %s —— 无法主动推送照片/报告",
                           ", ".join(self.robot.missing_credentials()) or "全部凭证")

    # ---------- 通道 ----------
    @property
    def channel(self) -> str:
        if self.robot.configured:
            return "robot"
        if self.webhook:
            return "webhook"
        return "none"

    def recipients(self) -> List[str]:
        return self.subscribers.all()

    def delivery_status(self) -> Dict[str, Any]:
        """启动自检 / 状态接口用。"""
        return {
            "channel": self.channel,
            "robot_configured": self.robot.configured,
            "webhook_configured": bool(self.webhook),
            "missing_credentials": self.robot.missing_credentials(),
            "recipients": self.recipients(),
            "mock_fallback": self.allow_mock_fallback,
            "last_error": self.last_error,
        }

    # ---------- 推送 ----------
    def send_text(self, content: str, at_mobiles: Optional[List[str]] = None) -> Dict[str, Any]:
        ch = self.channel
        if ch == "robot":
            return self._record(self.robot.send_text(content, self.recipients()))
        if ch == "webhook":
            payload = {
                "msgtype": "text",
                "text": {"content": content},
                "at": {"atMobiles": at_mobiles or [], "isAtAll": False},
            }
            return self._record(self._post(payload))
        return self._record(self._no_channel("send_text"))

    def send_markdown(self, title: str, markdown: str) -> Dict[str, Any]:
        ch = self.channel
        if ch == "robot":
            return self._record(self.robot.send_markdown(title, markdown, self.recipients()))
        if ch == "webhook":
            payload = {"msgtype": "markdown", "markdown": {"title": title, "text": markdown}}
            return self._record(self._post(payload))
        return self._record(self._no_channel("send_markdown"))

    def send_image(self, image_path: str | os.PathLike[str] | None,
                   title: str = "花园照片") -> Dict[str, Any]:
        """推送一张**本地**图片。

        机器人通道：上传拿 mediaId 后发 sampleImageMsg，内网可用。
        webhook 通道：钉钉会自己去抓图，必须是公网可访问 URL。
        """
        if not image_path:
            return self._record(PushError("not_found", "没有图片可推送", "先执行拍照。").as_dict())

        ch = self.channel
        if ch == "robot":
            return self._record(self.robot.send_image(image_path, self.recipients(), title=title))
        if ch == "webhook":
            base = str(self.cfg.get("image_public_base_url", "")).rstrip("/")
            if not base:
                err = PushError(
                    "no_public_url",
                    "webhook 通道无法推送本地图片：钉钉需要公网可访问的图片 URL",
                    "改用企业机器人通道（配 DINGTALK_ROBOT_CODE），"
                    "或把照片放到公网并配置 dingtalk.image_public_base_url。",
                )
                logger.error("钉钉推送失败 [%s]: %s | 建议: %s", err.kind, err, err.hint)
                return self._record(err.as_dict())
            url = f"{base}/{Path(image_path).name}"
            payload = {"msgtype": "markdown",
                       "markdown": {"title": title, "text": f"### {title}\n\n![photo]({url})"}}
            return self._record(self._post(payload))
        return self._record(self._no_channel("send_image"))

    # ---------- 内部 ----------
    def _no_channel(self, action: str) -> Dict[str, Any]:
        if self.allow_mock_fallback:
            logger.info("[MOCK 钉钉发送] action=%s（未配置推送通道，仅本地打印）", action)
            return {"errcode": 0, "errmsg": "mock", "ok": True, "mock": True}
        err = PushError(
            "not_configured",
            f"没有可用的钉钉推送通道，{action} 未发送",
            "配置企业机器人（DINGTALK_CLIENT_ID/SECRET + DINGTALK_ROBOT_CODE）"
            "或自定义机器人 webhook；只想本地跑通流程请把 system.mock_hardware 设为 true。",
        )
        logger.error("钉钉推送失败 [%s]: %s | 建议: %s", err.kind, err, err.hint)
        return err.as_dict()

    def _record(self, result: Dict[str, Any]) -> Dict[str, Any]:
        self.last_error = None if result.get("ok") else result
        return result

    def _post(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not self.webhook:
            return self._no_channel(payload.get("msgtype", "webhook"))
        url = self._signed_url()
        try:
            r = httpx.post(url, json=payload, timeout=10)
            r.raise_for_status()
            data = r.json()
            if int(data.get("errcode", 0)) != 0:
                return {"errcode": data.get("errcode"), "errmsg": data.get("errmsg"),
                        "ok": False, "error_kind": "api_error", "hint": "检查 webhook access_token/加签。"}
            return {"errcode": 0, "errmsg": "ok", "ok": True, "raw": data}
        except httpx.HTTPError as exc:
            logger.exception("钉钉 webhook 推送失败: %s", exc)
            return {"errcode": -1, "errmsg": str(exc), "ok": False,
                    "error_kind": "unreachable", "hint": "检查网络或 webhook 地址。"}

    def _signed_url(self) -> str:
        if not self.secret:
            return self.webhook
        timestamp = str(round(time.time() * 1000))
        string_to_sign = f"{timestamp}\n{self.secret}".encode("utf-8")
        sign = base64.b64encode(
            hmac.new(self.secret.encode("utf-8"), string_to_sign, digestmod=hashlib.sha256).digest()
        )
        sign_str = urllib.parse.quote_plus(sign)
        joiner = "&" if "?" in self.webhook else "?"
        return f"{self.webhook}{joiner}timestamp={timestamp}&sign={sign_str}"


# ---------- 指令解析 ----------
_HELP_TEXT = (
    "🌱 浇花助理指令：\n"
    "  浇水 <植物类型> [时长s]     例：浇水 多肉 30\n"
    "  浇水 通道<1-4> [时长s]      例：浇水 通道2 45\n"
    "  报告                       生成图文生长报告\n"
    "  拍照                       立即拍一张花园照片\n"
    "  订阅                       开启每日照片+报告推送\n"
    "  状态                       查看 4 路浇水状态\n"
    "  帮助                       查看本说明\n"
)


def parse_command(text: str, sender_id: Optional[str] = None) -> Command:
    raw = text.strip()
    cleaned = re.sub(r"@\S+", "", raw).strip()

    if cleaned in ("帮助", "help", "?", "？"):
        return Command(raw, "help", sender_id=sender_id)
    if cleaned in ("报告", "生长报告", "report"):
        return Command(raw, "report", sender_id=sender_id)
    if cleaned in ("拍照", "photo"):
        return Command(raw, "photo", sender_id=sender_id)
    if cleaned in ("订阅", "subscribe"):
        return Command(raw, "subscribe", sender_id=sender_id)
    if cleaned in ("状态", "status"):
        return Command(raw, "status", sender_id=sender_id)

    m = re.match(r"^(?:浇水|浇)\s*(\S+)\s*(?:(\d+)\s*(?:s|秒)?)?$", cleaned)
    if m:
        return Command(
            raw=raw,
            action="water",
            target=m.group(1),
            duration_sec=int(m.group(2)) if m.group(2) else None,
            sender_id=sender_id,
        )
    return Command(raw, "unknown", sender_id=sender_id)


def help_text() -> str:
    return _HELP_TEXT


# ---------- 消息文本提取 ----------
def _extract_text(incoming: ChatbotMessage) -> str:
    """从不同类型的钉钉消息中提取可处理的文本。

    支持的消息类型：
      - text  : 普通文本
      - audio : 语音消息，优先使用服务端语音识别结果（recognition），
                无识别文字时返回空串（无法处理）
    """
    msg_type = (incoming.message_type or "").lower()

    if msg_type == "audio":
        # 语音消息数据在 incoming.extensions，结构：
        # {"openThreadId": "...", "content": {"downloadCode": "...", "recognition": "..."}}
        extensions = incoming.extensions or {}
        if isinstance(extensions, str):
            try:
                extensions = json.loads(extensions)
            except json.JSONDecodeError:
                extensions = {}
        audio_content = extensions.get("content", {})
        recognition = audio_content.get("recognition", "")
        if recognition:
            logger.info("语音识别文字: %s", recognition)
        else:
            logger.info("语音消息无识别文字，downloadCode=%s", audio_content.get("downloadCode", "")[:16])
        return recognition.strip()

    # 默认：文本消息（msgtype=text）
    return (incoming.text.content if incoming.text else "") or ""


# ---------- Stream 模式 ----------
class WateringChatbotHandler(ChatbotHandler):
    """接收 @机器人 消息并交给 handle_command 回调处理。"""

    def __init__(self, command_handler: Callable[[str, Optional[str]], str],
                 logger_: Optional[logging.Logger] = None,
                 on_sender: Optional[Callable[[str], None]] = None) -> None:
        super().__init__()
        self._command_handler = command_handler
        self._on_sender = on_sender
        if logger_:
            self.logger = logger_

    async def process(self, callback: dingtalk_stream.CallbackMessage):
        incoming = ChatbotMessage.from_dict(callback.data)
        sender_id = incoming.sender_staff_id or incoming.sender_id

        text = _extract_text(incoming)
        logger.info("收到钉钉消息 sender=%s msgtype=%s text=%s",
                    sender_id, incoming.message_type, text)

        # 先记录发送者：机器人不能凭空发起单聊，必须靠这一步拿到 userId
        if self._on_sender and sender_id:
            try:
                self._on_sender(sender_id)
            except Exception:  # noqa: BLE001  记录失败不应影响指令执行
                logger.exception("记录推送订阅者失败")

        if not text:
            logger.info("消息无可处理文本（msgtype=%s），跳过", incoming.message_type)
            return AckMessage.STATUS_OK, "OK"

        try:
            reply = self._command_handler(text, sender_id)
        except Exception as exc:  # noqa: BLE001
            logger.exception("指令处理异常")
            reply = f"❌ 执行失败：{exc}"
        self.reply_markdown("浇花助理", reply, incoming)
        return AckMessage.STATUS_OK, "OK"


def start_stream_client(cfg: Dict[str, Any],
                        command_handler: Callable[[str, Optional[str]], str],
                        on_sender: Optional[Callable[[str], None]] = None
                        ) -> Optional[threading.Thread]:
    """根据配置启动 Stream 客户端。返回后台线程；未配置则返回 None。"""
    client_id = os.environ.get(cfg.get("client_id_env", "DINGTALK_CLIENT_ID"), "")
    client_secret = os.environ.get(cfg.get("client_secret_env", "DINGTALK_CLIENT_SECRET"), "")
    if not (client_id and client_secret):
        logger.warning("未配置 DINGTALK_CLIENT_ID/SECRET，跳过 Stream 客户端")
        return None

    credential = dingtalk_stream.Credential(client_id, client_secret)
    client = dingtalk_stream.DingTalkStreamClient(credential)
    handler = WateringChatbotHandler(command_handler, logger_=logger, on_sender=on_sender)
    client.register_callback_handler(ChatbotMessage.TOPIC, handler)

    thread = threading.Thread(
        target=client.start_forever,
        name="dingtalk-stream",
        daemon=True,
    )
    thread.start()
    logger.info("钉钉 Stream 客户端已启动")
    return thread
