"""摄像头模块单元测试。"""
from pathlib import Path
from typing import Dict

import pytest
from PIL import Image

import _bootstrap  # noqa: F401  必须在导入项目模块之前执行：把仓库根目录加入 sys.path
from hardware import camera as camera_mod
from hardware.camera import (
    Camera,
    CameraError,
    apply_capture_controls,
    assess_image,
    classify_error,
    judge_quality,
    native_mode_hint,
    parse_capture_settings,
)


def test_camera_mock_capture(temp_dirs: Dict[str, Path]):
    """mock 模式生成占位图。"""
    cam = Camera(str(temp_dirs["photo_dir"]), mock=True, warmup_sec=0)
    photo = cam.capture("test")
    assert photo.exists()
    assert photo.suffix == ".jpg"
    assert "test_" in photo.name
    # 校验是有效 JPEG
    img = Image.open(photo)
    assert img.size == (1920, 1080)
    assert img.format == "JPEG"
    cam.close()


def test_camera_custom_resolution(temp_dirs: Dict[str, Path]):
    """自定义分辨率。"""
    cam = Camera(str(temp_dirs["photo_dir"]), resolution=(640, 480), mock=True, warmup_sec=0)
    photo = cam.capture()
    img = Image.open(photo)
    assert img.size == (640, 480)
    cam.close()


def test_camera_multiple_captures(temp_dirs: Dict[str, Path]):
    """连续拍照不冲突。"""
    cam = Camera(str(temp_dirs["photo_dir"]), mock=True, warmup_sec=0)
    p1 = cam.capture("a")
    p2 = cam.capture("b")
    assert p1 != p2
    assert p1.exists() and p2.exists()
    cam.close()


# ---------- 真实相机失败时的行为（绝不静默降级） ----------
@pytest.mark.parametrize(
    "exc,expected_kind",
    [
        (ModuleNotFoundError("No module named 'picamera2'"), "not_found"),
        (RuntimeError("No cameras available"), "not_found"),
        (RuntimeError("Camera is in use by another process"), "unreachable"),
        (PermissionError("Permission denied"), "unreachable"),
        (ValueError("weird"), "unknown"),
    ],
)
def test_classify_error(exc: BaseException, expected_kind: str):
    kind, hint = classify_error(exc)
    assert kind == expected_kind
    assert hint  # 必须带修复建议


def test_camera_strict_raises_when_unavailable(temp_dirs: Dict[str, Path], monkeypatch):
    """mock=False 且不允许回退时，相机不可用必须抛 CameraError，而不是生成占位图。"""

    def _boom(*_a, **_kw):
        raise CameraError("导入 picamera2 失败", kind="not_found", hint="修复建议")

    monkeypatch.setattr(camera_mod, "try_open_picamera2", _boom)
    with pytest.raises(CameraError) as ei:
        Camera(str(temp_dirs["photo_dir"]), mock=False, allow_mock_fallback=False, warmup_sec=0)
    assert ei.value.kind == "not_found"
    assert ei.value.hint
    # 目录里不应残留任何照片
    assert list(Path(temp_dirs["photo_dir"]).glob("*.jpg")) == []


def test_camera_explicit_fallback_marks_mock(temp_dirs: Dict[str, Path], monkeypatch):
    """显式允许回退时：进入 mock 模式、文件名带 MOCK_ 前缀、状态可见原因。"""

    def _boom(*_a, **_kw):
        raise CameraError("无相机", kind="not_found", hint="修复建议")

    monkeypatch.setattr(camera_mod, "try_open_picamera2", _boom)
    cam = Camera(str(temp_dirs["photo_dir"]), mock=False, allow_mock_fallback=True, warmup_sec=0)
    assert cam.is_mock is True
    status = cam.status()
    assert status["mode"] == "mock"
    assert status["error_kind"] == "not_found"
    assert status["mock_reason"]

    photo = cam.capture("garden")
    assert photo.name.startswith("MOCK_")
    cam.close()


def test_camera_status_real_mode(temp_dirs: Dict[str, Path], monkeypatch):
    """打开成功时状态为 picamera2。"""

    class _FakePicam:
        def capture_file(self, path: str) -> None:
            Path(path).write_bytes(b"jpeg")

        def close(self) -> None:
            pass

    monkeypatch.setattr(
        camera_mod, "try_open_picamera2", lambda *_a, **_kw: (_FakePicam(), {"cameras": []})
    )
    cam = Camera(str(temp_dirs["photo_dir"]), mock=False, warmup_sec=0)
    assert cam.is_mock is False
    assert cam.status()["mode"] == "picamera2"
    photo = cam.capture("garden")
    assert not photo.name.startswith("MOCK_")
    cam.close()


