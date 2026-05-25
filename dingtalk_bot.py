#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""钉钉机器人：DeepSeek 闲聊 + 浇花助理控制命令。

命令路由（在 _route_command 中处理）：
  帮助 / help                显示命令清单
  状态                       查看各通道开关与剩余时长
  浇水 <通道id> [秒数]        立即浇水，秒数缺省取 default_duration_seconds
  停止 <通道id> | 停止 全部   关闭通道
  报告                       立即生成一次图文报告
  清空                       清空 DeepSeek 对话历史
其余消息走 DeepSeek 闲聊。
"""

import json
import logging
import os
import time
from typing import Optional

import dingtalk_stream
from alibabacloud_dingtalk.oauth2_1_0 import models as dingtalkoauth_2__1__0_models
from alibabacloud_dingtalk.oauth2_1_0.client import Client as dingtalkoauth2_1_0Client
from alibabacloud_dingtalk.robot_1_0 import models as dingtalkrobot__1__0_models
from alibabacloud_dingtalk.robot_1_0.client import Client as dingtalkrobot_1_0Client
from alibabacloud_tea_openapi import models as open_api_models
from alibabacloud_tea_util import models as util_models
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ========== 配置 ==========
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
DINGTALK_CLIENT_ID = os.getenv("DINGTALK_CLIENT_ID")
DINGTALK_CLIENT_SECRET = os.getenv("DINGTALK_CLIENT_SECRET")
DINGTALK_ROBOT_CODE = os.getenv("DINGTALK_ROBOT_CODE")

deepseek_client = OpenAI(
    api_key=DEEPSEEK_API_KEY,
    base_url="https://api.deepseek.com",
) if DEEPSEEK_API_KEY else None

conversation_history = {}
access_token_cache = {"token": None, "expires_in": 0}


# ========== 钉钉基础工具 ==========
def get_dingtalk_access_token():
    try:
        if access_token_cache["token"] and time.time() < access_token_cache["expires_in"]:
            return access_token_cache["token"]

        config = open_api_models.Config()
        config.protocol = "https"
        config.region_id = "central"
        client = dingtalkoauth2_1_0Client(config)

        req = dingtalkoauth_2__1__0_models.GetAccessTokenRequest(
            app_key=DINGTALK_CLIENT_ID,
            app_secret=DINGTALK_CLIENT_SECRET,
        )
        response = client.get_access_token(req).body
        access_token_cache["token"] = response.access_token
        access_token_cache["expires_in"] = time.time() + response.expire_in - 300
        logger.info("成功获取 access token，有效期: %ss", response.expire_in)
        return response.access_token
    except Exception as e:
        logger.error("获取钉钉 Access Token 失败: %s", e)
        return None


def send_dingtalk_message(user_id: str, text: str) -> bool:
    try:
        token = get_dingtalk_access_token()
        if not token:
            return False

        config = open_api_models.Config()
        config.protocol = "https"
        config.region_id = "central"
        client = dingtalkrobot_1_0Client(config)

        headers = dingtalkrobot__1__0_models.BatchSendOTOHeaders()
        headers.x_acs_dingtalk_access_token = token

        req = dingtalkrobot__1__0_models.BatchSendOTORequest(
            robot_code=DINGTALK_ROBOT_CODE,
            user_ids=[user_id],
            msg_key="sampleText",
            msg_param=json.dumps({"content": text}),
        )
        client.batch_send_otowith_options(req, headers, util_models.RuntimeOptions())
        return True
    except Exception as e:
        logger.error("发送钉钉消息失败: %s", e)
        return False


def call_deepseek(question: str, conversation_id: Optional[str] = None) -> str:
    if deepseek_client is None:
        return "（DeepSeek 未配置，闲聊功能关闭）"
    try:
        messages = [
            {"role": "system", "content": "你是一个专业的AI助手，请用简洁、友好的中文回答问题。"}
        ]
        if conversation_id and conversation_id in conversation_history:
            messages.extend(conversation_history[conversation_id][-10:])
        messages.append({"role": "user", "content": question})

        response = deepseek_client.chat.completions.create(
            model="deepseek-chat",
            messages=messages,
            temperature=0.7,
            max_tokens=2000,
            stream=False,
        )
        answer = response.choices[0].message.content
        if conversation_id:
            conversation_history.setdefault(conversation_id, []).append(
                {"role": "user", "content": question}
            )
            conversation_history[conversation_id].append(
                {"role": "assistant", "content": answer}
            )
            if len(conversation_history[conversation_id]) > 20:
                conversation_history[conversation_id] = conversation_history[conversation_id][-20:]
        return answer
    except Exception as e:
        logger.error("DeepSeek API 异常: %s", e, exc_info=True)
        return f"抱歉，处理您的问题时出现错误：{e}"


# ========== 命令路由 ==========
HELP_TEXT = """🌱 浇花助理命令

