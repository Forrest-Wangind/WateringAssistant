"""调用通义千问 qwen-vl-max，对花园照片做生长分析并产出 Markdown 报告。

使用 DashScope 的 OpenAI 兼容接口，复用 openai SDK，避免引入新 SDK。
"""
from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from openai import OpenAI

from .config import VisionConfig

logger = logging.getLogger(__name__)


SYSTEM_PROMPT = """你是一位资深园艺师与植物生长分析专家。用户会给你一张花园/盆栽照片，请输出一份**结构化的中文 JSON**，字段：
- overall_health: 1-10 的整数综合健康评分
- summary: 一句话生长情况概述（不超过 40 字）
- observations: 字符串数组，3-5 条具体观察（叶色、土壤湿度、虫害迹象、长势等）
- risks: 字符串数组，可能的风险或问题（缺水、过湿、病害、光照不足等）；若无写 ["暂未发现明显问题"]
- recommendations: 字符串数组，给出具体的浇水/养护建议（包含建议浇水时长，单位秒）
- watering_advice: 对象 {channel_hint: 字符串描述, suggest_seconds: 推荐浇水秒数(整数)}

只输出合法 JSON，不要 ``` 代码块，不要解释。"""


@dataclass
class VisionAnalysis:
    overall_health: int
    summary: str
    observations: list
    risks: list
    recommendations: list
    suggest_seconds: int
    raw_text: str

    def to_markdown(self, photo_name: str) -> str:
        lines = [
            f"# 🌱 花园生长日报",
            "",
            f"**照片**: {photo_name}  ",
            f"**综合健康评分**: {self.overall_health} / 10  ",
            f"**概述**: {self.summary}",
            "",
            "## 🔍 观察",
        ]
        lines.extend(f"- {x}" for x in self.observations)
        lines.append("")
        lines.append("## ⚠️ 风险")
        lines.extend(f"- {x}" for x in self.risks)
        lines.append("")
        lines.append("## 💡 养护建议")
        lines.extend(f"- {x}" for x in self.recommendations)
        lines.append("")
        lines.append(f"**建议浇水时长**: {self.suggest_seconds} 秒")
        return "\n".join(lines)


def _encode_image(path: Path) -> str:
    mime = "image/jpeg" if path.suffix.lower() in {".jpg", ".jpeg"} else "image/png"
    data = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{data}"


class VisionAnalyzer:
    def __init__(self, vision_cfg: VisionConfig, api_key: str):
        if not api_key:
            raise ValueError("DASHSCOPE_API_KEY 未配置")
        self._cfg = vision_cfg
        self._client = OpenAI(
            api_key=api_key,
            base_url=vision_cfg.base_url,
            timeout=vision_cfg.timeout,
        )

    def analyze(self, image_path: Path) -> VisionAnalysis:
        image_url = _encode_image(image_path)
        logger.info("调用 %s 分析图片: %s", self._cfg.model, image_path.name)

        resp = self._client.chat.completions.create(
            model=self._cfg.model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": image_url}},
                        {"type": "text", "text": "请按规定的 JSON 格式分析这张花园照片。"},
                    ],
                },
            ],
            temperature=0.3,
        )
        raw = resp.choices[0].message.content or ""
        return self._parse(raw)

    @staticmethod
    def _parse(raw: str) -> VisionAnalysis:
        text = raw.strip()
        # 容错：剥离可能的 ```json ... ```
        if text.startswith("```"):
            text = text.strip("`")
            if text.lower().startswith("json"):
                text = text[4:]
            text = text.strip()
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            logger.warning("视觉模型返回非 JSON，原文回填: %s", text[:200])
            return VisionAnalysis(
                overall_health=0,
                summary="模型返回格式异常，原文见下",
                observations=[text[:500]],
                risks=["解析失败"],
                recommendations=["人工查看原始返回"],
                suggest_seconds=0,
                raw_text=raw,
            )

        watering = data.get("watering_advice") or {}
        return VisionAnalysis(
            overall_health=int(data.get("overall_health", 0) or 0),
            summary=str(data.get("summary", "")),
            observations=list(data.get("observations", [])),
            risks=list(data.get("risks", [])),
            recommendations=list(data.get("recommendations", [])),
            suggest_seconds=int(watering.get("suggest_seconds", 0) or 0),
            raw_text=raw,
        )
