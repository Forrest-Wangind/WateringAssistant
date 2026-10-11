"""手动测试脚本：拍照 + AI 分析。

用法（在项目根目录执行）：
    python tests/photoAndAnalyse.py            # 拍照 + 分析
    python tests/photoAndAnalyse.py --photo    # 仅拍照

说明：
  * 拍照使用真实摄像头（配置 mock_hardware=false 时）。
  * 分析需要 ANTHROPIC_API_KEY（见 config.yaml 的 ai.api_key_env）；
    未设置时返回占位报告，不会真正分析照片。
"""
from __future__ import annotations

import sys
from pathlib import Path

# 允许直接以 `python tests/photoAndAnalyse.py` 运行（把项目根目录加入 sys.path）
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ai.plant_analyzer import PlantAnalyzer  # noqa: E402
from hardware.camera import Camera  # noqa: E402
from utils import load_config  # noqa: E402


def _camera() -> Camera:
    cfg = load_config()
    s = cfg["system"]
    return Camera(
        s["photo_dir"],
        resolution=tuple(cfg["camera"]["resolution"]),
        warmup_sec=float(cfg["camera"]["warmup_sec"]),
        mock=bool(s.get("mock_hardware", False)),
    )


def take_photo_only() -> None:
    cam = _camera()
    photo = cam.capture("garden")
    cam.close()
    print("PHOTO:", photo)


def take_photo_and_analyse() -> None:
    cfg = load_config()
    cam = _camera()
    photo = cam.capture("garden")
    cam.close()

    report = PlantAnalyzer(cfg["ai"], cfg["system"]["report_dir"]).analyze(photo)
    print("PHOTO :", photo)
    print("REPORT:", report.report_path)
    print("SCORE :", report.score)
    print("------")
    print(report.markdown)


if __name__ == "__main__":
    if "--photo" in sys.argv or "--photo-only" in sys.argv:
        take_photo_only()
    else:
        take_photo_and_analyse()
