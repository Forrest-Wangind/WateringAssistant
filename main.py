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

    # ---- 推送通道自检：发不出去必须立刻可见，不能等到晚上才发现没收到照片 ----
    push_st = assistant.bot.delivery_status()
    if push_st["channel"] == "none":
        if push_st["mock_fallback"]:
            log.info("钉钉推送为本地打印模式（system.mock_hardware=true）")
        else:
            log.error(
                "⚠️ 没有可用的钉钉推送通道（缺少 %s）—— 定时浇水仍会执行，"
                "但每日照片与报告**无法送达**。请配置企业机器人"
                "（DINGTALK_CLIENT_ID / DINGTALK_CLIENT_SECRET / DINGTALK_ROBOT_CODE）"
                "或自定义机器人 webhook。",
                ", ".join(push_st["missing_credentials"]) or "全部凭证",
            )
    elif not push_st["recipients"]:
        log.warning(
            "⚠️ 推送通道就绪(%s)，但还没有接收人：在钉钉里给机器人发一条消息即可自动订阅",
            push_st["channel"],
        )
    else:
        log.info("📮 钉钉推送就绪：通道=%s 接收人=%d 位",
                 push_st["channel"], len(push_st["recipients"]))

    push_cfg = cfg.get("push", {})
    daily = push_cfg.get("daily_report", {}) or {}
    if push_cfg.get("enabled", True):
        scheduler.schedule_daily_report(
            hour=int(daily.get("hour", 13)), minute=int(daily.get("minute", 0)),
            report_fn=lambda: _safe_run(
                lambda: assistant.take_photo_and_report(push=True),
                on_error=lambda exc: assistant.notify_failure("每日照片/报告任务失败", exc),
            ),
        )
    else:
        log.warning("push.enabled=false，未注册每日照片/报告任务")
    scheduler.start()

    start_stream_client(cfg.get("dingtalk", {}), assistant.handle_command,
                        on_sender=assistant.note_subscriber)

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


def _safe_run(fn, on_error=None):
    """执行定时任务；失败时先记日志，再尽力把失败原因推给用户（否则用户毫无感知）。"""
    try:
        fn()
    except Exception as exc:  # noqa: BLE001
        logging.getLogger("scheduler").exception("定时任务执行失败")
        if on_error is not None:
            try:
                on_error(exc)
            except Exception:  # noqa: BLE001  通知失败不能再往上抛
                logging.getLogger("scheduler").exception("失败通知推送失败")


if __name__ == "__main__":
    raise SystemExit(main())
