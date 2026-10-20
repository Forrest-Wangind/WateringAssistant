"""AI 分析层错误类型。

与 `hardware/errors.py` 同构：每个错误都带**分类 kind** 与**可执行 hint**，
确保「分析失败」永远以显式、可定位的方式暴露，而不是悄悄返回一份占位报告。
"""
from __future__ import annotations

from typing import Dict, Optional

# 分类 -> 默认处置建议。调用方可在构造时用 hint 覆盖。
KIND_HINTS: Dict[str, str] = {
    "no_api_key": (
        "未找到 API Key。请在 .env 里按下述变量名设置，"
        "或运行 `python tools/ai_diag.py` 自检："
    ),
    "auth_failed": (
        "API Key 无效、过期或权限不足。到供应商控制台确认 key 状态，"
        "并核对它与 config.yaml 中 `ai.api_key_env` 指向的变量是否一致。"
    ),
    "rate_limited": (
        "触发限流或账户余额不足。稍后重试，或调整 `ai.provider` / `ai.model`。"
    ),
    "vision_unsupported": (
        "当前模型不支持图片输入，无法分析照片 —— 这一点必须显式失败，"
        "因为纯文本模型收到图片时往往仍返回 HTTP 200，只会把图片丢成占位文字，"
        "然后**凭空编造**一段看起来正常的植物分析。"
        "请改用带视觉能力的模型：DeepSeek 用 `deepseek-flash`"
        "（`deepseek-v4-pro` 仅支持文本）。"
    ),
    "model_not_found": (
        "模型名不存在或当前账户无权调用。核对 `ai.model` 的拼写与可用性"
        "（DeepSeek 现支持 `deepseek-flash` / `deepseek-v4-pro`）。"
    ),
    "unreachable": (
        "无法连接供应商接口。检查网络/代理是否可达，"
        "并核对 `ai.base_url` 是否是有效的 OpenAI / Anthropic 兼容端点。"
    ),
    "bad_response": (
        "接口返回格式不符合预期（拿不到分析文本）。通常说明 `ai.base_url` "
        "指向的并不是兼容端点，或命中了一个非对话接口。"
    ),
    "empty_response": (
        "模型返回了空内容。常见原因：`ai.max_tokens` 太小 —— "
        "开启思考模式的模型会先把 token 花在思维链上，正文就没了。"
        "可提高 `ai.max_tokens`，或按 docs/ai_providers.md 关闭思考模式。"
    ),
    "truncated": (
        "输出被 `ai.max_tokens` 截断。请提高该值，"
        "或关闭思考模式（思考内容同样占用输出 token）。"
    ),
    "invalid_config": (
        "AI 配置不完整或不可用。运行 `python tools/ai_diag.py` 查看具体缺哪一项。"
    ),
    "unknown": "未知错误，详见日志。",
}


class AIError(RuntimeError):
    """AI 分析失败。

    kind   —— 分类，用于分诊与告警抬头；
    hint   —— 可直接执行的修复动作，会在日志、钉钉消息与自检工具里展示。
    """

    def __init__(self, message: str, *, kind: str = "unknown",
                 hint: Optional[str] = None) -> None:
        self.kind = kind
        self.hint = hint if hint is not None else KIND_HINTS.get(kind, KIND_HINTS["unknown"])
        super().__init__(message)

    def __str__(self) -> str:  # noqa: D105
        return f"[{self.kind}] {super().__str__()}"

    def as_dict(self) -> Dict[str, str]:
        """结构化输出，供 Web 接口 / 状态页直接使用（与 HardwareError.as_dict 同构）。"""
        return {"kind": self.kind, "message": super().__str__(), "hint": self.hint}
