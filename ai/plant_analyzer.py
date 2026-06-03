"""植物生长 AI 分析。

调用 Claude 多模态接口，输入花园照片，输出 Markdown 格式的图文分析报告。
报告同时保存为 .md 文件，方便后续展示或推送。
"""
from __future__ import annotations

import base64
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

import anthropic

logger = logging.getLogger(__name__)


@dataclass
class AnalysisReport:
    photo_path: Path
    report_path: Path
    markdown: str
    score: Optional[int] = None


class PlantAnalyzer:
    def __init__(self, ai_cfg: Dict, report_dir: str) -> None:
        self.cfg = ai_cfg
        self.report_dir = Path(report_dir)
        self.report_dir.mkdir(parents=True, exist_ok=True)
        self.api_key = os.environ.get(ai_cfg.get("api_key_env", "ANTHROPIC_API_KEY"))
        if not self.api_key:
            logger.warning("未设置 %s，AI 分析将返回占位结果", ai_cfg.get("api_key_env"))
        self._client = None

    def analyze(self, photo_path: Path) -> AnalysisReport:
        photo_path = Path(photo_path)
        if not photo_path.exists():
            raise FileNotFoundError(photo_path)

        markdown = self._call_claude(photo_path) if self.api_key else self._mock_report(photo_path)
        score = self._extract_score(markdown)

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        report_path = self.report_dir / f"report_{ts}.md"
        header = f"# 花园生长报告 {datetime.now().isoformat(timespec='seconds')}\n\n"
        header += f"![garden]({photo_path.resolve()})\n\n"
        report_path.write_text(header + markdown, encoding="utf-8")
        logger.info("AI 分析报告已生成: %s", report_path)
        return AnalysisReport(photo_path=photo_path, report_path=report_path, markdown=markdown, score=score)

    def _call_claude(self, photo_path: Path) -> str:
        if self._client is None:
            self._client = anthropic.Anthropic(api_key=self.api_key)
        with photo_path.open("rb") as fp:
            data_b64 = base64.standard_b64encode(fp.read()).decode("ascii")

        response = self._client.messages.create(
            model=self.cfg.get("model", "claude-opus-4-7"),
            max_tokens=int(self.cfg.get("max_tokens", 1500)),
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/jpeg",
                                "data": data_b64,
                            },
                        },
                        {"type": "text", "text": self.cfg.get("prompt", "请分析植物生长状况")},
                    ],
                }
            ],
        )
        # SDK 返回的 content 是块列表，取所有 text 拼接
        return "\n".join(block.text for block in response.content if getattr(block, "type", "") == "text")

    @staticmethod
    def _mock_report(photo_path: Path) -> str:
        return (
            "## 整体观感与构图\n"
            "- 花园整体绿意盎然，盆栽分区清晰。\n\n"
            "## 叶色 / 形态\n"
            "- 多肉区光照充足，叶片饱满。\n"
            "- 中生植物叶尖略黄，可能为浇水偏多。\n\n"
            "## 浇水建议\n"
            "- 热带植物：保持常规浇水。\n"
            "- 多肉植物：本周可暂停。\n"
            "- 中生植物：减少 30% 水量。\n"
            "- 水生植物：补水至刻度线。\n\n"
            "## TODO\n"
            "1. 修剪中生区黄叶。\n"
            "2. 检查热带区滴管是否通畅。\n\n"
            "**综合健康评分：4 / 5**\n"
        )

    @staticmethod
    def _extract_score(markdown: str) -> Optional[int]:
        import re

        m = re.search(r"综合健康评分[^0-9]*([1-5])", markdown)
        return int(m.group(1)) if m else None
