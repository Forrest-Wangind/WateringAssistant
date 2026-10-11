#!/usr/bin/env python3
"""相机一键诊断（在树莓派上运行）。

用途：回答「为什么拍照结果是 MOCK 占位图」。

用法：
    python tools/camera_diag.py            # 只做环境检查，不动相机
    python tools/camera_diag.py --live     # 额外做一次真实打开 + 拍照（会短暂占用相机）

退出码：0 = 可用，1 = 不可用（并给出分类原因）。
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

# 允许直接从仓库根目录运行
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hardware.camera import Camera, CameraError, diagnose  # noqa: E402

ICON = {"not_found": "🚫", "unreachable": "⛔", "unknown": "❓"}


def main() -> int:
    live = "--live" in sys.argv
    info = diagnose(include_live_test=False)

    print("=" * 62)
    print("相机诊断")
    print("=" * 62)
    print(json.dumps(info, ensure_ascii=False, indent=2))

    verdict = info["verdict"]
    print("-" * 62)
    print(f"{ICON.get(verdict['kind'], '?')} 环境结论 [{verdict['kind']}]: {verdict['reason']}")
    if verdict["hint"]:
        print(f"💡 {verdict['hint']}")

    # 直接复用 Camera 的真实路径，复现与主程序完全一致的判定
    print("-" * 62)
    print("尝试以 mock=False + allow_mock_fallback=False 打开相机（与主程序一致）…")
    with tempfile.TemporaryDirectory() as td:
        try:
            cam = Camera(td, mock=False, warmup_sec=2.0, allow_mock_fallback=False)
        except CameraError as exc:
            print(f"❌ 失败 [{exc.kind}]: {exc}")
            print(f"💡 {exc.hint}")
            return 1
        status = cam.status()
        print(f"✅ 已完成初始化: {json.dumps(status, ensure_ascii=False)}")
        if live:
            photo = cam.capture("diag")
            size = photo.stat().st_size
            print(f"📷 已拍照: {photo} ({size} bytes)")
            if size < 20_000:
                print("⚠️ 图片偏小，可能不是真实画面（真实 1080p 照片通常 > 100KB）")
        cam.close()

    if live and "live_test" in info:
        print(f"live_test: {json.dumps(info['live_test'], ensure_ascii=False)}")
    print("=" * 62)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
