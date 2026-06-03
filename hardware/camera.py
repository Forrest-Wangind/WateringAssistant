"""摄像头封装。

部署到 Raspberry Pi 时使用 picamera2，开发调试时回退到生成占位图。
拍摄结果统一保存到 system.photo_dir。
"""
from __future__ import annotations

import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Tuple

logger = logging.getLogger(__name__)


class Camera:
    def __init__(
        self,
        photo_dir: str,
        *,
        resolution: Tuple[int, int] = (1920, 1080),
        warmup_sec: float = 2.0,
        mock: bool = False,
    ) -> None:
        self.photo_dir = Path(photo_dir)
        self.photo_dir.mkdir(parents=True, exist_ok=True)
        self.resolution = tuple(resolution)
        self.warmup_sec = warmup_sec
        self.mock = mock
        self._picam = None

        if not mock:
            try:
                from picamera2 import Picamera2  # type: ignore

                self._picam = Picamera2()
                cfg = self._picam.create_still_configuration(
                    main={"size": self.resolution}
                )
                self._picam.configure(cfg)
                self._picam.start()
                time.sleep(self.warmup_sec)
                logger.info("Picamera2 已就绪 %s", self.resolution)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Picamera2 不可用，回退到 mock 模式: %s", exc)
                self._picam = None
                self.mock = True

    def capture(self, label: str = "garden") -> Path:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = self.photo_dir / f"{label}_{ts}.jpg"
        if self._picam is not None:
            self._picam.capture_file(str(path))
            logger.info("摄像头已拍照: %s", path)
            return path
        return self._mock_capture(path)

    def _mock_capture(self, path: Path) -> Path:
        from PIL import Image, ImageDraw, ImageFont

        img = Image.new("RGB", self.resolution, color=(34, 89, 50))
        draw = ImageDraw.Draw(img)
        text = f"MOCK GARDEN PHOTO\n{datetime.now().isoformat(timespec='seconds')}"
        try:
            font = ImageFont.load_default()
        except Exception:  # noqa: BLE001
            font = None
        draw.text((40, 40), text, fill=(255, 255, 255), font=font)
        img.save(path, format="JPEG", quality=85)
        logger.info("[MOCK] 已生成占位照片: %s", path)
        return path

    def close(self) -> None:
        if self._picam is not None:
            try:
                self._picam.close()
            except Exception:  # noqa: BLE001
                pass
            self._picam = None