def test_camera_capture_failure_raises(temp_dirs: Dict[str, Path], monkeypatch):
    """真实相机拍照失败要报错，不得退化成占位图。"""

    class _BadPicam:
        def capture_file(self, path: str) -> None:
            raise RuntimeError("Camera frontend has timed out")

        def close(self) -> None:
            pass

    monkeypatch.setattr(
        camera_mod, "try_open_picamera2", lambda *_a, **_kw: (_BadPicam(), {})
    )
    cam = Camera(str(temp_dirs["photo_dir"]), mock=False, warmup_sec=0)
    with pytest.raises(CameraError) as ei:
        cam.capture("garden")
    assert ei.value.kind == "unreachable"
    cam.close()


# ---------- 画质专项（「拍照很模糊」的回归防线） ----------
def test_parse_capture_settings_defaults_and_override():
    """config 段缺省项要用默认值补齐，避免调用方漏配就崩。"""
    s = parse_capture_settings(None)
    assert s["af_mode"] == "auto"          # 静止花架：单次对焦，而非 continuous
    assert s["jpeg_quality"] == 95
    assert s["max_exposure_us"] > 0
    assert s["quality_guard"]["enabled"] is True

    s2 = parse_capture_settings(
        {"autofocus": "continuous", "jpeg_quality": 80, "quality_guard": {"min_sharpness": 20}}
    )
    assert s2["af_mode"] == "continuous"
    assert s2["jpeg_quality"] == 80
    assert s2["quality_guard"]["min_sharpness"] == 20
    assert s2["quality_guard"]["min_contrast"] == s["quality_guard"]["min_contrast"]  # 未覆盖项保留默认


def test_native_mode_hint_flags_non_native_resolution():
    """1920x1080 不是 IMX708 的原生模式，必须给出提示。"""
    assert native_mode_hint("imx708", (2304, 1296)) is None
    hint = native_mode_hint("imx708", (1920, 1080))
    assert hint and "2304x1296" in hint
    # 未知型号 / 无型号时不乱报
    assert native_mode_hint(None, (1920, 1080)) is None
    assert native_mode_hint("ov5647", (1920, 1080)) is None


def _scene(path: Path, blur: float = 0.0) -> Path:
    """生成一张有高频细节的测试图（竖条纹），可选择性模糊。"""
    from PIL import ImageDraw, ImageFilter

    img = Image.new("RGB", (640, 360), (30, 90, 50))
    draw = ImageDraw.Draw(img)
    for x in range(0, 640, 16):
        draw.line([(x, 0), (x, 360)], fill=(220, 220, 220))
    if blur:
        img = img.filter(ImageFilter.GaussianBlur(blur))
    img.save(path, quality=95)
    return path


def test_assess_image_sharpness_drops_when_blurred(tmp_path: Path):
    """清晰度指标必须能区分清楚与模糊（否则门禁形同虚设）。"""
    sharp = assess_image(_scene(tmp_path / "sharp.jpg", 0.0))
    blur = assess_image(_scene(tmp_path / "blur.jpg", 3.0))
    assert sharp["sharpness"] > blur["sharpness"]
    assert sharp["size"] == [640, 360]
    assert sharp["bytes"] > 0


def test_judge_quality_detects_blur_low_contrast():
    """整体发虚 + 对比度被压平 → 指向镜头脏污/雾化。"""
    verdict = judge_quality(
        {"sharpness": 2.0, "contrast": 5.0, "brightness": 120.0},
        {},
        parse_capture_settings(None),
    )
    assert verdict["level"] == "bad"
    assert any("清晰度" in f for f in verdict["findings"])
    assert any("镜头" in a for a in verdict["advice"])


def test_judge_quality_flags_long_exposure_as_motion_blur():
    """曝光过长 → 判定为运动模糊（风吹/支架抖动），而不是对焦问题。"""
    verdict = judge_quality(
        {"sharpness": 5.0, "contrast": 40.0, "brightness": 120.0},
        {"ExposureTime": 60_000, "AnalogueGain": 12.0},
        parse_capture_settings(None),
    )
    joined = " ".join(verdict["findings"] + verdict["advice"])
    assert "曝光" in joined and "运动模糊" in joined


