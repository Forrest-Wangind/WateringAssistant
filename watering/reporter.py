"""每日图文报告：拍照 → 视觉分析 → 通过钉钉推送图片+Markdown。

钉钉 OTO 消息支持 sampleImageMsg（mediaId）与 sampleMarkdown，分别承载图片和文本。
若图片上传或发送失败，降级为纯 Markdown，正文中附本地路径。
"""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import List, Optional

import requests

from .camera import Camera, CameraError
from .config import AppConfig
from .vision import VisionAnalysis, VisionAnalyzer

logger = logging.getLogger(__name__)

# 钉钉媒体上传与消息发送复用 dingtalk_bot.py 中的工具
from dingtalk_bot import get_dingtalk_access_token  # noqa: E402

import json
from alibabacloud_dingtalk.robot_1_0.client import Client as RobotClient
from alibabacloud_dingtalk.robot_1_0 import models as robot_models
from alibabacloud_tea_openapi import models as open_api_models
from alibabacloud_tea_util import models as util_models


MEDIA_UPLOAD_URL = "https://oapi.dingtalk.com/media/upload"


class Reporter:
    def __init__(self, app_cfg: AppConfig, camera: Camera, vision: VisionAnalyzer):
        self._cfg = app_cfg
        self._camera = camera
        self._vision = vision

    # ---------- 主流程 ----------
    def generate_and_send(self) -> None:
        recipients = self._cfg.report_recipients
        if not recipients:
            logger.warning("未配置报告接收人，跳过本次推送")
            return

        try:
            photo_path = self._camera.capture()
        except CameraError as e:
            logger.error("拍照失败: %s", e)
            self._broadcast_text(recipients, f"⚠️ 花园日报生成失败：拍照异常 {e}")
            return

        try:
            analysis = self._vision.analyze(photo_path)
        except Exception as e:
            logger.exception("视觉分析失败")
            self._broadcast_text(
                recipients,
                f"⚠️ 花园日报生成失败：视觉分析异常 {e}\n本地照片: {photo_path}",
            )
            return

        markdown = analysis.to_markdown(photo_path.name)
        self._archive_report(photo_path, markdown, analysis)

        media_id = self._upload_image(photo_path)
        for user_id in recipients:
            sent_image = False
            if media_id:
                sent_image = self._send_image(user_id, media_id)
            if not sent_image:
                logger.warning("图片消息发送失败，降级为纯文本：user=%s", user_id)
            self._send_markdown(user_id, "🌱 花园生长日报", markdown)

    # ---------- 子步骤 ----------
    def _archive_report(self, photo: Path, markdown: str, analysis: VisionAnalysis) -> None:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        md_path = self._cfg.storage.report_dir / f"{ts}.md"
        md_path.write_text(
            f"<!-- photo: {photo} -->\n\n{markdown}\n\n---\n<details><summary>raw</summary>\n\n```\n{analysis.raw_text}\n```\n</details>\n",
            encoding="utf-8",
        )
        logger.info("报告已存档: %s", md_path)

    def _upload_image(self, image_path: Path) -> Optional[str]:
        token = get_dingtalk_access_token()
        if not token:
            logger.error("无 access token，跳过图片上传")
            return None
        try:
            with image_path.open("rb") as f:
                resp = requests.post(
                    MEDIA_UPLOAD_URL,
                    params={"access_token": token, "type": "image"},
                    files={"media": (image_path.name, f, "image/jpeg")},
                    timeout=30,
                )
            data = resp.json()
            if data.get("errcode") == 0:
                logger.info("图片上传成功 mediaId=%s", data.get("media_id"))
                return data.get("media_id")
            logger.error("图片上传返回错误: %s", data)
        except Exception:
            logger.exception("图片上传异常")
        return None

    def _robot_client(self) -> RobotClient:
        config = open_api_models.Config()
        config.protocol = "https"
        config.region_id = "central"
        return RobotClient(config)

    def _build_headers(self):
        token = get_dingtalk_access_token()
        headers = robot_models.BatchSendOTOHeaders()
        headers.x_acs_dingtalk_access_token = token
        return headers

    def _send_image(self, user_id: str, media_id: str) -> bool:
        try:
            req = robot_models.BatchSendOTORequest(
                robot_code=self._cfg.dingtalk_robot_code,
                user_ids=[user_id],
                msg_key="sampleImageMsg",
                msg_param=json.dumps({"photoURL": media_id}),
            )
            self._robot_client().batch_send_otowith_options(
                req, self._build_headers(), util_models.RuntimeOptions()
            )
            return True
        except Exception:
            logger.exception("发送图片消息失败 user=%s", user_id)
            return False

    def _send_markdown(self, user_id: str, title: str, text: str) -> bool:
        try:
            req = robot_models.BatchSendOTORequest(
                robot_code=self._cfg.dingtalk_robot_code,
                user_ids=[user_id],
                msg_key="sampleMarkdown",
                msg_param=json.dumps({"title": title, "text": text}),
            )
            self._robot_client().batch_send_otowith_options(
                req, self._build_headers(), util_models.RuntimeOptions()
            )
            return True
        except Exception:
            logger.exception("发送 Markdown 消息失败 user=%s", user_id)
            return False

    def _broadcast_text(self, recipients: List[str], text: str) -> None:
        from dingtalk_bot import send_dingtalk_message

        for uid in recipients:
            send_dingtalk_message(uid, text)
