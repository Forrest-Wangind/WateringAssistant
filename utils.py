"""通用工具：配置加载与日志。"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict

import yaml
from dotenv import load_dotenv

_CONFIG_CACHE: Dict[str, Any] | None = None


def load_config(path: str | os.PathLike[str] = "config.yaml") -> Dict[str, Any]:
    """加载 YAML 配置，并在第一次调用时载入 .env。"""
    global _CONFIG_CACHE
    if _CONFIG_CACHE is not None:
        return _CONFIG_CACHE

    load_dotenv()
    cfg_path = Path(path)
    if not cfg_path.exists():
        raise FileNotFoundError(f"配置文件不存在: {cfg_path}")
    with cfg_path.open("r", encoding="utf-8") as fp:
        cfg = yaml.safe_load(fp) or {}

    for sub in ("data_dir", "photo_dir", "report_dir"):
        d = cfg.get("system", {}).get(sub)
        if d:
            Path(d).mkdir(parents=True, exist_ok=True)

    _CONFIG_CACHE = cfg
    return cfg


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


def get_channel(cfg: Dict[str, Any], channel_id: int) -> Dict[str, Any] | None:
    for ch in cfg.get("channels", []):
        if int(ch["id"]) == int(channel_id):
            return ch
    return None


def find_channel_by_name(cfg: Dict[str, Any], name: str) -> Dict[str, Any] | None:
    name = name.strip()
    for ch in cfg.get("channels", []):
        if name == ch["name"] or name in ch["name"]:
            return ch
    return None