def test_judge_quality_warns_when_af_not_locked():
    verdict = judge_quality(
        {"sharpness": 60.0, "contrast": 30.0, "brightness": 120.0},
        {"AfState": 3, "ExposureTime": 8_000},
        parse_capture_settings(None),
    )
    assert verdict["level"] == "warn"
    assert any("对焦" in f for f in verdict["findings"])


def test_judge_quality_ok_for_good_photo():
    verdict = judge_quality(
        {"sharpness": 90.0, "contrast": 45.0, "brightness": 130.0},
        {"AfState": 2},
        parse_capture_settings(None),
    )
    assert verdict["level"] == "ok"
    assert verdict["findings"] == []


def test_apply_capture_controls_degrades_gracefully():
    """没有 set_controls / 没有 libcamera 时只记录 skipped，绝不能抛异常。"""
    out = apply_capture_controls(object(), parse_capture_settings(None))
    assert out["applied"] == {}
    assert out["skipped"]


class _AFPicam:
    """带自动对焦能力的替身，记录调用次数与控件。"""

    def __init__(self) -> None:
        self.af_calls = 0
        self.controls: list = []
        self.capture_kwargs: list = []

    def autofocus_cycle(self) -> bool:
        self.af_calls += 1
        return True

    def set_controls(self, controls) -> None:
        self.controls.append(controls)

    def capture_file(self, path: str, **kwargs) -> None:
        self.capture_kwargs.append(kwargs)
        Image.new("RGB", (800, 450), (200, 200, 200)).save(path, "JPEG")

    def close(self) -> None:
        pass


def test_camera_capture_triggers_autofocus(tmp_path: Path, monkeypatch):
    """IMX708 必须在拍照前显式对焦，否则等于盲拍（本次模糊问题的根因）。"""
    fake = _AFPicam()
    monkeypatch.setattr(camera_mod, "try_open_picamera2", lambda *_a, **_kw: (fake, {"cameras": []}))
    cam = Camera(str(tmp_path / "photos"), mock=False, warmup_sec=0)

    photo = cam.capture("garden")
    assert photo.exists()
    assert fake.af_calls == 1                     # 拍照前跑了一次对焦循环
    assert cam.last_focus["triggered"] is True
    assert cam.last_focus["locked"] is True
    assert fake.capture_kwargs and fake.capture_kwargs[0].get("quality") == 95
    cam.close()


def test_camera_capture_survives_autofocus_failure(tmp_path: Path, monkeypatch):
    """对焦失败也必须把照片拍下来 —— 拍照是浇水/报告流程的一环，不能被对焦拖死。"""

    class _NoAFPicam:
        def autofocus_cycle(self):
            raise RuntimeError("Autofocus not supported on this sensor")

        def capture_file(self, path: str, **kwargs) -> None:
            Image.new("RGB", (320, 240), (10, 10, 10)).save(path, "JPEG")

        def close(self) -> None:
            pass

    monkeypatch.setattr(camera_mod, "try_open_picamera2", lambda *_a, **_kw: (_NoAFPicam(), {}))
    cam = Camera(str(tmp_path / "photos"), mock=False, warmup_sec=0)
    photo = cam.capture("garden")
    assert photo.exists()
    assert cam.last_focus["triggered"] is False
    assert "失败" in cam.last_focus["note"]       # 失败要被记录，不能静默
    cam.close()


def test_camera_flags_blurry_photo(tmp_path: Path, monkeypatch):
    """成片没有高频细节时，必须在状态里留下画质告警（绝不静默地把糊图送给 AI）。"""

    class _FlatPicam:
        def capture_file(self, path: str, **kwargs) -> None:
            # 纯色图 = 零高频细节，等价于完全失焦 / 镜头被糊住
            Image.new("RGB", (800, 450), (34, 89, 50)).save(path, "JPEG")

        def close(self) -> None:
            pass

    monkeypatch.setattr(camera_mod, "try_open_picamera2", lambda *_a, **_kw: (_FlatPicam(), {}))
    cam = Camera(str(tmp_path / "photos"), mock=False, warmup_sec=0)
    cam.capture("garden")
    assert cam.last_quality["level"] == "bad"
    assert cam.last_quality["findings"]
    assert cam.status()["last_quality"]["level"] == "bad"
    cam.close()


if __name__ == "__main__":
    _bootstrap.run(__file__)