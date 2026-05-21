#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
钉钉AI助手 - Stream模式
使用钉钉官方SDK发送消息
"""

import os
import json
import logging
from dotenv import load_dotenv
import dingtalk_stream
from openai import OpenAI
from alibabacloud_dingtalk.robot_1_0.client import Client as dingtalkrobot_1_0Client
from alibabacloud_dingtalk.oauth2_1_0.client import Client as dingtalkoauth2_1_0Client
from alibabacloud_tea_openapi import models as open_api_models
from alibabacloud_dingtalk.robot_1_0 import models as dingtalkrobot__1__0_models
from alibabacloud_dingtalk.oauth2_1_0 import models as dingtalkoauth_2__1__0_models
from alibabacloud_tea_util import models as util_models
from alibabacloud_tea_util.client import Client as UtilClient

load_dotenv()

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ========== 配置 ==========
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
DINGTALK_CLIENT_ID = os.getenv("DINGTALK_CLIENT_ID")
DINGTALK_CLIENT_SECRET = os.getenv("DINGTALK_CLIENT_SECRET")
DINGTALK_ROBOT_CODE = os.getenv("DINGTALK_ROBOT_CODE")  # 机器人代码

# 初始化 DeepSeek OpenAI 客户端
deepseek_client = OpenAI(
    api_key=DEEPSEEK_API_KEY,
    base_url="https://api.deepseek.com"
)

# 会话历史存储
conversation_history = {}

# 全局 access token 缓存
access_token_cache = {
    'token': None,
    'expires_in': 0
}


def get_dingtalk_access_token():
    """获取钉钉访问令牌 - 使用官方 OAuth2.0 方式"""
    try:
        # 检查缓存
        import time
        if access_token_cache['token'] and time.time() < access_token_cache['expires_in']:
            logger.info("使用缓存的 access token")
            return access_token_cache['token']
        
        # 创建 OAuth2 客户端
        config = open_api_models.Config()
        config.protocol = 'https'
        config.region_id = 'central'
        client = dingtalkoauth2_1_0Client(config)
        
        # 构建请求
        get_access_token_request = dingtalkoauth_2__1__0_models.GetAccessTokenRequest(
            app_key=DINGTALK_CLIENT_ID,
            app_secret=DINGTALK_CLIENT_SECRET
        )
        
        # 发送请求
        response = client.get_access_token(get_access_token_request).body
        # 缓存 token
        access_token_cache['token'] = response.access_token
        # 设置过期时间（提前5分钟刷新）
        access_token_cache['expires_in'] = time.time() + response.expire_in - 300
        
        logger.info(f"成功获取 access token，有效期: {response.expire_in}秒")
        return response.access_token
        
    except Exception as e:
        logger.error(f"获取钉钉Access Token失败: {str(e)}")
        if hasattr(e, 'code') and hasattr(e, 'message'):
            logger.error(f"错误码: {e.code}, 错误信息: {e.message}")
        return None


def send_dingtalk_message(user_id: str, text: str):
    """使用钉钉官方SDK发送单聊消息"""
    try:
        # 获取 access token
        access_token = get_dingtalk_access_token()
        if not access_token:
            logger.error("无法获取 access token")
            return False
        
        # 创建机器人客户端
        config = open_api_models.Config()
        config.protocol = 'https'
        config.region_id = 'central'
        client = dingtalkrobot_1_0Client(config)
        
        # 构建请求头
        batch_send_otoheaders = dingtalkrobot__1__0_models.BatchSendOTOHeaders()
        batch_send_otoheaders.x_acs_dingtalk_access_token = access_token
        
        # 构建消息参数
        msg_param = json.dumps({
            "content": text
        })
        
        # 构建请求
        batch_send_otorequest = dingtalkrobot__1__0_models.BatchSendOTORequest(
            robot_code=DINGTALK_ROBOT_CODE,
            user_ids=[user_id],
            msg_key="sampleText",  # 使用文本消息
            msg_param=msg_param
        )
        
        # 发送消息
        response = client.batch_send_otowith_options(
            batch_send_otorequest, 
            batch_send_otoheaders, 
            util_models.RuntimeOptions()
        )
        
        logger.info(f"成功发送消息给用户 {user_id}，内容长度: {len(text)}")
        return True
        
    except Exception as e:
        logger.error(f"发送钉钉消息失败: {str(e)}")
        if hasattr(e, 'code') and hasattr(e, 'message'):
            logger.error(f"错误码: {e.code}, 错误信息: {e.message}")
        return False


def call_deepseek(question: str, conversation_id: str = None) -> str:
    """使用 OpenAI SDK 调用 DeepSeek API 获取回复"""
    try:
        # 构建消息列表
        messages = [
            {"role": "system", "content": "你是一个专业的AI助手，请用简洁、友好的中文回答问题。"}
        ]
        
        # 添加历史对话（最近10条）
        if conversation_id and conversation_id in conversation_history:
            history = conversation_history[conversation_id][-10:]
            messages.extend(history)
        
        # 添加当前问题
        messages.append({"role": "user", "content": question})
        
        logger.info(f"调用DeepSeek API，问题: {question[:50]}...")
        
        # 调用 DeepSeek API
        response = deepseek_client.chat.completions.create(
            model="deepseek-chat",
            messages=messages,
            temperature=0.7,
            max_tokens=2000,
            stream=False
        )
        
        # 提取回复内容
        answer = response.choices[0].message.content
        logger.info(f"DeepSeek回复成功，长度: {len(answer)}")
        
        # 保存对话历史
        if conversation_id:
            if conversation_id not in conversation_history:
                conversation_history[conversation_id] = []
            conversation_history[conversation_id].append({"role": "user", "content": question})
            conversation_history[conversation_id].append({"role": "assistant", "content": answer})
            
            # 限制历史长度（最多20条，即10轮对话）
            if len(conversation_history[conversation_id]) > 20:
                conversation_history[conversation_id] = conversation_history[conversation_id][-20:]
        
        return answer
        
    except Exception as e:
        logger.error(f"DeepSeek API异常: {str(e)}", exc_info=True)
        return f"抱歉，处理您的问题时出现错误：{str(e)}"


class DeepSeekChatbotHandler(dingtalk_stream.ChatbotHandler):
    """自定义消息处理器"""
    
    async def process(self, callback: dingtalk_stream.CallbackMessage):
        """处理接收到的消息"""
        try:
            data = callback.data
            
            # 获取消息内容
            msg_type = data.get('msgtype', '')
            incoming_message = ""
            
            if msg_type == 'text':
                incoming_message = data.get('text', {}).get('content', '').strip()
            elif msg_type == 'voice':
                incoming_message = data.get('voice', {}).get('text', '').strip()
            else:
                logger.info(f"暂不支持的消息类型: {msg_type}")
                return dingtalk_stream.AckMessage.STATUS_OK,"unsupported"
            
            # 获取发送者信息
            sender_staff_id = data.get('senderStaffId', '')
            sender_nick = data.get('senderNick', '未知')
            
            # 获取会话信息
            conversation_id = data.get('conversationId', '')
            conversation_type = data.get('conversationType', '')
            is_group = conversation_type == '2'
            
            chat_type = "群聊" if is_group else "单聊"
            logger.info(f"收到{chat_type}消息 - 发送者: {sender_nick}, ID: {sender_staff_id}, 内容: {incoming_message[:50]}...")
            
            if not incoming_message or not sender_staff_id:
                return dingtalk_stream.AckMessage.STATUS_OK,"ignore"
            
            # 处理群聊@机器人
            question = incoming_message
            if is_group:
                # 移除 @机器人 前缀
                if incoming_message.startswith('@'):
                    parts = incoming_message.split(' ', 1)
                    if len(parts) > 1:
                        question = parts[1].strip()
                    else:
                        question = ""
            
            # 处理特殊命令
            if question.lower() in ['清空', '清除', '重置', 'clear', 'reset']:
                if conversation_id in conversation_history:
                    del conversation_history[conversation_id]
                    reply_text = "✅ 已清除对话历史！"
                else:
                    reply_text = "✅ 对话历史已清空"
                
                # 发送回复
                send_dingtalk_message(sender_staff_id, reply_text)
                return dingtalk_stream.AckMessage.STATUS_OK,"clear"
            
            if question.lower() in ['帮助', 'help', '/help']:
                help_text = """🤖 钉钉AI助手使用帮助

