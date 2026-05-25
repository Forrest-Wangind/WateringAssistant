"""多路浇水继电器控制。

仅在树莓派上可用；导入 RPi.GPIO 失败时给出明确错误。
通过线程定时器实现"开阀 N 秒后自动关阀"，并发用锁互斥（同一通道）。
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

from .config import ChannelConfig, RelayConfig

logger = logging.getLogger(__name__)

try:
    import RPi.GPIO as GPIO  # type: ignore
    _GPIO_AVAILABLE = True
except (ImportError, RuntimeError) as e:
    GPIO = None  # type: ignore
    _GPIO_AVAILABLE = False
    _IMPORT_ERROR = e
else:
    _IMPORT_ERROR = None


@dataclass
class ChannelState:
    channel_id: int
    name: str
    is_open: bool
    opened_at: Optional[float]
    will_close_at: Optional[float]


class GPIOUnavailableError(RuntimeError):
    pass


class GPIOController:
    def __init__(self, channels: List[ChannelConfig], relay: RelayConfig):
        if not _GPIO_AVAILABLE:
            raise GPIOUnavailableError(
                f"RPi.GPIO 不可用（{_IMPORT_ERROR}）。本程序需在树莓派上运行。"
            )

        self._channels: Dict[int, ChannelConfig] = {c.id: c for c in channels}
        self._relay = relay
        self._timers: Dict[int, threading.Timer] = {}
        self._opened_at: Dict[int, float] = {}
        self._will_close_at: Dict[int, float] = {}
        self._lock = threading.Lock()

        GPIO.setmode(GPIO.BCM)
        GPIO.setwarnings(False)
        for ch in channels:
            GPIO.setup(ch.gpio_pin, GPIO.OUT)
            GPIO.output(ch.gpio_pin, self._closed_level())
        logger.info("GPIO 初始化完成，通道数: %d", len(channels))

    # ---------- 电平辅助 ----------
    def _open_level(self) -> int:
        return GPIO.LOW if self._relay.active_low else GPIO.HIGH

    def _closed_level(self) -> int:
        return GPIO.HIGH if self._relay.active_low else GPIO.LOW

    # ---------- 公共接口 ----------
    def list_channels(self) -> List[ChannelState]:
        with self._lock:
            return [
                ChannelState(
                    channel_id=ch.id,
                    name=ch.name,
                    is_open=ch.id in self._opened_at,
                    opened_at=self._opened_at.get(ch.id),
                    will_close_at=self._will_close_at.get(ch.id),
                )
                for ch in self._channels.values()
            ]

    def open_valve(self, channel_id: int, duration_seconds: int) -> ChannelState:
        ch = self._channels.get(channel_id)
        if ch is None:
            raise ValueError(f"通道 {channel_id} 不存在")

        duration = max(1, min(duration_seconds, self._relay.max_duration_seconds))
        if duration != duration_seconds:
            logger.warning(
                "通道 %d 时长 %ds 已被钳制到 %ds（上限 %ds）",
                channel_id,
                duration_seconds,
                duration,
                self._relay.max_duration_seconds,
            )

        with self._lock:
            self._cancel_timer_locked(channel_id)
            GPIO.output(ch.gpio_pin, self._open_level())
            now = time.time()
            self._opened_at[channel_id] = now
            self._will_close_at[channel_id] = now + duration

            timer = threading.Timer(duration, self._auto_close, args=(channel_id,))
            timer.daemon = True
            timer.start()
            self._timers[channel_id] = timer

            logger.info("通道 %d (%s) 开阀，时长 %ds", channel_id, ch.name, duration)
            return ChannelState(
                channel_id=channel_id,
                name=ch.name,
                is_open=True,
                opened_at=now,
                will_close_at=now + duration,
            )

    def close_valve(self, channel_id: int) -> ChannelState:
        ch = self._channels.get(channel_id)
        if ch is None:
            raise ValueError(f"通道 {channel_id} 不存在")

        with self._lock:
            self._cancel_timer_locked(channel_id)
            GPIO.output(ch.gpio_pin, self._closed_level())
            self._opened_at.pop(channel_id, None)
            self._will_close_at.pop(channel_id, None)
            logger.info("通道 %d (%s) 关阀", channel_id, ch.name)
            return ChannelState(
                channel_id=channel_id,
                name=ch.name,
                is_open=False,
                opened_at=None,
                will_close_at=None,
            )

    def close_all(self) -> None:
        for ch_id in list(self._channels.keys()):
            try:
                self.close_valve(ch_id)
            except Exception:
                logger.exception("关闭通道 %d 失败", ch_id)

    def cleanup(self) -> None:
        try:
            self.close_all()
        finally:
            GPIO.cleanup()
            logger.info("GPIO 已清理")

    # ---------- 内部 ----------
    def _cancel_timer_locked(self, channel_id: int) -> None:
        timer = self._timers.pop(channel_id, None)
        if timer is not None:
            timer.cancel()

    def _auto_close(self, channel_id: int) -> None:
        try:
            self.close_valve(channel_id)
        except Exception:
            logger.exception("自动关阀失败: 通道 %d", channel_id)
