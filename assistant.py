"""核心业务编排：把硬件、AI、钉钉、调度串联起来。"""
from __future__ import annotations

import logging
import threading
from typing import Any, Dict, Optional

from ai.plant_analyzer import AnalysisReport, PlantAnalyzer
from dingtalk.bot import Command, DingTalkBot, help_text, parse_command
from hardware.camera import Camera
from hardware.pump_controller import PumpController
from utils import find_channel_by_name, get_channel

logger = logging.getLogger(__name__)


class WateringAssistant:
    def __init__(self, cfg: Dict[str, Any]) -> None:
        self.cfg = cfg
        sys_cfg = cfg.get("system", {})
        safety = cfg.get("safety", {})

        self.pump = PumpController(
            channels=cfg["channels"],
            mock=bool(sys_cfg.get("mock_hardware", False)),
            max_single_run_sec=int(safety.get("max_single_run_sec", 180)),
            cooldown_sec=int(safety.get("cooldown_sec", 30)),
            global_lock=bool(safety.get("global_lock", True)),
        )
        self.camera = Camera(
            photo_dir=sys_cfg.get("photo_dir", "./data/photos"),
            resolution=tuple(cfg.get("camera", {}).get("resolution", (1920, 1080))),
            warmup_sec=float(cfg.get("camera", {}).get("warmup_sec", 2.0)),
            mock=bool(sys_cfg.get("mock_hardware", False)),
        )
        self.analyzer = PlantAnalyzer(
            ai_cfg=cfg.get("ai", {}),
            report_dir=sys_cfg.get("report_dir", "./data/reports"),
        )
        self.bot = DingTalkBot(cfg.get("dingtalk", {}))
        self._water_lock = threading.Lock()

    # ---------- 浇水 ----------
    def water_channel(self, channel_id: int, volume_ml: Optional[int] = None,
                       notify: bool = True) -> Dict[str, Any]:
        ch_cfg = get_channel(self.cfg, channel_id)
        if ch_cfg is None:
            raise ValueError(f"未知通道 {channel_id}")
        with self._water_lock:
            result = self.pump.water(channel_id, volume_ml)
        if notify:
            self.bot.send_text(
                f"✅ 已为「{result['name']}」浇水 {result['volume_ml']}ml"
                f"（{result['duration_sec']}s）"
            )
        return result

    # ---------- 报告 ----------
    def take_photo_and_report(self, push: bool = True) -> AnalysisReport:
        photo = self.camera.capture("garden")
        if push:
            self.bot.send_text(f"📷 已拍摄花园照片: {photo.name}，正在分析…")
        report = self.analyzer.analyze(photo)
        if push:
            title = "🌿 花园生长报告"
            score = f"  健康评分 {report.score}/5" if report.score else ""
            self.bot.send_markdown(title, f"# {title}{score}\n\n{report.markdown}")
        return report

    def take_photo_only(self, push: bool = True) -> str:
        photo = self.camera.capture("garden")
        if push:
            self.bot.send_text(f"📷 已拍照: {photo.resolve()}")
        return str(photo.resolve())

    # ---------- 状态 ----------
    def status_text(self) -> str:
        rows = ["**🌱 浇花助理状态**", "", "| 通道 | 植物 | 桶 | 上次浇水(ml) | 计划 |", "|---|---|---|---|---|"]
        for s in self.pump.status():
            rows.append(
                f"| {s['id']} | {s['name']} | {s['bucket']} | "
                f"{s['last_volume_ml']} | `{s['schedule_cron'] or '-'}` |"
            )
        return "\n".join(rows)

    # ---------- 钉钉指令分发 ----------
    def handle_command(self, text: str, sender_id: Optional[str] = None) -> str:
        cmd: Command = parse_command(text, sender_id=sender_id)
        allowed = self.cfg.get("dingtalk", {}).get("allowed_users") or []
        if allowed and sender_id and sender_id not in allowed:
            return "🚫 您没有权限执行该指令"

        try:
            if cmd.action == "help":
                return help_text()
            if cmd.action == "status":
                return self.status_text()
            if cmd.action == "photo":
                path = self.take_photo_only(push=False)
                return f"📷 已拍照: {path}"
            if cmd.action == "report":
                report = self.take_photo_and_report(push=False)
                score = f"  健康评分 {report.score}/5" if report.score else ""
                return f"# 🌿 花园生长报告{score}\n\n{report.markdown}"
            if cmd.action == "water":
                ch = self._resolve_channel(cmd.target or "")
                if not ch:
                    return f"⚠️ 找不到对应植物或通道：{cmd.target}\n\n{help_text()}"
                result = self.water_channel(int(ch["id"]), cmd.volume_ml, notify=False)
                return (
                    f"✅ 已为「{result['name']}」浇水 {result['volume_ml']}ml "
                    f"（{result['duration_sec']}s）"
                )
            return f"❓ 无法识别指令：{cmd.raw}\n\n{help_text()}"
        except Exception as exc:  # noqa: BLE001
            logger.exception("指令执行失败")
            return f"❌ 执行失败：{exc}"

    def _resolve_channel(self, target: str) -> Optional[Dict[str, Any]]:
        if not target:
            return None
        # 通道2 / channel2 / 2
        import re

        m = re.match(r"^(?:通道|channel)?\s*(\d+)$", target.strip(), re.IGNORECASE)
        if m:
            return get_channel(self.cfg, int(m.group(1)))
        return find_channel_by_name(self.cfg, target)

    # ---------- 资源释放 ----------
    def shutdown(self) -> None:
        self.pump.cleanup()
        self.camera.close()
