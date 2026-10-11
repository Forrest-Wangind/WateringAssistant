"""摄像头封装。

部署到 Raspberry Pi 时使用 picamera2，开发调试时回退到生成占位图。

设计原则（与项目「绝不静默失败」一致）：
  * 当 mock=False 时，只要真实相机不可用就**抛出 CameraError**，绝不悄悄生成 mock 图；
  * 异常按 not_found / unreachable / unknown 三类归档，并附带可执行的修复提示；
  * 只有在显式配置 `camera.allow_mock_fallback: true` 时才允许降级，且降级日志为 WARNING，
    生成的文件名带 `MOCK_` 前缀，便于一眼识别；
  * 提供 diagnose() 收集环境信息，用于在树莓派上一键排查。
"""
from __future__ import annotations

import glob
import importlib.util
import logging
import os
import platform
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from hardware.errors import HardwareError

logger = logging.getLogger(__name__)

_BOOT_CONFIG = "/boot/firmware/config.txt"
_BOOT_CONFIG_LEGACY = "/boot/config.txt"

# 常见 libcamera 报错关键词 → 归类与修复提示
_NOT_FOUND_HINTS = (
    "no cameras", "no camera", "camera not available", "no device",
    "not detected", "unable to find any camera", "cannot find any camera",
)
_UNREACHABLE_HINTS = (
    "busy", "in use", "could not acquire", "failed to acquire", "resource",
    "timeout", "timed out", "permission", "denied", "eagain",
)


class CameraError(HardwareError):
    """真实相机不可用（kind / hint 见 HardwareError）。"""

    pass


def classify_error(exc: BaseException) -> Tuple[str, str]:
    """把 picamera2 / libcamera 的异常归类成 not_found / unreachable / unknown。"""
    text = f"{type(exc).__name__}: {exc}".lower()

    if isinstance(exc, ModuleNotFoundError) or "no module named" in text:
        return "not_found", (
            "picamera2 / libcamera 未安装，或当前解释器看不到系统包。"
            "树莓派上请执行：sudo apt install -y python3-picamera2 python3-libcamera；"
            "若用 venv，需以 python3 -m venv --system-site-packages .venv 重建"
            "（否则 venv 看不到 apt 安装的包），或直接用 /usr/bin/python3 运行。"
        )
    if any(k in text for k in _NOT_FOUND_HINTS):
        return "not_found", (
            "libcamera 未检测到摄像头。检查：① FPC 排线方向与是否插紧（Pi 5 窄口排线朝向不同）；"
            "② /boot/firmware/config.txt 中 camera_auto_detect=1，且未启用 legacy 的 start_x=1；"
            "③ 先用 rpicam-hello --list-cameras 在系统层确认能否看到相机；改完需重启。"
        )
    if any(k in text for k in _UNREACHABLE_HINTS):
        return "unreachable", (
            "摄像头存在但当前无法访问。检查：① 是否被其它进程占用"
            "（rpicam-hello / 未退出的旧实例 / 另一路 libcamera 客户端）；"
            "② 当前用户是否在 video 组（groups | grep video，必要时 "
            "sudo usermod -aG video $USER 后重新登录）；③ libcamera 组件是否完整。"
        )
    return "unknown", "未识别的相机错误，请查看 ERROR 日志中的完整堆栈。"


# ---------- 真实相机 ----------
def try_open_picamera2(
    resolution: Tuple[int, int], *, warmup_sec: float = 2.0
) -> Tuple[Any, Dict[str, Any]]:
    """尝试打开 picamera2，成功返回 (picam, info)；失败抛 CameraError。"""
    info: Dict[str, Any] = {"resolution": tuple(resolution), "warmup_sec": warmup_sec}
    try:
        from picamera2 import Picamera2  # type: ignore
    except Exception as exc:  # noqa: BLE001
        kind, hint = classify_error(exc)
        raise CameraError(f"导入 picamera2 失败: {exc}", kind=kind, hint=hint) from exc

    try:
        # libcamera 视角下的相机列表，便于日志定位
        try:
            info["cameras"] = [
                {
                    "num": c.get("Num"),
                    "model": c.get("Model"),
                    "location": c.get("Location"),
                }
                for c in Picamera2.global_camera_info()
            ]
        except Exception as exc:  # noqa: BLE001
            info["cameras_error"] = str(exc)

        picam = Picamera2()
        cfg = picam.create_still_configuration(main={"size": tuple(resolution)})
        picam.configure(cfg)
        picam.start()
        time.sleep(warmup_sec)
    except Exception as exc:  # noqa: BLE001
        kind, hint = classify_error(exc)
        raise CameraError(f"初始化 Picamera2 失败: {exc}", kind=kind, hint=hint) from exc

    info["cameras_ok"] = True
    return picam, info


