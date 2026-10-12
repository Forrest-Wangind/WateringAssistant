"""手动测试脚本：拍照 + AI 分析。

用法（在项目根目录执行）：
    python tests/photoAndAnalyse.py            # 拍照 + AI 分析
    python tests/photoAndAnalyse.py --photo    # 仅拍照（推荐先跑这个看画质）
    python tests/photoAndAnalyse.py --verbose  # 打印画质指标与拍摄元数据

说明：
  * 拍照使用真实摄像头（配置 mock_hardware=false 时）。
  * 拍照会先触发一次自动对焦（config.yaml 的 camera.autofocus），再限制曝光上限，
    最后对成片做清晰度体检 —— 结果会打印在下方，并在异常时给出处置建议。
  * 分析走 config.yaml 的 ai 段（provider 可为 deepseek / anthropic / openai 等），
    API Key 由 ai.api_key_env 指定。**缺 Key 或调用失败会显式报错并返回退出码 1**，
    不会悄悄给出一份占位报告（除非显式打开 ai.allow_mock_fallback）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# 允许直接以 `python tests/photoAndAnalyse.py` 运行（把项目根目录加入 sys.path）
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ai.errors import AIError  # noqa: E402
from ai.plant_analyzer import PlantAnalyzer  # noqa: E402
from hardware.camera import Camera, assess_image, judge_quality, summarize_metadata  # noqa: E402
from utils import load_config  # noqa: E402


def _camera() -> Camera:
    cfg = load_config()
    s = cfg["system"]
    cam_cfg = cfg.get("camera", {})
    return Camera(
        s["photo_dir"],
        resolution=tuple(cam_cfg["resolution"]),
        warmup_sec=float(cam_cfg["warmup_sec"]),
        mock=bool(s.get("mock_hardware", False)),
        # 默认不允许静默降级：相机不可用就直接报错
        allow_mock_fallback=bool(cam_cfg.get("allow_mock_fallback", False)),
        capture_settings=cam_cfg,
    )


def _report_quality(cam: Camera, photo: Path, verbose: bool) -> None:
    """打印本次成片的客观画质结论 —— 回答「为什么看起来糊」。"""
    if cam.is_mock:
        print("⚠️ 当前是 MOCK 占位图，画质结论无意义（先跑 tools/camera_diag.py 修相机）")
        return
    metrics = assess_image(photo)
    verdict = judge_quality(metrics, cam.last_metadata, cam.settings)
    md = summarize_metadata(cam.last_metadata)

    print("-" * 62)
    print(f"画质指标 : 清晰度 {metrics['sharpness']}（门槛 {cam.settings['quality_guard']['min_sharpness']}）"
          f" / 对比度 {metrics['contrast']} / 亮度 {metrics['brightness']}"
          f" / {metrics['size'][0]}x{metrics['size'][1]} {metrics['megapixels']}MP"
          f" {metrics['bytes'] // 1024}KB")
    print(f"对焦过程 : {json.dumps(cam.last_focus, ensure_ascii=False)}")
    print(f"拍摄元数据: {json.dumps(md, ensure_ascii=False)}")
    print(f"画质结论 : {verdict['level'].upper()}  " + ("；".join(verdict["findings"]) or "未见异常"))
    for line in verdict["advice"]:
        print(f"  💡 {line}")
    if cam.info.get("resolution_hint"):
        print(f"  💡 {cam.info['resolution_hint']}")
    if verbose:
        print("-" * 62)
        print("完整状态:", json.dumps(cam.status(), ensure_ascii=False, indent=2))


def take_photo_only(verbose: bool = False) -> int:
    cam = _camera()
    photo = cam.capture("garden")
    print("PHOTO:", photo)
    _report_quality(cam, photo, verbose)
    cam.close()
    return 0


def take_photo_and_analyse(verbose: bool = False) -> int:
    cfg = load_config()
    cam = _camera()
    photo = cam.capture("garden")
    _report_quality(cam, photo, verbose)
    cam.close()

    ai_cfg = cfg.get("ai", {})
    analyzer = PlantAnalyzer(
        ai_cfg,
        cfg["system"]["report_dir"],
        allow_mock_fallback=bool(ai_cfg.get("allow_mock_fallback", False)),
    )
    info = analyzer.describe()
    print("-" * 62)
    print(f"AI 配置  : provider={info['provider']} / model={info['model']}"
          f" / 视觉={'是' if info['supports_vision'] else '否'}")

    try:
        report = analyzer.analyze(photo)
    except AIError as exc:
        print(f"❌ AI 分析失败 [{exc.kind}]：{exc}")
        print(f"💡 {exc.hint}")
        print("   （这是显式失败，不是占位报告；修复后重跑）")
        return 1

    print("PHOTO :", photo)
    print("REPORT:", report.report_path)
    print("SCORE :", report.score)
    if report.degraded:
        print("⚠️ 本次为 MOCK 占位报告，内容不可信 ——", report.degraded_reason)
    print("------")
    print(report.markdown)
    return 0


if __name__ == "__main__":
    verbose = "--verbose" in sys.argv or "-v" in sys.argv
    only_photo = "--photo" in sys.argv or "--photo-only" in sys.argv
    raise SystemExit(take_photo_only(verbose) if only_photo else take_photo_and_analyse(verbose))