控制类：
  浇水 <通道> [秒数]   例: 浇水 1 30
  停止 <通道|全部>      例: 停止 1 / 停止 全部
  状态                  查看各通道当前状态
  报告                  立即生成一次图文日报

其它：
  清空 / 重置           清空闲聊上下文
  帮助 / help           显示本帮助
其余消息默认走 DeepSeek 闲聊。"""


class WateringChatbotHandler(dingtalk_stream.ChatbotHandler):
    """钉钉消息处理：先尝试命令路由，未命中再走 DeepSeek 闲聊。"""

    def __init__(self, controller=None, reporter=None, default_duration: int = 30):
        super().__init__()
        self._controller = controller
        self._reporter = reporter
        self._default_duration = default_duration

    async def process(self, callback: dingtalk_stream.CallbackMessage):
        try:
            data = callback.data
            msg_type = data.get("msgtype", "")
            if msg_type == "text":
                incoming = data.get("text", {}).get("content", "").strip()
            elif msg_type == "voice":
                incoming = data.get("voice", {}).get("text", "").strip()
            else:
                return dingtalk_stream.AckMessage.STATUS_OK, "unsupported"

            sender_id = data.get("senderStaffId", "")
            sender_nick = data.get("senderNick", "未知")
            conv_id = data.get("conversationId", "")
            is_group = data.get("conversationType", "") == "2"

            logger.info("收到消息: %s -> %s", sender_nick, incoming[:50])

            if not incoming or not sender_id:
                return dingtalk_stream.AckMessage.STATUS_OK, "ignore"

            # 群聊去掉 @机器人 前缀
            question = incoming
            if is_group and incoming.startswith("@"):
                parts = incoming.split(" ", 1)
                question = parts[1].strip() if len(parts) > 1 else ""

            handled, reply = self._route_command(question, conv_id)
            if handled:
                send_dingtalk_message(sender_id, reply)
                return dingtalk_stream.AckMessage.STATUS_OK, "command"

            if not question:
                send_dingtalk_message(sender_id, "您好，请问有什么可以帮您？输入"帮助"查看命令。")
                return dingtalk_stream.AckMessage.STATUS_OK, "ask"

            answer = call_deepseek(question, conv_id)
            send_dingtalk_message(sender_id, answer)
        except Exception as e:
            logger.error("处理消息异常: %s", e, exc_info=True)

        return dingtalk_stream.AckMessage.STATUS_OK, "success"

    # ---------- 命令解析 ----------
    def _route_command(self, text: str, conv_id: str):
        """返回 (是否命中命令, 回复文本)。"""
        if not text:
            return False, ""

        lower = text.lower().strip()
        if lower in {"帮助", "help", "/help", "?", "？"}:
            return True, HELP_TEXT
        if lower in {"清空", "清除", "重置", "clear", "reset"}:
            conversation_history.pop(conv_id, None)
            return True, "✅ 已清空闲聊上下文"

        tokens = text.split()
        head = tokens[0]

        if head in {"状态", "status"}:
            return True, self._cmd_status()

        if head in {"浇水", "water"}:
            return True, self._cmd_water(tokens[1:])

        if head in {"停止", "stop", "关闭"}:
            return True, self._cmd_stop(tokens[1:])

        if head in {"报告", "report", "日报"}:
            return True, self._cmd_report()

        return False, ""

    def _require_controller(self) -> Optional[str]:
        if self._controller is None:
            return "⚠️ GPIO 控制器未初始化（可能不在树莓派环境）"
        return None

    def _cmd_status(self) -> str:
        if (err := self._require_controller()):
            return err
        states = self._controller.list_channels()
        lines = ["📊 通道状态"]
        for s in states:
            if s.is_open and s.will_close_at:
                remain = max(0, int(s.will_close_at - time.time()))
                lines.append(f"  [{s.channel_id}] {s.name}: 🟢 开 (剩余 {remain}s)")
            else:
                lines.append(f"  [{s.channel_id}] {s.name}: ⚪ 关")
        return "\n".join(lines)

    def _cmd_water(self, args) -> str:
        if (err := self._require_controller()):
            return err
        if not args:
            return "用法: 浇水 <通道id> [秒数]"
        try:
            channel_id = int(args[0])
        except ValueError:
            return f"通道 id 必须是数字: {args[0]}"

        duration = self._default_duration
        if len(args) >= 2:
            try:
                duration = int(args[1])
            except ValueError:
                return f"秒数必须是整数: {args[1]}"

        try:
            state = self._controller.open_valve(channel_id, duration)
        except ValueError as e:
            return f"❌ {e}"
        except Exception as e:
            logger.exception("开阀失败")
            return f"❌ 操作失败: {e}"
        return f"💧 通道 {state.channel_id} ({state.name}) 已开阀 {duration}s"

    def _cmd_stop(self, args) -> str:
        if (err := self._require_controller()):
            return err
        if not args:
            return "用法: 停止 <通道id> | 停止 全部"
        target = args[0]
        if target in {"全部", "all", "*"}:
            self._controller.close_all()
            return "✅ 所有通道已关闭"
        try:
            channel_id = int(target)
        except ValueError:
            return f"通道 id 必须是数字: {target}"
        try:
            state = self._controller.close_valve(channel_id)
        except ValueError as e:
            return f"❌ {e}"
        return f"✅ 通道 {state.channel_id} ({state.name}) 已关闭"

    def _cmd_report(self) -> str:
        if self._reporter is None:
            return "⚠️ 报告模块未启用"
        # 报告流程含 AI 调用，时间较长，异步执行避免阻塞 stream 回调
        import threading
        threading.Thread(target=self._reporter.generate_and_send, daemon=True).start()
        return "📷 已开始拍照分析，稍后将推送图文报告"


# 历史名保留（外部代码可能引用）
DeepSeekChatbotHandler = WateringChatbotHandler


def main():
    """单独运行 dingtalk_bot.py 时的入口（不含浇花功能）。
    生产部署请使用 main.py，会注入 GPIO/Reporter 服务。
    """
    if not all([DEEPSEEK_API_KEY, DINGTALK_CLIENT_ID, DINGTALK_CLIENT_SECRET, DINGTALK_ROBOT_CODE]):
        logger.error("环境变量缺失，请检查 .env")
        return

    if not get_dingtalk_access_token():
        logger.error("Access Token 获取失败")
        return

    credential = dingtalk_stream.Credential(DINGTALK_CLIENT_ID, DINGTALK_CLIENT_SECRET)
    client = dingtalk_stream.DingTalkStreamClient(credential=credential)
    client.register_callback_handler(
        "/v1.0/im/bot/messages/get",
        WateringChatbotHandler(),
    )
    logger.info("🚀 钉钉机器人启动（仅闲聊模式）")
    try:
        client.start_forever()
    except KeyboardInterrupt:
        logger.info("✅ 已停止")


if __name__ == "__main__":
    main()
