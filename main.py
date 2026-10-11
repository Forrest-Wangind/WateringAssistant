"""多路浇花助理主入口。

启动顺序：
  1. 加载配置 + 初始化日志
  2. 构造 WateringAssistant（硬件 + AI + 钉钉）
  3. 注册定时任务（4 路浇水 + 每日报告）
  4. 启动钉钉 Stream 客户端（后台线程，接收 @机器人 消息）
  5. 启动 FastAPI（REST 接口）
"""
from __future__ import annotations

import logging
import signal
import sys

import uvicorn

from assistant import WateringAssistant
from dingtalk.bot import start_stream_client
from hardware.errors import HardwareError
from scheduler.watering_scheduler import WateringScheduler
from utils import load_config, setup_logging
from web.server import create_app


def main() -> int:
    cfg = load_config()
    setup_logging(cfg.get("system", {}).get("log_level", "INFO"))
    log = logging.getLogger("main")

    try:
        assistant = WateringAssistant(cfg)
    except HardwareError as exc:
        # 硬件不可用：立即退出并给出可执行建议，绝不以模拟方式继续运行
        log.error(
            "启动失败：硬件不可用 [%s]\n%s\n修复建议：%s\n"
            "（若只想在本机跑通流程，请把 config.yaml 的 system.mock_hardware 设为 true）",
            exc.kind, exc, exc.hint,
        )
        return 2
    cam = assistant.camera.status()
    if cam["is_mock"]:
        log.warning("⚠️ 相机处于 MOCK 模式：生成的照片为占位图（mode=%s kind=%s）",
                    cam["mode"], cam["error_kind"])
    else:
        log.info("📷 相机就绪：%s", cam["mode"])
    scheduler = WateringScheduler(timezone=cfg.get("system", {}).get("timezone", "Asia/Shanghai"))
    scheduler.schedule_channels(
        cfg["channels"],
        water_fn=lambda cid: _safe_run(lambda: assistant.water_channel(cid, notify=True)),
    )
    scheduler.schedule_daily_report(
        hour=18, minute=0,
        report_fn=lambda: _safe_run(lambda: assistant.take_photo_and_report(push=True)),
    )
    scheduler.start()

    start_stream_client(cfg.get("dingtalk", {}), assistant.handle_command)

    app = create_app(assistant)
    web_cfg = cfg.get("web", {})

    def _shutdown(*_):
        log.info("收到退出信号，清理资源…")
        scheduler.shutdown()
        assistant.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    log.info("启动 Web 服务 %s:%s", web_cfg.get("host", "0.0.0.0"), web_cfg.get("port", 8080))
    uvicorn.run(
        app,
        host=web_cfg.get("host", "0.0.0.0"),
        port=int(web_cfg.get("port", 8080)),
        log_level=cfg.get("system", {}).get("log_level", "INFO").lower(),
    )
    return 0


def _safe_run(fn):
    try:
        fn()
    except Exception:  # noqa: BLE001
        logging.getLogger("scheduler").exception("定时任务执行失败")


if __name__ == "__main__":
    raise SystemExit(main())
