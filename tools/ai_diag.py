#!/usr/bin/env python3
"""AI 分析配置一键诊断（在树莓派/开发机上运行）。

用途：回答两个问题 ——
  1. 「为什么每日报告没有生成」（缺 Key / 配置错 / 连不上 / 模型不支持图片）；
  2. 「为什么报告看起来正常，但内容像是编的」（多半是占位报告或纯文本模型硬吃图片）。

用法：
    python tools/ai_diag.py           # 只读自检：查配置、Key、模型与视觉能力（不联网、不花钱）
    python tools/ai_diag.py --live    # 额外真实调用一次（会用掉少量 token）

退出码：
    0 = 配置完整且可用
    1 = 不可用（缺 Key / 配置错误 / 连接失败）
    2 = 可用但有隐患（例如模型不支持图片 → 拍照报告会直接失败）
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ai.errors import AIError  # noqa: E402
from ai.providers import PRESETS, build_provider, resolve_settings  # noqa: E402
from utils import load_config  # noqa: E402

ICON = {"not_found": "🚫", "unreachable": "⛔", "unknown": "❓"}


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def mask(name: str, value: str) -> str:
    """只暴露长度，绝不打印凭证内容。"""
    if not value:
        return f"   ❌ {name:<26} 未设置"
    return f"   ✅ {name:<26} 已设置（长度 {len(value)}）"


def make_probe_image(path: Path) -> Path:
    """生成一张小测试图（不碰相机，避免诊断工具依赖硬件）。"""
    try:
        from PIL import Image, ImageDraw

        img = Image.new("RGB", (320, 180), (34, 139, 34))
        d = ImageDraw.Draw(img)
        d.rectangle([40, 60, 120, 150], fill=(30, 100, 220))
        d.ellipse([180, 50, 270, 140], fill=(230, 200, 40))
        img.save(path, "JPEG", quality=88)
    except Exception:  # noqa: BLE001  PIL 不可用时退化为一帧最小 JPEG
        path.write_bytes(
            b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
            b"\xff\xdb\x00C\x00" + b"\x08" * 64 + b"\xff\xc0\x00\x0b\x08\x00\x01\x00"
            b"\x01\x01\x01\x11\x00\xff\xc4\x00\x14\x00\x01\x00\x00\x00\x00\x00\x00"
            b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\xff\xc4\x00\x14\x10\x01\x00"
            b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\xff"
            b"\xda\x00\x08\x01\x01\x00\x00?\x00\xd2\xcf\x20\xff\xd9"
        )
    return path


def show_config(ai_cfg: dict) -> dict:
    section("生效配置")
    s = resolve_settings(ai_cfg)
    print(f"   provider      : {s['name']}"
          f"{'' if s['known'] else '  ⚠️ 不在内置预设中（按兼容端点处理）'}")
    print(f"   api_style     : {s['api_style']}（anthropic = Messages API；openai = Chat Completions）")
    print(f"   model         : {s['model'] or '（未配置）'}")
    print(f"   base_url      : {s['base_url'] or '（未配置）'}")
    print(f"   api_key_env   : {s['api_key_env'] or '（未配置）'}")
    print(f"   supports_vision: {'是' if s['supports_vision'] else '否'}"
          f"   （图片{'可以' if s['supports_vision'] else '不会'}发给模型）")
    print(f"   timeout_sec   : {s['timeout_sec']:g}")
    print(f"   extra_body    : {ai_cfg.get('extra_body') or '（无）'}")
    preset = PRESETS.get(s["name"])
    if preset is not None and preset.note:
        print(f"   预设说明       : {preset.note}")
    return s


def show_credentials(s: dict) -> bool:
    section("凭证")
    print(mask(s["api_key_env"] or "(未配置 api_key_env)",
               os.environ.get(s["api_key_env"], "") if s["api_key_env"] else ""))
    print(f"   .env 文件      : {'存在' if (ROOT / '.env').exists() else '不存在'}")
    return bool(s["api_key"])


def live_call(ai_cfg: dict, provider) -> int:
    section("真实调用（--live）")
    with tempfile.TemporaryDirectory() as td:
        img = make_probe_image(Path(td) / "probe.jpg")
        prompt = "用一句中文描述这张图的主要颜色和形状。"
        try:
            text = provider.complete(
                image=img.read_bytes(), prompt=prompt,
                max_tokens=int(ai_cfg.get("max_tokens") or 300),
                temperature=ai_cfg.get("temperature"),
                image_detail=ai_cfg.get("image_detail") or None,
                extra_body=ai_cfg.get("extra_body") or None,
            )
        except AIError as exc:
            print(f"   ❌ 调用失败 [{exc.kind}]：{exc}")
            print(f"   💡 {exc.hint}")
            if exc.kind == "vision_unsupported":
                print("   （如上：这是发送前的刻意拦截，避免纯文本模型编造分析）")
                return 2
            return 1
    print("   ✅ 调用成功，模型回复：")
    for line in text.strip().splitlines()[:6]:
        print(f"      {line}")
    if provider.supports_vision:
        print("   ✅ 若模型确实看到了图（上面对应图中颜色/形状），说明视觉链路完好。")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="AI 分析配置自检")
    ap.add_argument("--live", action="store_true", help="真实调用一次（消耗少量 token）")
    args = ap.parse_args()

    print("=" * 62)
    print("AI 分析自检 —— 回答「为什么没有报告 / 报告为什么不可信」")
    print("=" * 62)

    try:
        cfg = load_config()
    except Exception as exc:  # noqa: BLE001
        print(f"\n❌ 读取 config.yaml 失败：{exc}")
        return 1
    ai_cfg = cfg.get("ai", {}) or {}

    s = show_config(ai_cfg)
    has_key = show_credentials(s)

    allow_mock = bool(ai_cfg.get("allow_mock_fallback", False))
    section("降级策略")
    print(f"   ai.allow_mock_fallback : {allow_mock}")
    if allow_mock:
        print("   ⚠️ 已打开 —— 缺 Key / 调用失败时会生成**占位报告**（抬头带 MOCK 横幅、无评分）")
    else:
        print("   ✅ 关闭（推荐）—— 失败时显式报错并推送告警，绝不会给出假报告")

    section("内置 provider 预设")
    for name, p in sorted(PRESETS.items()):
        print(f"   {name:<18} {p.api_style:<10} {p.default_model:<24} env={p.api_key_env}")

    if not has_key:
        section("结论")
        print(f"   ❌ AI 分析不可用：未设置环境变量 {s['api_key_env'] or '(api_key_env)'}")
        print(f"   💡 在 .env 里加上 `{s['api_key_env']}=<你的 key>` 后重跑本工具。")
        return 1

    provider = build_provider(ai_cfg, allow_mock_fallback=allow_mock)
    if not provider.available:
        section("结论")
        print(f"   ❌ AI 分析不可用 [{provider.unavailable_kind}]：{provider.unavailable_reason}")
        print(f"   💡 {provider.unavailable_hint}")
        return 1

    if args.live:
        code = live_call(ai_cfg, provider)
        if code != 0:
            return code

    section("结论")
    if not s["supports_vision"]:
        print(f"   ⚠️ 可用，但模型「{s['model']}」不具备视觉能力 —— 拍照报告会**直接失败**。")
        print("   💡 这是刻意设计：纯文本模型收到图片通常不报错，只会把图片丢掉后编造分析。")
        print("      请改用带视觉能力的模型（DeepSeek 用 deepseek-flash）。")
        return 2
    if not args.live:
        print("   ✅ 配置完整（未做真实调用，加 --live 可实测一次）")
    else:
        print("   ✅ 配置完整且真实调用成功")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
