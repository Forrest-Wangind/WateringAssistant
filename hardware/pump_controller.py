"""4 路水泵控制器。

通过 GPIO 控制低电平触发的继电器模块，进而驱动 12V 直流水泵。
设计原则：
  * 软硬限位（最大单次时长、冷却、全局互锁）保证不会因为软件 bug 把水泵烧掉。
  * 在非树莓派环境提供 mock 实现，方便本地开发和单元测试。
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


# ---------- GPIO 后端 ----------
class _GpioBackend:
    HIGH = 1
    LOW = 0

    def setup(self, pin: int) -> None: ...
    def write(self, pin: int, value: int) -> None: ...
    def cleanup(self) -> None: ...


class _RpiGpioBackend(_GpioBackend):
    def __init__(self) -> None:
        import RPi.GPIO as GPIO  # type: ignore

        self._GPIO = GPIO
        GPIO.setmode(GPIO.BCM)
        GPIO.setwarnings(False)

    def setup(self, pin: int) -> None:
        self._GPIO.setup(pin, self._GPIO.OUT, initial=self._GPIO.HIGH)  # 继电器低电平触发，初始置高=关

    def write(self, pin: int, value: int) -> None:
        # 0 = 打开水泵（继电器吸合），1 = 关闭水泵
        self._GPIO.output(pin, self._GPIO.LOW if value else self._GPIO.HIGH)

    def cleanup(self) -> None:
        self._GPIO.cleanup()


class _MockGpioBackend(_GpioBackend):
    def __init__(self) -> None:
        self.state: Dict[int, int] = {}

    def setup(self, pin: int) -> None:
        self.state[pin] = 0
        logger.info("[MOCK GPIO] setup pin=%s", pin)

    def write(self, pin: int, value: int) -> None:
        self.state[pin] = value
        logger.info("[MOCK GPIO] pin=%s -> %s", pin, "ON" if value else "OFF")

    def cleanup(self) -> None:
        logger.info("[MOCK GPIO] cleanup")
        self.state.clear()


def _make_backend(mock: bool) -> _GpioBackend:
    if mock:
        return _MockGpioBackend()
    try:
        return _RpiGpioBackend()
    except Exception as exc:  # noqa: BLE001
        logger.warning("RPi.GPIO 不可用，自动回退到 mock 模式: %s", exc)
        return _MockGpioBackend()


# ---------- 通道 ----------
@dataclass
class Channel:
    id: int
    name: str
    pin: int
    bucket: str
    flow_ml_per_sec: float
    default_volume_ml: int
    schedule_cron: str = ""
    last_run_ts: float = 0.0
    last_volume_ml: int = 0
    is_running: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


class PumpController:
    def __init__(
        self,
        channels: List[Dict],
        *,
        mock: bool = False,
        max_single_run_sec: int = 180,
        cooldown_sec: int = 30,
        global_lock: bool = True,
    ) -> None:
        self.backend = _make_backend(mock)
        self.channels: Dict[int, Channel] = {}
        for c in channels:
            ch = Channel(
                id=int(c["id"]),
                name=c["name"],
                pin=int(c["pin"]),
                bucket=c.get("bucket", ""),
                flow_ml_per_sec=float(c["flow_ml_per_sec"]),
                default_volume_ml=int(c["default_volume_ml"]),
                schedule_cron=c.get("schedule_cron", ""),
            )
            self.backend.setup(ch.pin)
            self.channels[ch.id] = ch

        self.max_single_run_sec = max_single_run_sec
        self.cooldown_sec = cooldown_sec
        self._global_lock: Optional[threading.Lock] = threading.Lock() if global_lock else None

    # 计算需要运行的秒数
    def _seconds_for_volume(self, ch: Channel, volume_ml: int) -> float:
        seconds = volume_ml / max(ch.flow_ml_per_sec, 0.1)
        return min(seconds, self.max_single_run_sec)

    def water(self, channel_id: int, volume_ml: Optional[int] = None) -> Dict:
        if channel_id not in self.channels:
            raise ValueError(f"未知通道 id={channel_id}")
        ch = self.channels[channel_id]
        volume = int(volume_ml) if volume_ml else ch.default_volume_ml

        # 冷却检查
        now = time.time()
        if now - ch.last_run_ts < self.cooldown_sec:
            wait = int(self.cooldown_sec - (now - ch.last_run_ts))
            raise RuntimeError(f"通道 {ch.name} 仍在冷却，请 {wait}s 后再试")

        seconds = self._seconds_for_volume(ch, volume)
        actual_volume = int(seconds * ch.flow_ml_per_sec)

        glock = self._global_lock if self._global_lock else None
        if glock and not glock.acquire(timeout=1):
            raise RuntimeError("已有水泵在运行，本次请求被拒绝")

        try:
            with ch.lock:
                ch.is_running = True
                logger.info("通道 %s 开始浇水: 目标 %sml → 运行 %.1fs", ch.name, volume, seconds)
                self.backend.write(ch.pin, 1)
                try:
                    time.sleep(seconds)
                finally:
                    self.backend.write(ch.pin, 0)
                ch.last_run_ts = time.time()
                ch.last_volume_ml = actual_volume
                ch.is_running = False
        finally:
            if glock:
                glock.release()

        logger.info("通道 %s 浇水完成: 实际 %sml", ch.name, actual_volume)
        return {
            "channel_id": ch.id,
            "name": ch.name,
            "duration_sec": round(seconds, 2),
            "volume_ml": actual_volume,
        }

    def status(self) -> List[Dict]:
        out = []
        for ch in self.channels.values():
            out.append({
                "id": ch.id,
                "name": ch.name,
                "pin": ch.pin,
                "bucket": ch.bucket,
                "is_running": ch.is_running,
                "last_run_ts": ch.last_run_ts,
                "last_volume_ml": ch.last_volume_ml,
                "schedule_cron": ch.schedule_cron,
            })
        return out

    def cleanup(self) -> None:
        for ch in self.channels.values():
            try:
                self.backend.write(ch.pin, 0)
            except Exception:  # noqa: BLE001
                pass
        self.backend.cleanup()