def _module_state(name: str) -> Dict[str, Any]:
    try:
        spec = importlib.util.find_spec(name)
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "error": str(exc)}
    if spec is None:
        return {"available": False, "reason": "not found in sys.path"}
    return {"available": True, "origin": spec.origin}


def _group_names() -> List[str]:
    try:
        import grp

        return sorted({grp.getgrgid(g).gr_name for g in set(os.getgroups())})
    except Exception:  # noqa: BLE001
        return []


def _read_boot_config() -> Dict[str, Any]:
    path = _BOOT_CONFIG if Path(_BOOT_CONFIG).exists() else _BOOT_CONFIG_LEGACY
    info: Dict[str, Any] = {"path": path, "exists": Path(path).exists()}
    if not info["exists"]:
        return info
    try:
        lines = [
            ln.strip()
            for ln in Path(path).read_text(encoding="utf-8", errors="ignore").splitlines()
            if ln.strip() and not ln.strip().startswith("#")
        ]
    except Exception as exc:  # noqa: BLE001
        info["error"] = str(exc)
        return info
    info["camera_lines"] = [
        ln for ln in lines
        if "camera" in ln.lower() or ln.startswith(("dtoverlay", "start_x"))
    ]
    info["camera_auto_detect"] = any("camera_auto_detect=1" in ln for ln in lines)
    info["legacy_start_x"] = any(ln.startswith("start_x=1") for ln in lines)
    return info


def _verdict(info: Dict[str, Any]) -> Dict[str, Any]:
    """基于环境给出明确结论（不猜测，分类归档）。"""
    mods = info.get("modules", {})
    if not mods.get("picamera2", {}).get("available"):
        return {
            "ok": False,
            "kind": "not_found",
            "reason": "当前 Python 解释器找不到 picamera2 模块",
            "hint": (
                "sudo apt install -y python3-picamera2 python3-libcamera；"
                "若使用 venv，请用 --system-site-packages 重建，或直接用系统 python 运行。"
            ),
        }
    if not mods.get("libcamera", {}).get("available"):
        return {
            "ok": False,
            "kind": "not_found",
            "reason": "picamera2 存在，但 libcamera 的 Python 绑定缺失",
            "hint": "sudo apt install -y python3-libcamera 后重试。",
        }
    cfg = info.get("boot_config", {})
    if cfg.get("legacy_start_x"):
        return {
            "ok": False,
            "kind": "unknown",
            "reason": "config.txt 启用了 legacy 相机栈（start_x=1），会与 libcamera 冲突",
            "hint": "注释掉 start_x / gpu_mem 相关行，确保 camera_auto_detect=1，然后重启。",
        }
    if not info.get("device", {}).get("video_nodes"):
        return {
            "ok": False,
            "kind": "not_found",
            "reason": "/dev/video* 不存在，内核未识别到摄像头",
            "hint": "检查 FPC 排线是否插反/未插紧，确认 camera_auto_detect=1 后重启。",
        }
    if not info.get("user", {}).get("in_video_group"):
        return {
            "ok": False,
            "kind": "unreachable",
            "reason": "当前用户不在 video 组，预计无权访问 /dev/video*",
            "hint": "sudo usermod -aG video $USER 后重新登录。",
        }
    return {
        "ok": True,
        "kind": "unknown",
        "reason": "环境层检查通过；若仍失败请查看脚本末尾的真实打开测试结果",
        "hint": "",
    }


def diagnose(include_live_test: bool = False, resolution: Tuple[int, int] = (1920, 1080)) -> Dict[str, Any]:
    """收集相机运行环境信息，供树莓派上一键排查。"""
    out: Dict[str, Any] = {
        "platform": {
            "system": platform.system(),
            "machine": platform.machine(),
            "release": platform.release(),
        },
        "python": {
            "version": sys.version.split()[0],
            "executable": sys.executable,
            "in_venv": sys.prefix != getattr(sys, "base_prefix", sys.prefix),
            "prefix": sys.prefix,
            "base_prefix": getattr(sys, "base_prefix", sys.prefix),
        },
        "modules": {
            "picamera2": _module_state("picamera2"),
            "libcamera": _module_state("libcamera"),
            "PIL": _module_state("PIL"),
        },
        "device": {
            "video_nodes": sorted(glob.glob("/dev/video*")),
            "media_nodes": sorted(glob.glob("/dev/media*")),
            "gpiochips": sorted(glob.glob("/dev/gpiochip*")),
        },
        "user": {
            "uid": os.getuid(),
            "groups": _group_names(),
            "in_video_group": "video" in _group_names(),
        },
        "boot_config": _read_boot_config(),
    }
    out["verdict"] = _verdict(out)

    if include_live_test:
        try:
            picam, info = try_open_picamera2(resolution, warmup_sec=1.0)
            picam.close()
            out["live_test"] = {"ok": True, "info": info}
        except CameraError as exc:
            out["live_test"] = {"ok": False, **exc.as_dict()}
        except Exception as exc:  # noqa: BLE001
            kind, hint = classify_error(exc)
            out["live_test"] = {"ok": False, "kind": kind, "message": str(exc), "hint": hint}
    return out


