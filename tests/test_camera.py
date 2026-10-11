"""摄像头模块单元测试。"""
from pathlib import Path
from typing import Dict

import pytest
from PIL import Image

import _bootstrap  # noqa: F401  必须在导入项目模块之前执行：把仓库根目录加入 sys.path
from hardware import camera as camera_mod
from hardware.camera import Camera, CameraError, classify_error


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


if __name__ == "__main__":
    _bootstrap.run(__file__)
