"""钉钉双向通信。

* DingTalkBot: 通过 webhook 主动推送文本 / Markdown / 图文卡片。
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
from typing import Any, Callable, Dict, List, Optional

import dingtalk_stream
import httpx
from dingtalk_stream import AckMessage, ChatbotHandler, ChatbotMessage

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
    def __init__(self, cfg: Dict[str, Any]) -> None:
        self.webhook = os.environ.get(cfg.get("webhook_env", "DINGTALK_WEBHOOK"), "")
        self.secret = os.environ.get(cfg.get("secret_env", "DINGTALK_SECRET"), "")
        if not self.webhook:
            logger.warning("未设置钉钉 webhook，发送将变成本地打印")

    # ---------- 推送 ----------
    def send_text(self, content: str, at_mobiles: Optional[List[str]] = None) -> Dict[str, Any]:
        payload = {
            "msgtype": "text",
            "text": {"content": content},
            "at": {"atMobiles": at_mobiles or [], "isAtAll": False},
        }
        return self._post(payload)

    def send_markdown(self, title: str, markdown: str) -> Dict[str, Any]:
        payload = {
            "msgtype": "markdown",
            "markdown": {"title": title, "text": markdown},
        }
        return self._post(payload)

    def send_image(self, image_url: str, title: str = "花园照片") -> Dict[str, Any]:
        return self.send_markdown(title, f"### {title}\n\n![photo]({image_url})")

    def _post(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not self.webhook:
            logger.info("[MOCK 钉钉发送] %s", json.dumps(payload, ensure_ascii=False))
            return {"errcode": 0, "errmsg": "mock"}
        url = self._signed_url()
        try:
            r = httpx.post(url, json=payload, timeout=10)
            r.raise_for_status()
            return r.json()
        except Exception as exc:  # noqa: BLE001
            logger.exception("钉钉推送失败: %s", exc)
            return {"errcode": -1, "errmsg": str(exc)}

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
                 logger_: Optional[logging.Logger] = None) -> None:
        super().__init__()
        self._command_handler = command_handler
        if logger_:
            self.logger = logger_

    async def process(self, callback: dingtalk_stream.CallbackMessage):
        incoming = ChatbotMessage.from_dict(callback.data)
        sender_id = incoming.sender_staff_id or incoming.sender_id

        text = _extract_text(incoming)
        logger.info("收到钉钉消息 sender=%s msgtype=%s text=%s",
                    sender_id, incoming.message_type, text)

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
                        command_handler: Callable[[str, Optional[str]], str]
                        ) -> Optional[threading.Thread]:
    """根据配置启动 Stream 客户端。返回后台线程；未配置则返回 None。"""
    client_id = os.environ.get(cfg.get("client_id_env", "DINGTALK_CLIENT_ID"), "")
    client_secret = os.environ.get(cfg.get("client_secret_env", "DINGTALK_CLIENT_SECRET"), "")
    if not (client_id and client_secret):
        logger.warning("未配置 DINGTALK_CLIENT_ID/SECRET，跳过 Stream 客户端")
        return None

    credential = dingtalk_stream.Credential(client_id, client_secret)
    client = dingtalk_stream.DingTalkStreamClient(credential)
    handler = WateringChatbotHandler(command_handler, logger_=logger)
    client.register_callback_handler(ChatbotMessage.TOPIC, handler)

    thread = threading.Thread(
        target=client.start_forever,
        name="dingtalk-stream",
        daemon=True,
    )
    thread.start()
    logger.info("钉钉 Stream 客户端已启动")
    return thread