# ---------- Camera ----------
class Camera:
    def __init__(
        self,
        photo_dir: str,
        *,
        resolution: Tuple[int, int] = (1920, 1080),
        warmup_sec: float = 2.0,
        mock: bool = False,
        allow_mock_fallback: bool = False,
    ) -> None:
        self.photo_dir = Path(photo_dir)
        self.photo_dir.mkdir(parents=True, exist_ok=True)
        self.resolution = tuple(resolution)
        self.warmup_sec = warmup_sec
        self.mock = mock
        self.allow_mock_fallback = allow_mock_fallback
        self._picam = None

        # 供上层展示的状态
        self.error_kind: Optional[str] = None
        self.error_hint: Optional[str] = None
        self.mock_reason: Optional[str] = None
        self.info: Dict[str, Any] = {}

        if not mock:
            try:
                self._picam, self.info = try_open_picamera2(
                    self.resolution, warmup_sec=self.warmup_sec
                )
                logger.info(
                    "Picamera2 已就绪 %s, cameras=%s", self.resolution, self.info.get("cameras")
                )
            except CameraError as exc:
                self.error_kind = exc.kind
                self.error_hint = exc.hint
                self.mock_reason = str(exc)
                # 完整堆栈 + 修复建议：不再只留一行 WARNING
                logger.error(
                    "相机初始化失败 [%s]: %s\n修复建议: %s\n%s",
                    exc.kind, exc, exc.hint, traceback.format_exc(),
                )
                if not allow_mock_fallback:
                    raise
                logger.warning(
                    "⚠️ 已按 camera.allow_mock_fallback=true 降级为 mock："
                    "本进程生成的是占位图，不能用于真实生长分析 (kind=%s)",
                    exc.kind,
                )
                self.mock = True

    # ---------- 状态 ----------
    @property
    def is_mock(self) -> bool:
        """True 表示当前不会使用真实相机。"""
        return self._picam is None

    def status(self) -> Dict[str, Any]:
        return {
            "mode": "mock" if self.is_mock else "picamera2",
            "is_mock": self.is_mock,
            "resolution": list(self.resolution),
            "error_kind": self.error_kind,
            "error_hint": self.error_hint,
            "mock_reason": self.mock_reason,
            "cameras": self.info.get("cameras"),
        }

    # ---------- 拍照 ----------
    def capture(self, label: str = "garden") -> Path:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        prefix = "MOCK_" if self.is_mock else ""
        path = self.photo_dir / f"{prefix}{label}_{ts}.jpg"

        if self._picam is not None:
            try:
                self._picam.capture_file(str(path))
            except Exception as exc:  # noqa: BLE001
                kind, hint = classify_error(exc)
                logger.error("拍照失败 [%s]: %s\n%s", kind, exc, traceback.format_exc())
                raise CameraError(f"拍照失败: {exc}", kind=kind, hint=hint) from exc
            logger.info("摄像头已拍照: %s", path)
            return path

        return self._mock_capture(path)

    def _mock_capture(self, path: Path) -> Path:
        from PIL import Image, ImageDraw, ImageFont

        img = Image.new("RGB", self.resolution, color=(34, 89, 50))
        draw = ImageDraw.Draw(img)
        text = (
            f"MOCK GARDEN PHOTO\n{datetime.now().isoformat(timespec='seconds')}\n"
            f"reason: {self.mock_reason or 'mock_hardware=true'}"
        )
        try:
            font = ImageFont.load_default()
        except Exception:  # noqa: BLE001
            font = None
        draw.text((40, 40), text, fill=(255, 255, 255), font=font)
        img.save(path, format="JPEG", quality=85)
        logger.warning("[MOCK] 已生成占位照片（非真实画面）: %s", path)
        return path

    def close(self) -> None:
        if self._picam is not None:
            try:
                self._picam.close()
            except Exception:  # noqa: BLE001
                pass
            self._picam = None
