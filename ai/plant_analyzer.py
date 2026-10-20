"""植物生长 AI 分析。

把花园照片交给可配置的多模态大模型（见 :mod:`ai.providers`），
输出 Markdown 格式的图文报告，并落盘为 ``.md`` 文件。

**绝不静默失败**：没有 API Key、模型不支持图片、被限流、输出被截断时，
默认抛 :class:`ai.errors.AIError`（带分类与修复建议），而不是返回一份
看起来正常的占位报告。只有显式打开 ``ai.allow_mock_fallback`` 才会降级，
且降级横幅一定会写进报告正文，评分也会置空。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from ai.errors import AIError
from ai.providers import MOCK_REPORT_TEXT, BaseProvider, MockProvider, build_provider

logger = logging.getLogger(__name__)

DEFAULT_PROMPT = (
    "你是一名资深园艺师。请基于这张花园照片，分析画面中可见盆栽的整体生长状况，\n"
    "分别从下列维度给出图文报告：\n"
    "  1. 整体观感与构图\n"
    "  2. 叶色 / 形态异常（黄叶、徒长、虫害、缺水、烂根迹象）\n"
    "  3. 浇水建议（结合热带 / 多肉 / 中生 / 水生 4 类）\n"
    "  4. 下一步养护 TODO 清单\n"
    "使用 Markdown，简洁分点，并在结尾给出 1-5 分的综合健康评分。"
)

DEFAULT_SYSTEM = "你是一名严谨的园艺师，只依据画面中可见的信息作答，不要编造看不到的细节。"


@dataclass
class AnalysisReport:
    photo_path: Path
    report_path: Path
    markdown: str
    score: Optional[int] = None
    # 是否为降级（非真实 AI）报告。True 时 score 恒为 None。
    degraded: bool = False
    degraded_reason: str = ""
    # 实际使用的模型（降级时为空）
    model: str = ""


class PlantAnalyzer:
    """按配置调用任一大模型完成照片分析。"""

    def __init__(self, ai_cfg: Optional[Dict[str, Any]], report_dir: str, *,
                 allow_mock_fallback: bool = False) -> None:
        self.cfg = ai_cfg or {}
        self.report_dir = Path(report_dir)
        self.report_dir.mkdir(parents=True, exist_ok=True)
        self.allow_mock_fallback = bool(allow_mock_fallback)

        self.provider: BaseProvider = build_provider(
            self.cfg, allow_mock_fallback=self.allow_mock_fallback,
        )
        # 最近一次失败信息（供状态页/告警抬头展示）
        self.last_error: Optional[Dict[str, str]] = None

        info = self.provider.describe()
        if not self.provider.available:
            logger.error(
                "AI 分析不可用 [%s]：%s | %s",
                self.provider.unavailable_kind or "unavailable",
                self.provider.unavailable_reason,
                self.provider.unavailable_hint,
            )
        else:
            logger.info(
                "AI 分析就绪：provider=%s model=%s vision=%s base_url=%s",
                info["provider"], info["model"], info["supports_vision"], info["base_url"],
            )

    # ------------------------------------------------------------------ #
    # 对外属性
    # ------------------------------------------------------------------ #
    @property
    def is_mock(self) -> bool:
        """当前是否处于降级（占位报告）状态。"""
        return not self.provider.available

    @property
    def error_kind(self) -> str:
        return self.provider.unavailable_kind or ""

    @property
    def mock_reason(self) -> str:
        return self.provider.unavailable_reason or ""

    @property
    def model(self) -> str:
        return self.provider.model if self.provider.available else ""

    def describe(self) -> Dict[str, Any]:
        """供状态页 / 自检工具使用的结构化描述。"""
        info = self.provider.describe()
        info["allow_mock_fallback"] = self.allow_mock_fallback
        info["last_error"] = self.last_error
        return info

    # ------------------------------------------------------------------ #
    # 主流程
    # ------------------------------------------------------------------ #
    def analyze(self, photo_path: Path) -> AnalysisReport:
        photo_path = Path(photo_path)
        if not photo_path.exists():
            raise FileNotFoundError(photo_path)

        markdown, degraded, reason = self._run(photo_path)

        # 降级报告不给出评分 —— 假的分数会顺着钉钉标题一路传出去
        score = None if degraded else self._extract_score(markdown)

        if degraded:
            markdown = self._degraded_banner(reason) + markdown

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        report_path = self.report_dir / f"report_{ts}.md"
        header = f"# 花园生长报告 {datetime.now().isoformat(timespec='seconds')}\n\n"
        header += f"![garden]({photo_path.resolve()})\n\n"
        report_path.write_text(header + markdown, encoding="utf-8")
        logger.info("AI 分析报告已生成: %s（degraded=%s）", report_path, degraded)

        return AnalysisReport(
            photo_path=photo_path,
            report_path=report_path,
            markdown=markdown,
            score=score,
            degraded=degraded,
            degraded_reason=reason,
            model="" if degraded else self.model,
        )

    def _run(self, photo_path: Path) -> tuple:
        """返回 (markdown, degraded, reason)。未开启降级时失败会直接抛 AIError。"""
        prompt = str(self.cfg.get("prompt") or DEFAULT_PROMPT)
        system = self.cfg.get("system_prompt") or DEFAULT_SYSTEM
        max_tokens = int(self.cfg.get("max_tokens") or 1500)
        temperature = self.cfg.get("temperature")
        if temperature is not None:
            temperature = float(temperature)
        image_detail = self.cfg.get("image_detail") or None
        extra_body = self.cfg.get("extra_body") or None

        def _call() -> str:
            return self.provider.complete(
                image=photo_path.read_bytes(),
                prompt=prompt,
                system=system,
                max_tokens=max_tokens,
                temperature=temperature,
                image_media_type=_media_type_for(photo_path),
                image_detail=image_detail,
                extra_body=extra_body,
            )

        # 构造期就已确定降级（缺 Key / provider=mock），且显式允许降级 → 直接给占位报告
        if not self.provider.available and self.allow_mock_fallback:
            reason = f"{self.error_kind}：{self.mock_reason}"
            logger.warning("AI 不可用，使用占位报告：%s", reason)
            return MOCK_REPORT_TEXT, True, reason

        try:
            text = _call()
        except AIError as exc:
            # 注意：这里**不能**再调用 provider 取文本 —— 失败的正是它本身
            self.last_error = {"kind": exc.kind, "message": str(exc), "hint": exc.hint}
            if not self.allow_mock_fallback:
                logger.error("AI 分析失败 [%s]：%s | %s", exc.kind, exc, exc.hint)
                raise
            logger.warning(
                "AI 分析失败 [%s]，按 ai.allow_mock_fallback 降级为占位报告：%s",
                exc.kind, exc,
            )
            return MOCK_REPORT_TEXT, True, f"{exc.kind}：{exc}"

        self.last_error = None
        return text, False, ""

    def _degraded_banner(self, reason: str) -> str:
        kind = self.error_kind or "unknown"
        full_reason = reason or self.mock_reason
        return (
            "\n> ⚠️ **[MOCK] 本报告不是真实 AI 分析结果，内容为占位模板，不可信**\n"
            f"> 原因分类: `{kind}`｜{full_reason}\n"
            "> 报告评分已置空。修复方式见 `python tools/ai_diag.py`\n\n"
        )

    # ------------------------------------------------------------------ #
    # 工具
    # ------------------------------------------------------------------ #
    @staticmethod
    def _extract_score(markdown: str) -> Optional[int]:
        import re

        m = re.search(r"综合健康评分[^0-9]*([1-5])", markdown)
        return int(m.group(1)) if m else None

    @staticmethod
    def _mock_report(_photo_path: Optional[Path] = None) -> str:
        """占位报告（保留为静态方法以兼容既有调用/测试）。"""
        from ai.providers import MOCK_REPORT_TEXT

        return MOCK_REPORT_TEXT


_MEDIA_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


def _media_type_for(path: Path) -> str:
    return _MEDIA_TYPES.get(path.suffix.lower(), "image/jpeg")
