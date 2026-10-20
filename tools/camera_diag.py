#!/usr/bin/env python3
"""相机一键诊断（在树莓派上运行）。

用途：
  1. 回答「为什么拍照结果是 MOCK 占位图」—— 环境层检查；
  2. 回答「为什么照片很模糊」—— 对真实成片做清晰度体检，并给出分诊结论。

用法：
    python tools/camera_diag.py                 # 只做环境检查，不动相机
    python tools/camera_diag.py --live          # 真实打开 + 拍照 + 画质体检（会短暂占用相机）
    python tools/camera_diag.py --live --baseline   # 额外把本次指标记为基线（供日后对比）

退出码：
    0 = 相机可用且画质正常
    1 = 相机不可用（已给出分类原因）
    2 = 相机可用，但成片画质异常（模糊 / 欠曝 / 疑似镜头脏）—— 拍照流程可用，但结果不可信
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

# 允许直接从仓库根目录运行
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hardware.camera import (  # noqa: E402
    Camera,
    CameraError,
    assess_image,
    diagnose,
    judge_quality,
    summarize_metadata,
)
from utils import load_config  # noqa: E402

ICON = {"not_found": "🚫", "unreachable": "⛔", "unknown": "❓"}
LEVEL_ICON = {"ok": "✅", "warn": "⚠️", "bad": "❌"}

# 现场人工检查项：软件查不出来，但「很模糊」十次里有三次是这些原因
MANUAL_CHECKS = (
    "① 镜头保护膜：新模组镜头上有一层透明贴膜，没撕掉会整幅雾化发虚（最常见的低级错误）。",
    "② 镜头表面：本项目会喷水，水渍/水雾/灰尘/结露会让画面变糊并出现亮斑 —— 用镜头布擦净。",
    "③ 对焦距离：IMX708 标准版合焦范围约 10cm~∞。若相机与主要植物距离超出此范围，"
    "请改用 autofocus=auto，或把 lens_position 设到对应屈光度（0=无穷远，1=1m，10=10cm）。",
    "④ 景深：相机在 L3 顶端俯角 30°，同一画面里近处约 0.3m、远处约 2m，"
    "f/1.8 景深不足以让两者同时清晰。若照片是「一半清楚一半糊」，这是光学限制而非故障 —— "
    "见 docs/camera_image_quality.md。",
    "⑤ 供电与震动：AF 音圈马达瞬时电流可达 300mA，与 4 路水泵/继电器共用电源时可能被拉低；"
    "水泵工作时铝合金支架的震动也会在慢速快门下被放大成拖影。",
)


def _section(title: str) -> None:
    print("-" * 62)
    print(title)


def _load_baseline(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def main() -> int:
    live = "--live" in sys.argv
    set_baseline = "--baseline" in sys.argv

    try:
        cfg = load_config()
    except Exception as exc:  # noqa: BLE001
        print(f"❌ 读取 config.yaml 失败：{exc}")
        return 1
    sys_cfg = cfg.get("system", {})
    cam_cfg = cfg.get("camera", {})
    resolution = tuple(cam_cfg.get("resolution", (1920, 1080)))

    print("=" * 62)
    print("相机诊断")
    print("=" * 62)
    info = diagnose(include_live_test=False, resolution=resolution, capture_settings=cam_cfg)
    print(json.dumps(info, ensure_ascii=False, indent=2))

    verdict = info["verdict"]
    _section(f"{ICON.get(verdict['kind'], '?')} 环境结论 [{verdict['kind']}]: {verdict['reason']}")
    if verdict["hint"]:
        print(f"💡 {verdict['hint']}")

    # 直接复用 Camera 的真实路径，复现与主程序完全一致的判定与拍摄流程
    _section("尝试以 mock=False + allow_mock_fallback=False 打开相机（与主程序一致）…")
    data_dir = Path(sys_cfg.get("data_dir", "./data"))
    diag_dir = data_dir / "diag"
    try:
        cam = Camera(
            str(diag_dir),
            resolution=resolution,
            warmup_sec=float(cam_cfg.get("warmup_sec", 2.0)),
            mock=False,
            allow_mock_fallback=False,
            capture_settings=cam_cfg,
        )
    except CameraError as exc:
        print(f"❌ 失败 [{exc.kind}]: {exc}")
        print(f"💡 {exc.hint}")
        return 1

    print(f"✅ 已完成初始化: {json.dumps(cam.status(), ensure_ascii=False)}")
    model = (cam.info.get("cameras") or [{}])[0].get("model") if cam.info.get("cameras") else None
    print(f"   传感器实际输出尺寸: {cam.info.get('sensor_mode')}（请求 {list(resolution)}）")
    applied = (cam.info.get("capture_controls") or {}).get("applied") or {}
    skipped = (cam.info.get("capture_controls") or {}).get("skipped") or []
    print(f"   已生效控件: {len(applied)} 项 {json.dumps(applied, ensure_ascii=False)}")
    if skipped:
        print(f"   ⚠️ 未生效控件（可能导致对焦/曝光不受控）: {json.dumps(skipped, ensure_ascii=False)}")
    if cam.info.get("resolution_hint"):
        print(f"   💡 {cam.info['resolution_hint']}")

    code = 0
    if live:
        _section("拍照 + 画质体检…")
        try:
            photo = cam.capture("diag")
        except CameraError as exc:
            print(f"❌ 拍照失败 [{exc.kind}]: {exc}")
            print(f"💡 {exc.hint}")
            cam.close()
            return 1

        metrics = assess_image(photo)
        qv = judge_quality(metrics, cam.last_metadata, cam.settings)
        md = summarize_metadata(cam.last_metadata)
        img_ok = cam.status().get("last_quality") or {}

        print(f"📷 照片: {photo}  ({metrics['bytes']} bytes)")
        print(f"   尺寸: {metrics['size'][0]}x{metrics['size'][1]}  {metrics['megapixels']}MP")
        print(f"   清晰度: {metrics['sharpness']}   对比度: {metrics['contrast']}   "
              f"亮度: {metrics['brightness']}   门槛: {cam.settings['quality_guard']}")
        print(f"   对焦: {json.dumps(cam.last_focus, ensure_ascii=False)}")
        print(f"   元数据: {json.dumps(md, ensure_ascii=False)}")
        if metrics["bytes"] < 20_000:
            print("   ⚠️ 文件过小（<20KB），大概率不是真实画面")

        # 基线对比：绝对值与场景强相关，跨时间比较才有意义
        baseline_path = data_dir / "camera_baseline.json"
        baseline = _load_baseline(baseline_path)
        if baseline.get("sharpness") and not set_baseline:
            delta = metrics["sharpness"] - baseline["sharpness"]
            pct = delta / baseline["sharpness"] * 100 if baseline["sharpness"] else 0
            print(f"   基线对比: 清晰度 {metrics['sharpness']} vs 基线 {baseline['sharpness']} "
                  f"({delta:+.1f} / {pct:+.0f}%)  基线时间 {baseline.get('ts')}")

        _section(f"{LEVEL_ICON.get(qv['level'], '?')} 画质结论 [{qv['level']}]")
        for line in qv["findings"] or ["未见异常"]:
            print(f"   • {line}")
        for line in qv["advice"]:
            print(f"   💡 {line}")
        if qv["level"] != "ok":
            code = 2

        if set_baseline:
            baseline_path.parent.mkdir(parents=True, exist_ok=True)
            baseline_path.write_text(
                json.dumps(
                    {**metrics, "metadata": md, "model": model,
                     "ts": datetime.now().isoformat(timespec="seconds")},
                    ensure_ascii=False, indent=2,
                ),
                encoding="utf-8",
            )
            print(f"   已更新基线: {baseline_path}")

    cam.close()

    if code != 0 or not live:
        _section("若照片仍模糊，请按顺序排除下列人工可查项")
        for line in MANUAL_CHECKS:
            print(f"   {line}")

    print("=" * 62)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
