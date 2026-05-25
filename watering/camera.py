"""摄像头采集：优先 picamera2（CSI 摄像头），失败时回落到 OpenCV V4L2（USB 摄像头）。"""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class CameraError(RuntimeError):
    pass


class Camera:
    def __init__(self, photo_dir: Path, width: int = 1280, height: int = 720):
        self._photo_dir = photo_dir
        self._width = width
        self._height = height
        self._impl = self._init_backend()

    def _init_backend(self) -> str:
        try:
            from picamera2 import Picamera2  # type: ignore
            self._picam = Picamera2()
            cfg = self._picam.create_still_configuration(
                main={"size": (self._width, self._height)}
            )
            self._picam.configure(cfg)
            self._picam.start()
            logger.info("使用 picamera2 后端")
            return "picamera2"
        except Exception as e:
            logger.info("picamera2 不可用（%s），尝试 OpenCV V4L2", e)

        try:
            import cv2  # type: ignore
            self._cv2 = cv2
            self._cap = cv2.VideoCapture(0, cv2.CAP_V4L2)
            if not self._cap.isOpened():
                self._cap = cv2.VideoCapture(0)
            if not self._cap.isOpened():
                raise CameraError("无法打开摄像头设备 /dev/video0")
            self._cap.set(cv2.CAP_PROP_FRAME_WIDTH, self._width)
            self._cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self._height)
            logger.info("使用 OpenCV 后端")
            return "opencv"
        except CameraError:
            raise
        except Exception as e:
            raise CameraError(f"摄像头初始化失败: {e}")

    def capture(self) -> Path:
        now = datetime.now()
        day_dir = self._photo_dir / now.strftime("%Y-%m-%d")
        day_dir.mkdir(parents=True, exist_ok=True)
        save_path = day_dir / f"{now.strftime('%H%M%S')}.jpg"

        if self._impl == "picamera2":
            self._picam.capture_file(str(save_path))
        else:
            # OpenCV：丢弃前几帧避免曝光未稳
            for _ in range(3):
                self._cap.read()
            ok, frame = self._cap.read()
            if not ok:
                raise CameraError("OpenCV 读取帧失败")
            self._cv2.imwrite(str(save_path), frame)

        logger.info("拍照完成: %s", save_path)
        return save_path

    def close(self) -> None:
        try:
            if self._impl == "picamera2":
                self._picam.stop()
            else:
                self._cap.release()
        except Exception:
            logger.exception("摄像头关闭异常")
