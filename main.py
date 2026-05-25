#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""浇花助理总入口：装配 GPIO / Camera / Vision / Reporter / Scheduler / 钉钉。"""

import logging
import signal
import sys
from typing import Optional

import dingtalk_stream

from dingtalk_bot import (
    WateringChatbotHandler,
    get_dingtalk_access_token,
)
from watering.camera import Camera, CameraError
from watering.config import load_config
from watering.gpio_controller import GPIOController, GPIOUnavailableError
from watering.reporter import Reporter
from watering.scheduler import WateringScheduler
from watering.vision import VisionAnalyzer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


def main() -> None:
    cfg = load_config()

    missing = [
        k
        for k, v in {
            "DINGTALK_CLIENT_ID": cfg.dingtalk_client_id,
            "DINGTALK_CLIENT_SECRET": cfg.dingtalk_client_secret,
            "DINGTALK_ROBOT_CODE": cfg.dingtalk_robot_code,
        }.items()
        if not v
    ]
    if missing:
        logger.error("环境变量缺失: %s", ", ".join(missing))
        sys.exit(1)

    if not get_dingtalk_access_token():
        logger.error("钉钉 Access Token 获取失败，停止启动")
        sys.exit(1)

    # ---------- GPIO ----------
    try:
        gpio = GPIOController(cfg.channels, cfg.relay)
    except GPIOUnavailableError as e:
        logger.error("%s", e)
        sys.exit(1)

    # ---------- 摄像头 + 视觉 + 报告 ----------
    reporter: Optional[Reporter] = None
    camera: Optional[Camera] = None
    if cfg.report.enabled:
        if not cfg.dashscope_api_key:
            logger.warning("DASHSCOPE_API_KEY 未配置，禁用日报")
        else:
            try:
                camera = Camera(
                    cfg.storage.photo_dir,
                    cfg.report.image_width,
                    cfg.report.image_height,
                )
                vision = VisionAnalyzer(cfg.vision, cfg.dashscope_api_key)
                reporter = Reporter(cfg, camera, vision)
            except CameraError as e:
                logger.error("摄像头初始化失败，禁用日报: %s", e)

    # ---------- 调度器 ----------
    scheduler = WateringScheduler(cfg, gpio, reporter)
    scheduler.start()

    # ---------- 钉钉 Stream ----------
    handler = WateringChatbotHandler(
        controller=gpio,
        reporter=reporter,
        default_duration=cfg.relay.default_duration_seconds,
    )
    credential = dingtalk_stream.Credential(
        cfg.dingtalk_client_id, cfg.dingtalk_client_secret
    )
    stream_client = dingtalk_stream.DingTalkStreamClient(credential=credential)
    stream_client.register_callback_handler(
        "/v1.0/im/bot/messages/get", handler
    )

    # ---------- 优雅退出 ----------
    def _shutdown(signum, _frame):
        logger.info("收到信号 %s，开始关闭", signum)
        try:
            scheduler.stop()
        except Exception:
            logger.exception("scheduler stop 异常")
        try:
            gpio.cleanup()
        except Exception:
            logger.exception("gpio cleanup 异常")
        if camera is not None:
            try:
                camera.close()
            except Exception:
                logger.exception("camera close 异常")
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    logger.info("=" * 50)
    logger.info("🌱 浇花助理已启动")
    logger.info("通道: %d  定时任务: %d  日报: %s",
                len(cfg.channels), len(cfg.schedules),
                "开" if reporter else "关")
    logger.info("接收人: %s", ", ".join(cfg.report_recipients) or "（未配置）")
    logger.info("=" * 50)

    try:
        stream_client.start_forever()
    except KeyboardInterrupt:
        _shutdown(signal.SIGINT, None)


if __name__ == "__main__":
    main()
