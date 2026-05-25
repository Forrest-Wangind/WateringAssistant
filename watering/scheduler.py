"""APScheduler 调度：定时浇水 + 每日图文报告。"""
from __future__ import annotations

import logging
from typing import Callable, Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from .config import AppConfig
from .gpio_controller import GPIOController
from .reporter import Reporter

logger = logging.getLogger(__name__)


def _cron_to_trigger(expr: str) -> CronTrigger:
    """支持 5 字段 cron：分 时 日 月 周。"""
    parts = expr.split()
    if len(parts) != 5:
        raise ValueError(f"非法 cron 表达式（需要 5 字段）: {expr!r}")
    minute, hour, day, month, dow = parts
    return CronTrigger(
        minute=minute, hour=hour, day=day, month=month, day_of_week=dow
    )


class WateringScheduler:
    def __init__(
        self,
        cfg: AppConfig,
        gpio: GPIOController,
        reporter: Optional[Reporter],
    ):
        self._cfg = cfg
        self._gpio = gpio
        self._reporter = reporter
        self._scheduler = BackgroundScheduler(timezone="Asia/Shanghai")

    def start(self) -> None:
        self._register_watering_jobs()
        self._register_report_jobs()
        self._scheduler.start()
        logger.info("调度器已启动，任务数: %d", len(self._scheduler.get_jobs()))
        for job in self._scheduler.get_jobs():
            logger.info(" - %s @ %s", job.id, job.trigger)

    def stop(self) -> None:
        self._scheduler.shutdown(wait=False)
        logger.info("调度器已停止")

    # ---------- 注册 ----------
    def _register_watering_jobs(self) -> None:
        for s in self._cfg.schedules:
            ch = self._cfg.get_channel(s.channel_id)
            if ch is None:
                logger.warning("调度任务 %s 引用了不存在的通道 %d，已跳过", s.name, s.channel_id)
                continue
            self._scheduler.add_job(
                func=self._watering_job,
                args=(s.channel_id, s.duration_seconds, s.name),
                trigger=_cron_to_trigger(s.cron),
                id=f"water_{s.name}_{s.channel_id}",
                name=f"浇水-{s.name}",
                replace_existing=True,
                misfire_grace_time=300,
            )

    def _register_report_jobs(self) -> None:
        if not self._cfg.report.enabled or self._reporter is None:
            return
        for idx, expr in enumerate(self._cfg.report.cron_list):
            self._scheduler.add_job(
                func=self._report_job,
                trigger=_cron_to_trigger(expr),
                id=f"report_{idx}",
                name=f"花园日报-{expr}",
                replace_existing=True,
                misfire_grace_time=600,
            )

    # ---------- 运行时新增/取消 ----------
    def add_one_shot_water(
        self, channel_id: int, duration_seconds: int, run_date
    ) -> str:
        from apscheduler.triggers.date import DateTrigger
        job = self._scheduler.add_job(
            func=self._watering_job,
            args=(channel_id, duration_seconds, "manual"),
            trigger=DateTrigger(run_date=run_date),
            misfire_grace_time=120,
        )
        return job.id

    def cancel_job(self, job_id: str) -> bool:
        try:
            self._scheduler.remove_job(job_id)
            return True
        except Exception:
            return False

    # ---------- 任务体 ----------
    def _watering_job(self, channel_id: int, duration: int, label: str) -> None:
        try:
            state = self._gpio.open_valve(channel_id, duration)
            logger.info("[定时浇水/%s] 通道 %d 已开阀 %ds", label, channel_id, duration)
        except Exception:
            logger.exception("[定时浇水/%s] 通道 %d 失败", label, channel_id)

    def _report_job(self) -> None:
        if self._reporter is None:
            return
        try:
            self._reporter.generate_and_send()
        except Exception:
            logger.exception("生成日报失败")
