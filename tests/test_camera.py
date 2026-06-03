"""摄像头模块单元测试。"""
from pathlib import Path
from typing import Dict

from PIL import Image

from hardware.camera import Camera


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
