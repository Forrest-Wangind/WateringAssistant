"""摄像头封装。

部署到 Raspberry Pi 时使用 picamera2，开发调试时回退到生成占位图。

设计原则（与项目「绝不静默失败」一致）：
  * 当 mock=False 时，只要真实相机不可用就**抛出 CameraError**，绝不悄悄生成 mock 图；
  * 异常按 not_found / unreachable / unknown 三类归档，并附带可执行的修复提示；
  * 只有在显式配置 `camera.allow_mock_fallback: true` 时才允许降级，且降级日志为 WARNING，
    生成的文件名带 `MOCK_` 前缀，便于一眼识别；
  * 提供 diagnose() 收集环境信息，用于在树莓派上一键排查。

关于「能拍出图，但很模糊」（重要）：
  能出图 ≠ 拍得清楚。本项目相机是 **Pi Camera v3 / IMX708**，它带 VCM 自动对焦（AF），
  而 libcamera 不会在拍照瞬间自动帮你对一次焦。原先只做 `start()` + `sleep()` 就直接
  `capture_file()` 属于**盲拍** —— 对焦环可能停在无穷远，拍 0.5~2m 外的花架必然整幅发虚。
  因此本模块在拍照前会：
    1. 显式触发一次对焦循环（autofocus_cycle）并确认锁定状态；
    2. 丢弃若干帧让 AE/AWB 收敛；
    3. 限制曝光上限，抑制慢快门造成的水叶拖影（运动模糊）；
    4. 记录 ExposureTime / AnalogueGain / LensPosition / AfState 等元数据，便于事后归因；
    5. 对成片做清晰度体检（assess_image / judge_quality），低于阈值时显式告警。
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

# ---------------------------------------------------------------- 拍摄参数
# 全部可在 config.yaml 的 camera: 段覆盖（见 DEFAULT_CAPTURE_SETTINGS 的键名）。
DEFAULT_CAPTURE_SETTINGS: Dict[str, Any] = {
    # 对焦：auto = 拍照前跑一次单次对焦循环（静止场景首选）；
    #       continuous = 持续对焦（适合移动目标，但低光/低纹理下会来回"找虚"）；
    #       manual = 固定镜头位置（lens_position，单位屈光度：0=无穷远，1=1m，10=10cm）；
    #       off = 完全不动对焦（仅用于排查）。
    # 注意：Picamera2 在 Continuous 模式下调用 autofocus_cycle() 会抛 RuntimeError，
    #       所以这里默认用 auto + 显式触发，而不是 continuous。
    "af_mode": "auto",
    "af_range": "normal",          # normal | macro（近摄 <30cm 时用 macro）
    "af_wait_sec": 6.0,            # 等待对焦稳定的上限（continuous 模式生效）
    "lens_position": None,         # 仅 manual 模式使用
    # 曝光：IMX708 曝光最长可到 ~1.7s，暗光下 AEC 会拉到很长 → 有风时叶子必拖影。
    # 限制帧时长上限即限制最长曝光。66666us = 1/15s。
    "max_exposure_us": 66666,
    "exposure_mode": "auto",       # auto | short | long（short 优先短曝光，减少运动模糊）
    "sharpness_boost": 1.5,        # ISP 锐化，默认 1.0
    "jpeg_quality": 95,
    "settle_frames": 2,            # 拍照前丢弃的帧数，让 AE/AWB 收敛
    # 画质门禁：成片清晰度/对比度低于阈值时显式告警（不阻止拍照，但绝不沉默）
    "quality_guard": {
        "enabled": True,
        "min_sharpness": 8.0,      # 经验下限，见 tools/camera_diag.py 的标定说明
        "min_contrast": 12.0,
    },
}

# IMX708 / IMX219 的原生 sensor mode。请求非原生尺寸时 ISP 需重采样，锐度会有损失。
_NATIVE_SENSOR_MODES: Dict[str, Tuple[Tuple[int, int], ...]] = {
    "imx708": ((4608, 2592), (2304, 1296), (1536, 864)),
    "imx219": ((3280, 2464), (1640, 1232), (1920, 1080), (640, 480)),
    "imx477": ((4056, 3040), (2028, 1520), (1332, 990)),
}

_AF_STATE_NAMES = {0: "Idle", 1: "Scanning", 2: "Focused", 3: "Failed"}


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


# ---------- 拍摄参数解析 ----------
def parse_capture_settings(raw: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """把 config.yaml 的 camera: 段合并进默认拍摄参数（缺省项用默认值）。"""
    s = dict(DEFAULT_CAPTURE_SETTINGS)
    raw = dict(raw or {})
    # config.yaml 里对外暴露的名字是 autofocus，内部字段叫 af_mode —— 两个都认，
    # 否则用户在配置里写了 autofocus 却毫无效果（静默失效）。
    if raw.get("af_mode") is None and raw.get("autofocus") is not None:
        raw["af_mode"] = raw["autofocus"]
    for key in (
        "af_mode", "af_range", "af_wait_sec", "lens_position",
        "max_exposure_us", "exposure_mode", "sharpness_boost",
        "jpeg_quality", "settle_frames",
    ):
        if raw.get(key) is not None:
            s[key] = raw[key]
    s["af_mode"] = str(s["af_mode"]).lower()
    s["af_range"] = str(s["af_range"]).lower()
    s["exposure_mode"] = str(s["exposure_mode"]).lower()
    s["af_wait_sec"] = float(s["af_wait_sec"])
    s["settle_frames"] = int(s["settle_frames"])
    s["jpeg_quality"] = int(s["jpeg_quality"])

    qg = dict(DEFAULT_CAPTURE_SETTINGS["quality_guard"])
    qg.update(raw.get("quality_guard") or {})
    s["quality_guard"] = qg
    return s


def native_mode_hint(model: Optional[str], resolution: Tuple[int, int]) -> Optional[str]:
    """请求的分辨率若不在传感器原生模式里，返回提示（否则 None）。"""
    if not model:
        return None
    key = str(model).lower()
    modes = next((v for k, v in _NATIVE_SENSOR_MODES.items() if k in key), None)
    if not modes:
        return None
    if tuple(resolution) in modes:
        return None
    native = "、".join(f"{w}x{h}" for w, h in modes)
    return (
        f"{resolution[0]}x{resolution[1]} 不是 {model} 的原生模式（原生：{native}），"
        f"ISP 需要重采样，画面会偏软。建议改用原生模式，例如 "
        f"{modes[1][0]}x{modes[1][1]}（全视野，无缩放）。"
    )


def _libcamera_controls() -> Any:
    try:
        from libcamera import controls  # type: ignore

        return controls
    except Exception:  # noqa: BLE001
        return None


def _enum_attr(root: Any, dotted: str, name: str) -> Any:
    """按 'draft.NoiseReductionModeEnum' 这类路径取值，兼容不同 libcamera 版本。"""
    obj = root
    for part in dotted.split("."):
        obj = getattr(obj, part, None)
        if obj is None:
            return None
    return getattr(obj, name, None)


def _build_controls(libcontrols: Any, settings: Dict[str, Any]) -> Dict[str, Any]:
    """把设置翻译成 libcamera 控件字典。"""
    out: Dict[str, Any] = {}
    if libcontrols is None:
        return out

    af_mode = settings.get("af_mode", "auto")
    af_enum = getattr(libcontrols, "AfModeEnum", None)
    if af_enum is not None:
        wanted = {"auto": "Auto", "continuous": "Continuous", "manual": "Manual"}.get(af_mode)
        if wanted:
            value = getattr(af_enum, wanted, None)
            if value is not None:
                out["AfMode"] = value
    if af_mode == "manual" and settings.get("lens_position") is not None:
        out["LensPosition"] = float(settings["lens_position"])
    if af_mode in ("auto", "manual"):
        af_range = getattr(libcontrols, "AfRangeEnum", None)
        if af_range is not None and settings.get("af_range") == "macro":
            value = getattr(af_range, "Macro", None)
            if value is not None:
                out["AfRange"] = value

    max_us = settings.get("max_exposure_us")
    if max_us:
        # (最短, 最长) 帧时长 = 曝光上限；同时保留自动曝光
        out["FrameDurationLimits"] = (1000, int(max_us))

    exposure_mode = settings.get("exposure_mode", "auto")
    if exposure_mode in ("short", "long"):
        ae_enum = getattr(libcontrols, "AeExposureModeEnum", None)
        if ae_enum is not None:
            value = getattr(ae_enum, exposure_mode.capitalize(), None)
            if value is not None:
                out["AeExposureMode"] = value

    if settings.get("sharpness_boost") is not None:
        out["Sharpness"] = float(settings["sharpness_boost"])

    # 注意：不能用 `or` 串联 —— pybind11 枚举里值为 0 的成员会被当成假值
    nr_enum = _enum_attr(libcontrols, "draft.NoiseReductionModeEnum", "HighQuality")
    if nr_enum is None:
        nr_enum = _enum_attr(libcontrols, "NoiseReductionModeEnum", "HighQuality")
    if nr_enum is not None:
        out["NoiseReductionMode"] = nr_enum
    return out


def apply_capture_controls(picam: Any, settings: Dict[str, Any]) -> Dict[str, Any]:
    """把对焦 / 曝光 / 画质控件下发到相机。

    逐个下发：某一个控件在当前传感器或 libcamera 版本上不支持时，只记录到 skipped，
    不影响其余控件，也不中断拍照（但会在日志里留痕，绝不静默）。
    """
    set_controls = getattr(picam, "set_controls", None)
    if not callable(set_controls):
        return {"applied": {}, "skipped": ["相机对象无 set_controls（测试替身或旧版 picamera2）"]}

    libcontrols = _libcamera_controls()
    if libcontrols is None:
        return {"applied": {}, "skipped": ["libcamera.controls 不可用（未安装或版本不兼容）"]}

    applied: Dict[str, str] = {}
    skipped: List[str] = []
    for key, value in _build_controls(libcontrols, settings).items():
        try:
            set_controls({key: value})
            applied[key] = str(getattr(value, "name", value))
        except Exception as exc:  # noqa: BLE001
            skipped.append(f"{key}={value}: {exc}")
    if skipped:
        logger.warning("部分相机控件未生效（不影响拍照，但可能影响画质）：%s", "; ".join(skipped))
    return {"applied": applied, "skipped": skipped}


# ---------- 真实相机 ----------
def try_open_picamera2(
    resolution: Tuple[int, int],
    *,
    warmup_sec: float = 2.0,
    capture_settings: Optional[Dict[str, Any]] = None,
) -> Tuple[Any, Dict[str, Any]]:
    """尝试打开 picamera2，成功返回 (picam, info)；失败抛 CameraError。"""
    settings = capture_settings or dict(DEFAULT_CAPTURE_SETTINGS)
    info: Dict[str, Any] = {
        "resolution": tuple(resolution),
        "warmup_sec": warmup_sec,
        "capture_settings": settings,
    }
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
    except Exception as exc:  # noqa: BLE001
        kind, hint = classify_error(exc)
        raise CameraError(f"初始化 Picamera2 失败: {exc}", kind=kind, hint=hint) from exc

    # 相机已在出帧，此时下发对焦/曝光控件才生效
    info["capture_controls"] = apply_capture_controls(picam, settings)
    info["sensor_mode"] = _sensor_mode_of(picam) or list(resolution)
    model = (info.get("cameras") or [{}])[0].get("model") if info.get("cameras") else None
    hint = native_mode_hint(model, resolution)
    if hint:
        info["resolution_hint"] = hint
        logger.warning("分辨率提示：%s", hint)

    time.sleep(warmup_sec)
    info["cameras_ok"] = True
    return picam, info


def _sensor_mode_of(picam: Any) -> Optional[List[int]]:
    """实际选中的 sensor/输出尺寸，用于确认 ISP 是否发生了缩放。"""
    try:
        cfg = picam.camera_configuration()
        size = cfg["main"]["size"]
        return [int(size[0]), int(size[1])]
    except Exception:  # noqa: BLE001
        return None


# ---------- 画质评估（纯 PIL，不依赖 numpy / OpenCV） ----------
def assess_image(path: Path, probe_max_side: int = 1280) -> Dict[str, Any]:
    """给出成片的客观画质指标。

    sharpness = 「原图 - 1.2px 高斯模糊」残差的方差，即高频能量，越大越锐。
                该指标对失焦、运动模糊、镜头雾化都敏感。
    contrast  = 灰度标准差。镜头被油污/水雾糊住时，全局对比度会被"压平"。
    """
    from PIL import Image, ImageChops, ImageFilter, ImageStat  # 局部导入：mock 环境可能无 PIL

    path = Path(path)
    with Image.open(path) as im:
        rgb = im.convert("RGB")
        full_size = list(rgb.size)
        gray = rgb.convert("L")
        gray.thumbnail((probe_max_side, probe_max_side), Image.LANCZOS)
        stat = ImageStat.Stat(gray)
        sharpness = ImageStat.Stat(
            ImageChops.difference(gray, gray.filter(ImageFilter.GaussianBlur(1.2)))
        ).var[0]

    return {
        "path": str(path),
        "size": full_size,
        "megapixels": round(full_size[0] * full_size[1] / 1e6, 2),
        "bytes": path.stat().st_size,
        "sharpness": round(sharpness, 2),
        "brightness": round(stat.mean[0], 1),
        "contrast": round(stat.stddev[0], 1),
    }


def _af_state_name(md: Dict[str, Any]) -> Optional[str]:
    raw = md.get("AfState")
    if raw is None:
        return None
    try:
        return _AF_STATE_NAMES.get(int(raw), f"Unknown({int(raw)})")
    except Exception:  # noqa: BLE001
        text = str(raw)
        return text.rsplit(".", 1)[-1] or text


def summarize_metadata(md: Dict[str, Any]) -> Dict[str, Any]:
    """把 libcamera 元数据裁剪成人能读的几项（无关键一律丢弃）。"""
    if not md:
        return {}
    out: Dict[str, Any] = {}
    exposure = md.get("ExposureTime")
    if exposure is not None:
        out["exposure_ms"] = round(float(exposure) / 1000.0, 2)
    gain = md.get("AnalogueGain")
    if gain is not None:
        out["analogue_gain"] = round(float(gain), 2)
    lens = md.get("LensPosition")
    if lens is not None:
        out["lens_position"] = round(float(lens), 2)
    af_state = _af_state_name(md)
    if af_state:
        out["af_state"] = af_state
    lux = md.get("Lux")
    if lux is not None:
        out["lux"] = round(float(lux), 1)
    return out


def judge_quality(
    metrics: Dict[str, Any],
    metadata: Optional[Dict[str, Any]] = None,
    settings: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """综合图像指标与拍摄元数据给出画质分诊结论。

    返回 {"level": "ok"|"warn"|"bad", "findings": [...], "advice": [...]}。
    这里只做"提示"，不做拦截 —— 拍照是浇水/报告流程的一环，不能因此中断，
    但结论必须显式带出去（与项目「绝不静默失败」一致）。
    """
    settings = settings or dict(DEFAULT_CAPTURE_SETTINGS)
    qg = settings.get("quality_guard") or {}
    findings: List[str] = []
    advice: List[str] = []
    sharp = float(metrics.get("sharpness") or 0.0)
    contrast = float(metrics.get("contrast") or 0.0)
    bright = float(metrics.get("brightness") or 0.0)
    md = summarize_metadata(metadata or {})

    min_sharp = float(qg.get("min_sharpness", 8.0))
    min_contrast = float(qg.get("min_contrast", 12.0))

    if sharp < min_sharp:
        findings.append(f"清晰度 {sharp} 低于门槛 {min_sharp}（画面整体发虚）")
        if contrast < min_contrast:
            findings.append(f"同时对比度仅 {contrast}，呈现雾化特征（全局对比度被压平）")
            advice.append("高度怀疑镜头脏污/水渍/结露或被贴上保护膜：擦净或撕膜后重拍对比。")
        elif md.get("exposure_ms", 0) and md["exposure_ms"] > 33:
            advice.append(
                f"曝光 {md['exposure_ms']}ms 偏长，疑似运动模糊（风吹/支架抖动）："
                "缩短曝光上限、加固支架或补光。"
            )
        else:
            advice.append(
                "疑似未合焦：确认相机为 IMX708 且 autofocus=auto/macro，"
                "必要时用 lens_position 固定焦距；"
                "若画面一半清晰一半糊，是景深不足（见 docs/camera_image_quality.md）。"
            )
    if bright < 45:
        findings.append(f"画面偏暗（亮度 {bright}）")
        advice.append("增加补光或放宽 max_exposure_us；过暗会连带降低清晰度与可辨识度。")
    if bright > 215:
        findings.append(f"画面过曝（亮度 {bright}）")
        advice.append("避免阳光直射镜头，或下调 ExposureValue。")
    if contrast < min_contrast and sharp >= min_sharp:
        findings.append(f"对比度偏低（{contrast}）")
        advice.append("若画面像蒙了一层雾，优先检查镜头表面。")
    if md.get("analogue_gain", 0) and md["analogue_gain"] > 8:
        findings.append(f"模拟增益高达 {md['analogue_gain']}x（暗光）")
        advice.append("低光下噪点与拖影都会增加，建议在白天补光后拍摄。")
    if md.get("af_state") and md["af_state"] != "Focused":
        findings.append(f"对焦状态为 {md['af_state']}（未确认聚焦）")
        advice.append("提高光照或增加拍摄前等待时间；纹理过少时可在画面内放参照物。")

    level = "ok"
    if findings:
        level = "bad" if sharp < min_sharp * 0.6 else "warn"
    return {"level": level, "findings": findings, "advice": advice}


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
    # video 组只在 Linux 上有意义；在 PC/macOS 上跑诊断时不要据此误报
    if platform.system() == "Linux" and not info.get("user", {}).get("in_video_group"):
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


def diagnose(
    include_live_test: bool = False,
    resolution: Tuple[int, int] = (1920, 1080),
    capture_settings: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
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
            # getuid / grp 仅在 POSIX 存在；Windows 上返回 None/[]，便于本地先验证脚本
            "uid": os.getuid() if hasattr(os, "getuid") else None,
            "groups": _group_names(),
            "in_video_group": "video" in _group_names(),
        },
        "boot_config": _read_boot_config(),
    }
    out["verdict"] = _verdict(out)

    if include_live_test:
        try:
            picam, info = try_open_picamera2(
                resolution, warmup_sec=1.0, capture_settings=capture_settings
            )
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
        capture_settings: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.photo_dir = Path(photo_dir)
        self.photo_dir.mkdir(parents=True, exist_ok=True)
        self.resolution = tuple(resolution)
        self.warmup_sec = warmup_sec
        self.mock = mock
        self.allow_mock_fallback = allow_mock_fallback
        self.settings = parse_capture_settings(capture_settings)
        self._picam = None

        # 供上层展示的状态
        self.error_kind: Optional[str] = None
        self.error_hint: Optional[str] = None
        self.mock_reason: Optional[str] = None
        self.info: Dict[str, Any] = {}
        self.last_metadata: Dict[str, Any] = {}
        self.last_focus: Dict[str, Any] = {}
        self.last_quality: Dict[str, Any] = {}

        if not mock:
            try:
                self._picam, self.info = try_open_picamera2(
                    self.resolution,
                    warmup_sec=self.warmup_sec,
                    capture_settings=self.settings,
                )
                logger.info(
                    "Picamera2 已就绪 %s, cameras=%s, controls=%s",
                    self.resolution,
                    self.info.get("cameras"),
                    (self.info.get("capture_controls") or {}).get("applied"),
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
            "capture_controls": self.info.get("capture_controls"),
            "resolution_hint": self.info.get("resolution_hint"),
            "last_focus": self.last_focus or None,
            "last_metadata": summarize_metadata(self.last_metadata) or None,
            "last_quality": self.last_quality or None,
        }

    # ---------- 对焦 / 曝光辅助 ----------
    def _read_metadata(self) -> Dict[str, Any]:
        md = getattr(self._picam, "capture_metadata", None)
        if not callable(md):
            return {}
        try:
            return dict(md() or {})
        except Exception as exc:  # noqa: BLE001
            logger.debug("读取相机元数据失败: %s", exc)
            return {}

    def _wait_for_focus(self) -> Dict[str, Any]:
        """拍照前确保对焦完成 —— 这是「拍出来发虚」的首要修复点。

        IMX708（Pi Camera v3）是 VCM 自动对焦模组，libcamera 不会在 capture 瞬间
        自动帮你对一次焦，必须显式触发，否则对焦环可能停在无穷远。
        """
        mode = self.settings.get("af_mode", "auto")
        result: Dict[str, Any] = {"mode": mode, "triggered": False, "af_state": None, "note": ""}
        picam = self._picam
        if picam is None:
            return result
        if mode == "off":
            result["note"] = "按配置跳过自动对焦（仅用于排查）"
            return result

        if mode == "continuous":
            # Continuous 模式下不能调用 autofocus_cycle（Picamera2 会抛 RuntimeError），
            # 只能给算法一点时间稳定，再读一次状态。
            result["note"] = "continuous 模式：等待对焦算法自行收敛"
            time.sleep(min(float(self.settings.get("af_wait_sec", 6.0)), 2.0))
            result["af_state"] = _af_state_name(self._read_metadata())
            return result

        cycle = getattr(picam, "autofocus_cycle", None)
        if not callable(cycle):
            result["note"] = "相机不支持 autofocus_cycle（定焦模组或测试替身），跳过对焦"
            return result

        t0 = time.monotonic()
        try:
            ok = bool(cycle())
            result["triggered"] = True
            result["locked"] = ok
            if not ok:
                logger.warning(
                    "自动对焦未确认锁定（低光 / 低纹理 / 距离超出范围），仍继续拍照；"
                    "若成片发虚请参考 tools/camera_diag.py 的结论"
                )
        except Exception as exc:  # noqa: BLE001
            result["note"] = f"自动对焦执行失败：{exc}"
            logger.warning("自动对焦未能执行（%s）；按定焦情形继续拍照", exc)
        result["elapsed_sec"] = round(time.monotonic() - t0, 2)
        result["af_state"] = _af_state_name(self._read_metadata())
        return result

    def _settle(self) -> None:
        """丢弃若干帧，让 AE / AWB 从 start 后的初始值收敛到稳态。"""
        for _ in range(max(0, int(self.settings.get("settle_frames", 0)))):
            if not self._read_metadata():
                break

    def _save_still(self, path: Path) -> Dict[str, Any]:
        """保存一张 still，并尽可能把拍摄元数据带回来（用于画质归因）。"""
        quality = int(self.settings.get("jpeg_quality", 95))
        picam = self._picam

        # 首选 capture_request：可在同一帧里同时取到图与元数据，避免"图对不上参数"
        capture_request = getattr(picam, "capture_request", None)
        if callable(capture_request):
            req = capture_request()
            try:
                try:
                    req.save("main", str(path), quality=quality)
                except TypeError:
                    req.save("main", str(path))
                return dict(req.get_metadata() or {})
            finally:
                try:
                    req.release()
                except Exception:  # noqa: BLE001
                    pass

        # 回退路径（含测试替身）：capture_file
        try:
            picam.capture_file(str(path), quality=quality)
        except TypeError:
            picam.capture_file(str(path))
        return {}

    # ---------- 拍照 ----------
    def capture(self, label: str = "garden") -> Path:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        prefix = "MOCK_" if self.is_mock else ""
        path = self.photo_dir / f"{prefix}{label}_{ts}.jpg"

        if self._picam is not None:
            try:
                self.last_focus = self._wait_for_focus()
                self._settle()
                self.last_metadata = self._save_still(path)
            except Exception as exc:  # noqa: BLE001
                kind, hint = classify_error(exc)
                logger.error("拍照失败 [%s]: %s\n%s", kind, exc, traceback.format_exc())
                raise CameraError(f"拍照失败: {exc}", kind=kind, hint=hint) from exc

            md = summarize_metadata(self.last_metadata)
            logger.info("摄像头已拍照: %s  曝光=%sms 增益=%s 对焦=%s", path,
                        md.get("exposure_ms"), md.get("analogue_gain"), md.get("af_state"))
            self._run_quality_guard(path)
            return path

        return self._mock_capture(path)

    def _run_quality_guard(self, path: Path) -> None:
        """成片体检：低于阈值时显式告警（拍照已成功，不抛异常打断流程）。"""
        try:
            metrics = assess_image(path)
            verdict = judge_quality(metrics, self.last_metadata, self.settings)
            self.last_quality = {**metrics, **verdict}
            if verdict["level"] == "ok":
                return
            logger.warning(
                "⚠️ 照片画质异常 [%s] %s\n  建议：%s",
                verdict["level"], "；".join(verdict["findings"]), "；".join(verdict["advice"]),
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("画质体检失败（不影响拍照结果）: %s", exc)

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
