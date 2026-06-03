"""调度器单元测试。"""
import time
from typing import Any, Dict

from scheduler.watering_scheduler import WateringScheduler


def test_scheduler_init():
    sched = WateringScheduler(timezone="Asia/Shanghai")
    assert sched.scheduler is not None
    assert not sched.scheduler.running
    sched.shutdown()


def test_schedule_channels(mock_config: Dict[str, Any]):
    """注册 4 路定时任务。"""
    sched = WateringScheduler()
    called = []

    def _water(cid: int):
        called.append(cid)

    sched.schedule_channels(mock_config["channels"], _water)
    jobs = sched.jobs_info()
    assert len(jobs) == 4
    assert all("water_" in j["id"] for j in jobs)
    sched.shutdown()


def test_schedule_daily_report():
    sched = WateringScheduler()
    called = []

    def _report():
        called.append(1)

    sched.schedule_daily_report(hour=12, minute=30, report_fn=_report)
    jobs = sched.jobs_info()
    assert len(jobs) == 1
    assert jobs[0]["id"] == "daily_report"
    sched.shutdown()


def test_scheduler_start_stop():
    sched = WateringScheduler()
    sched.start()
    assert sched.scheduler.running
    sched.shutdown()
    assert not sched.scheduler.running


def test_scheduler_invalid_cron(mock_config: Dict[str, Any]):
    """无效 cron 表达式跳过。"""
    sched = WateringScheduler()
    bad_channels = [{**mock_config["channels"][0], "schedule_cron": "invalid cron"}]
    sched.schedule_channels(bad_channels, lambda cid: None)
    jobs = sched.jobs_info()
    assert len(jobs) == 0  # 解析失败，未注册
    sched.shutdown()