功能特性：
- 💬 智能对话：基于DeepSeek AI模型
- 📝 上下文记忆：记住对话历史
- 🧹 清空历史：发送"清空"或"重置"

使用方式：
- 单聊：直接发送消息即可
- 群聊：@机器人后发送消息

命令列表：
- 清空/重置：清除当前会话的对话历史
- 帮助/help：显示此帮助信息"""
                
                send_dingtalk_message(sender_staff_id, help_text)
                return dingtalk_stream.AckMessage.STATUS_OK,"help"
            
            if not question or question.strip() == "":
                send_dingtalk_message(sender_staff_id, "您好，请问有什么可以帮您？")
                return dingtalk_stream.AckMessage.STATUS_OK,"ask"
            
            # 调用DeepSeek获取回复
            answer = call_deepseek(question, conversation_id)
            
            # 发送回复
            send_dingtalk_message(sender_staff_id, answer)
            
        except Exception as e:
            logger.error(f"处理消息时出错: {str(e)}", exc_info=True)
            try:
                if 'sender_staff_id' in locals() and sender_staff_id:
                    send_dingtalk_message(sender_staff_id, f"处理您的消息时出现错误：{str(e)}")
            except Exception:
                pass
        
        return dingtalk_stream.AckMessage.STATUS_OK,"success"


def main():
    """主函数：启动Stream模式客户端"""
    
    # 检查必要的环境变量
    if not DEEPSEEK_API_KEY:
        logger.error("=" * 50)
        logger.error("❌ 请设置 DEEPSEEK_API_KEY 环境变量")
        logger.error("=" * 50)
        return
    
    if not DINGTALK_CLIENT_ID or not DINGTALK_CLIENT_SECRET:
        logger.error("=" * 50)
        logger.error("❌ 请设置 DINGTALK_CLIENT_ID 和 DINGTALK_CLIENT_SECRET")
        logger.error("=" * 50)
        return
    
    if not DINGTALK_ROBOT_CODE:
        logger.error("=" * 50)
        logger.error("❌ 请设置 DINGTALK_ROBOT_CODE 环境变量")
        logger.error("在钉钉开放平台-机器人-机器人配置中查看")
        logger.error("=" * 50)
        return
    
    # 测试获取 access token
    logger.info("测试获取钉钉 Access Token...")
    test_token = get_dingtalk_access_token()
    if test_token:
        logger.info("✅ Access Token 获取成功")
    else:
        logger.error("❌ Access Token 获取失败，请检查配置")
        return
    
    # 配置钉钉Stream客户端
    credential = dingtalk_stream.Credential(DINGTALK_CLIENT_ID, DINGTALK_CLIENT_SECRET)
    client = dingtalk_stream.DingTalkStreamClient(credential=credential)
    
    # 注册回调处理器
    client.register_callback_handler(
        '/v1.0/im/bot/messages/get',
        DeepSeekChatbotHandler()
    )
    
    logger.info("=" * 50)
    logger.info("🚀 钉钉AI助手启动中...")
    logger.info(f"📱 Client ID: {DINGTALK_CLIENT_ID[:10]}...")
    logger.info(f"🤖 Robot Code: {DINGTALK_ROBOT_CODE[:10]}...")
    logger.info("💬 请在钉钉中@机器人 或 私聊测试")
    logger.info("⌨️  按 Ctrl+C 停止服务")
    logger.info("=" * 50)
    
    try:
        client.start_forever()
    except KeyboardInterrupt:
        logger.info("✅ 服务已停止")
    except Exception as e:
        logger.error(f"❌ 启动失败: {str(e)}", exc_info=True)


if __name__ == "__main__":
    main()