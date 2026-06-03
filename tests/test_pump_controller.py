"""水泵控制器单元测试。"""
import time
from typing import Any, Dict

import pytest

from hardware.pump_controller import PumpController


def test_pump_init_mock(mock_config: Dict[str, Any]):
    """mock 后端初始化成功。"""
    pump = PumpController(mock_config["channels"], mock=True)
    assert len(pump.channels) == 4
    assert pump.channels[1].name == "热带植物"
    assert pump.channels[2].pin == 27
    pump.cleanup()


def test_water_default_volume(mock_config: Dict[str, Any]):
    """默认水量浇水。"""
    pump = PumpController(mock_config["channels"], mock=True, cooldown_sec=0)
    result = pump.water(1)
    assert result["channel_id"] == 1
    assert result["name"] == "热带植物"
    assert result["volume_ml"] == 800
    assert result["duration_sec"] > 0
    pump.cleanup()


def test_water_custom_volume(mock_config: Dict[str, Any]):
    """自定义水量。"""
    pump = PumpController(mock_config["channels"], mock=True, cooldown_sec=0)
    result = pump.water(2, volume_ml=200)
    assert result["volume_ml"] == 200
    # 200ml / 18ml/s ≈ 11.1s
    assert 10 < result["duration_sec"] < 12
    pump.cleanup()


def test_max_single_run_limit(mock_config: Dict[str, Any]):
    """单次最大时长限制。"""
    pump = PumpController(mock_config["channels"], mock=True, max_single_run_sec=5, cooldown_sec=0)
    # 请求 10000ml，但 max=5s → 实际 5s * 18ml/s = 90ml
    result = pump.water(1, volume_ml=10000)
    assert result["duration_sec"] == 5.0
    assert result["volume_ml"] == 90
    pump.cleanup()


def test_cooldown(mock_config: Dict[str, Any]):
    """冷却时间检查。"""
    pump = PumpController(mock_config["channels"], mock=True, cooldown_sec=2)
    pump.water(1, volume_ml=100)
    with pytest.raises(RuntimeError, match="仍在冷却"):
        pump.water(1, volume_ml=100)
    time.sleep(2.1)
    pump.water(1, volume_ml=100)  # 应该成功
    pump.cleanup()


def test_global_lock(mock_config: Dict[str, Any]):
    """全局互锁：同一时刻只允许一路。"""
    import threading

    pump = PumpController(mock_config["channels"], mock=True, global_lock=True, cooldown_sec=0)
    results = []
    errors = []

    def _water(cid: int):
        try:
            results.append(pump.water(cid, volume_ml=50))
        except Exception as exc:  # noqa: BLE001
            errors.append(str(exc))

    # 并发启动 2 路
    t1 = threading.Thread(target=_water, args=(1,))
    t2 = threading.Thread(target=_water, args=(2,))
    t1.start()
    time.sleep(0.05)
    t2.start()
    t1.join()
    t2.join()

    # 一个成功，一个被拒绝
    assert len(results) == 1
    assert len(errors) == 1
    assert "已有水泵在运行" in errors[0]
    pump.cleanup()


def test_status(mock_config: Dict[str, Any]):
    """状态查询。"""
    pump = PumpController(mock_config["channels"], mock=True, cooldown_sec=0)
    pump.water(3, volume_ml=100)
    status = pump.status()
    assert len(status) == 4
    ch3 = next(s for s in status if s["id"] == 3)
    assert ch3["name"] == "中生植物"
    assert ch3["last_volume_ml"] == 100
    assert ch3["last_run_ts"] > 0
    pump.cleanup()


def test_unknown_channel(mock_config: Dict[str, Any]):
    """未知通道 ID。"""
    pump = PumpController(mock_config["channels"], mock=True)
    with pytest.raises(ValueError, match="未知通道"):
        pump.water(99)
    pump.cleanup()
