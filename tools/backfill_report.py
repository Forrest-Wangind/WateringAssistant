"""对历史花园照片补跑 AI 生长分析（正式报告缺失时的补救工具）。

场景：树莓派上拍照成功但 AI 分析失败（照片已落盘、报告未生成）时，
把照片拉到任意装有本项目代码与 API Key 的机器上，用它补生成报告。

用法::

    python tools/backfill_report.py data/photos/garden_20261015_130000.jpg
    python tools/backfill_report.py <photo> [--report-dir DIR] [--suffix _backfill]

规则：
- 报告文件名沿用系统规范 ``report_<拍摄时间戳>.md``（从照片文件名解析，
  解析不出则退回文件 mtime），已有同名报告时**拒绝覆盖**。
- 报告头部写明 BACKFILL 事实：照片拍摄时间、补跑时间，绝不冒充当日实时结论。
- **绝不静默失败**：AI 调用失败直接抛 :class:`ai.errors.AIError` 并给出修复建议，
  绝不生成占位报告（不受 ``ai.allow_mock_fallback`` 影响）。
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ai.errors import AIError  # noqa: E402
from ai.plant_analyzer import DEFAULT_PROMPT, DEFAULT_SYSTEM, PlantAnalyzer  # noqa: E402
from ai.providers import build_provider  # noqa: E402
from utils import load_config  # noqa: E402

_STAMP_RE = re.compile(r"garden_(\d{8})_(\d{6})")
_MEDIA_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


def photo_taken_at(photo: Path) -> datetime:
    """从 ``garden_YYYYMMDD_HHMMSS.jpg`` 文件名解析拍摄时间，失败退回 mtime。"""
    m = _STAMP_RE.search(photo.name)
    if m:
        return datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
    return datetime.fromtimestamp(photo.stat().st_mtime)


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description="对历史花园照片补跑 AI 生长分析")
    ap.add_argument("photo", type=Path, help="照片路径")
    ap.add_argument("--report-dir", default=None,
                    help="报告输出目录（默认取 config.yaml 的 system.report_dir）")
    ap.add_argument("--suffix", default="",
                    help="报告文件名后缀（如 _backfill），默认与系统命名完全一致")
    args = ap.parse_args(argv)

    cfg = load_config(ROOT / "config.yaml")
    ai_cfg = cfg.get("ai") or {}
    report_dir = Path(args.report_dir) if args.report_dir else Path(cfg["system"]["report_dir"])
    report_dir.mkdir(parents=True, exist_ok=True)

    photo = args.photo.expanduser().resolve()
    if not photo.exists():
        print(f"❌ 照片不存在: {photo}", file=sys.stderr)
        return 1

    provider = build_provider(ai_cfg, allow_mock_fallback=False)
    info = provider.describe()
    if not provider.available:
        print(f"❌ AI 不可用 [{provider.unavailable_kind}]：{provider.unavailable_reason}",
              file=sys.stderr)
        print(f"   修复建议: {provider.unavailable_hint}", file=sys.stderr)
        return 1

    taken_at = photo_taken_at(photo)
    stamp = taken_at.strftime("%Y%m%d_%H%M%S")
    report_path = report_dir / f"report_{stamp}{args.suffix}.md"
    if report_path.exists():
        print(f"❌ 报告已存在，拒绝覆盖: {report_path}", file=sys.stderr)
        return 1

    print(f"模型: {info['model']}（provider={info['provider']}）")
    print(f"照片: {photo.name}（拍摄于 {taken_at:%Y-%m-%d %H:%M:%S}），AI 分析中…")

    try:
        markdown = provider.complete(
            image=photo.read_bytes(),
            prompt=str(ai_cfg.get("prompt") or DEFAULT_PROMPT),
            system=ai_cfg.get("system_prompt") or DEFAULT_SYSTEM,
            max_tokens=int(ai_cfg.get("max_tokens") or 1500),
            temperature=(float(ai_cfg["temperature"])
                         if ai_cfg.get("temperature") is not None else None),
            image_media_type=_MEDIA_TYPES.get(photo.suffix.lower(), "image/jpeg"),
            image_detail=ai_cfg.get("image_detail") or None,
            extra_body=ai_cfg.get("extra_body") or None,
        )
    except AIError as exc:
        print(f"❌ AI 分析失败 [{exc.kind}]：{exc}", file=sys.stderr)
        print(f"   修复建议: {exc.hint}", file=sys.stderr)
        return 1

    header = (
        f"# 花园生长报告 {taken_at.isoformat(timespec='seconds')}\n\n"
        f"> 🛠 **[BACKFILL]** 本报告为事后补跑：照片由树莓派于 "
        f"{taken_at:%Y-%m-%d %H:%M:%S} 拍摄，但当时 AI 分析失败、报告未生成；"
        f"本报告于 {datetime.now().isoformat(timespec='seconds')} 在同一张照片上"
        f"补跑 AI 分析（模型 {info['model']}）生成，**非当日实时结论**。\n\n"
        f"![garden]({photo})\n\n"
    )
    report_path.write_text(header + markdown, encoding="utf-8")

    score = PlantAnalyzer._extract_score(markdown)
    print(f"✅ 报告已生成: {report_path}")
    print(f"综合健康评分: {score if score else '（报告中未给出 1-5 分评分）'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
