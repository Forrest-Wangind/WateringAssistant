"""电磁阀控制器单元测试。"""
import time
from typing import Any, Dict

import pytest

from hardware.valve_controller import ValveController


def test_valve_init_mock(mock_config: Dict[str, Any]):
    """mock 后端初始化成功。"""
    valve = ValveController(mock_config["channels"], mock=True)
    assert len(valve.channels) == 4
    assert valve.channels[1].name == "热带植物"
    assert valve.channels[2].pin == 27
    valve.cleanup()


def test_water_default_duration(mock_config: Dict[str, Any]):
    """默认时长浇水。"""
    valve = ValveController(mock_config["channels"], mock=True, cooldown_sec=0)
    result = valve.water(1)
    assert result["channel_id"] == 1
    assert result["name"] == "热带植物"
    assert result["duration_sec"] == 45
    valve.cleanup()


def test_water_custom_duration(mock_config: Dict[str, Any]):
    """自定义时长。"""
    valve = ValveController(mock_config["channels"], mock=True, cooldown_sec=0)
    result = valve.water(2, duration_sec=10)
    assert result["duration_sec"] == 10
    valve.cleanup()


def test_max_single_run_limit(mock_config: Dict[str, Any]):
    """单次最大时长限制。"""
    valve = ValveController(mock_config["channels"], mock=True, max_single_run_sec=5, cooldown_sec=0)
    result = valve.water(1, duration_sec=100)
    assert result["duration_sec"] == 5.0
    valve.cleanup()


def test_cooldown(mock_config: Dict[str, Any]):
    """冷却时间检查。"""
    valve = ValveController(mock_config["channels"], mock=True, cooldown_sec=2)
    valve.water(1, duration_sec=5)
    with pytest.raises(RuntimeError, match="仍在冷却"):
        valve.water(1, duration_sec=5)
    time.sleep(2.1)
    valve.water(1, duration_sec=5)  # 应该成功
    valve.cleanup()


def test_global_lock(mock_config: Dict[str, Any]):
    """全局互锁：同一时刻只允许一路。"""
    import threading

    valve = ValveController(mock_config["channels"], mock=True, global_lock=True, cooldown_sec=0)
    results = []
    errors = []

    def _water(cid: int):
        try:
            results.append(valve.water(cid, duration_sec=3))
        except Exception as exc:  # noqa: BLE001
            errors.append(str(exc))

    t1 = threading.Thread(target=_water, args=(1,))
    t2 = threading.Thread(target=_water, args=(2,))
    t1.start()
    time.sleep(0.05)
    t2.start()
    t1.join()
    t2.join()

    assert len(results) == 1
    assert len(errors) == 1
    assert "已有电磁阀在运行" in errors[0]
    valve.cleanup()


def test_status(mock_config: Dict[str, Any]):
    """状态查询。"""
    valve = ValveController(mock_config["channels"], mock=True, cooldown_sec=0)
    valve.water(3, duration_sec=5)
    status = valve.status()
    assert len(status) == 4
    ch3 = next(s for s in status if s["id"] == 3)
    assert ch3["name"] == "中生植物"
    assert ch3["last_duration_sec"] == 5
    assert ch3["last_run_ts"] > 0
    valve.cleanup()


def test_unknown_channel(mock_config: Dict[str, Any]):
    """未知通道 ID。"""
    valve = ValveController(mock_config["channels"], mock=True)
    with pytest.raises(ValueError, match="未知通道"):
        valve.water(99)
    valve.cleanup()
