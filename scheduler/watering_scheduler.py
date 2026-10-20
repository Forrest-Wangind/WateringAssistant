"""定时浇花调度。

* 4 路电磁阀按 config.yaml 中的 cron 表达式分别触发；
* 每天定时拍照 + AI 报告，结果推送到钉钉；
* 所有任务通过 APScheduler 后台线程执行，不阻塞主进程。
"""
from __future__ import annotations

import logging
from typing import Callable, Dict, List

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

logger = logging.getLogger(__name__)


class WateringScheduler:
    def __init__(self, timezone: str = "Asia/Shanghai") -> None:
        self.scheduler = BackgroundScheduler(timezone=timezone)

    def schedule_channels(self, channels: List[Dict], water_fn: Callable[[int], None]) -> None:
        for ch in channels:
            cron = ch.get("schedule_cron")
            if not cron:
                continue
            try:
                trigger = CronTrigger.from_crontab(cron)
            except Exception as exc:  # noqa: BLE001
                logger.error("通道 %s cron 解析失败 %r: %s", ch.get("name"), cron, exc)
                continue
            ch_id = int(ch["id"])
            self.scheduler.add_job(
                water_fn,
                trigger=trigger,
                args=[ch_id],
                id=f"water_{ch_id}",
                replace_existing=True,
                misfire_grace_time=600,
            )
            logger.info("通道 %s 已注册定时任务: %s", ch["name"], cron)

    def schedule_daily_report(self, hour: int, minute: int, report_fn: Callable[[], None]) -> None:
        trigger = CronTrigger(hour=hour, minute=minute)
        self.scheduler.add_job(
            report_fn,
            trigger=trigger,
            id="daily_report",
            replace_existing=True,
        )
        logger.info("已注册每日报告任务: %02d:%02d", hour, minute)

    def start(self) -> None:
        if not self.scheduler.running:
            self.scheduler.start()
            logger.info("调度器已启动")

    def shutdown(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
            logger.info("调度器已停止")

    def jobs_info(self) -> List[Dict]:
        out = []
        for j in self.scheduler.get_jobs():
            # APScheduler 3.x: next_run_time 是属性
            try:
                next_run = j.next_run_time
            except AttributeError:
                next_run = None
            out.append({
                "id": j.id,
                "next_run_time": next_run.isoformat() if next_run else None,
                "trigger": str(j.trigger),
            })
        return out
