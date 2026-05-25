"""读取 config.yaml 与环境变量，集中暴露配置对象。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

import yaml
from dotenv import load_dotenv

load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config.yaml"


@dataclass
class ChannelConfig:
    id: int
    name: str
    gpio_pin: int


@dataclass
class RelayConfig:
    active_low: bool = True
    max_duration_seconds: int = 600
    default_duration_seconds: int = 30


@dataclass
class ScheduleConfig:
    name: str
    cron: str
    channel_id: int
    duration_seconds: int


@dataclass
class ReportConfig:
    enabled: bool = True
    cron_list: List[str] = field(default_factory=list)
    image_width: int = 1280
    image_height: int = 720
    recipient_user_ids: List[str] = field(default_factory=list)


@dataclass
class VisionConfig:
    provider: str = "dashscope"
    model: str = "qwen-vl-max"
    base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    timeout: int = 60


@dataclass
class StorageConfig:
    photo_dir: Path
    report_dir: Path
    keep_days: int = 30


@dataclass
class AppConfig:
    channels: List[ChannelConfig]
    relay: RelayConfig
    schedules: List[ScheduleConfig]
    report: ReportConfig
    vision: VisionConfig
    storage: StorageConfig

    # 环境变量
    deepseek_api_key: Optional[str] = None
    dashscope_api_key: Optional[str] = None
    dingtalk_client_id: Optional[str] = None
    dingtalk_client_secret: Optional[str] = None
    dingtalk_robot_code: Optional[str] = None
    recipient_user_ids_env: List[str] = field(default_factory=list)

    def get_channel(self, channel_id: int) -> Optional[ChannelConfig]:
        for ch in self.channels:
            if ch.id == channel_id:
                return ch
        return None

    @property
    def report_recipients(self) -> List[str]:
        # .env 优先于 config.yaml
        if self.recipient_user_ids_env:
            return self.recipient_user_ids_env
        return self.report.recipient_user_ids


def _split_csv(value: Optional[str]) -> List[str]:
    if not value:
        return []
    return [x.strip() for x in value.split(",") if x.strip()]


def load_config(config_path: Path = CONFIG_PATH) -> AppConfig:
    with config_path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    channels = [ChannelConfig(**c) for c in raw.get("channels", [])]
    relay = RelayConfig(**raw.get("relay", {}))
    schedules = [ScheduleConfig(**s) for s in raw.get("schedules", [])]
    report = ReportConfig(**raw.get("report", {}))
    vision = VisionConfig(**raw.get("vision", {}))

    storage_raw = raw.get("storage", {})
    photo_dir = (PROJECT_ROOT / storage_raw.get("photo_dir", "data/photos")).resolve()
    report_dir = (PROJECT_ROOT / storage_raw.get("report_dir", "data/reports")).resolve()
    photo_dir.mkdir(parents=True, exist_ok=True)
    report_dir.mkdir(parents=True, exist_ok=True)
    storage = StorageConfig(
        photo_dir=photo_dir,
        report_dir=report_dir,
        keep_days=storage_raw.get("keep_days", 30),
    )

    return AppConfig(
        channels=channels,
        relay=relay,
        schedules=schedules,
        report=report,
        vision=vision,
        storage=storage,
        deepseek_api_key=os.getenv("DEEPSEEK_API_KEY"),
        dashscope_api_key=os.getenv("DASHSCOPE_API_KEY"),
        dingtalk_client_id=os.getenv("DINGTALK_CLIENT_ID"),
        dingtalk_client_secret=os.getenv("DINGTALK_CLIENT_SECRET"),
        dingtalk_robot_code=os.getenv("DINGTALK_ROBOT_CODE"),
        recipient_user_ids_env=_split_csv(os.getenv("DINGTALK_RECIPIENT_USER_IDS")),
    )
