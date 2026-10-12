"""可插拔的 AI 供应商适配层。

支持两类 API 风格：

* ``anthropic`` —— Anthropic Messages API。既用于 Claude 官方，
  也可指向任何 Anthropic 兼容端点（例如 DeepSeek 的 ``https://api.deepseek.com/anthropic``）。
* ``openai``    —— OpenAI Chat Completions 兼容端点。覆盖 DeepSeek / OpenAI /
  通义 / Moonshot / 智谱 / Ollama / vLLM / 任何自建网关。

设计原则与 ``hardware/`` 保持一致：**绝不静默失败**。

* 缺 key、模型不支持图片、被限流、连不上、输出被截断 —— 一律抛 :class:`AIError`，
  带**分类 kind** 与**可执行 hint**；
* 只有显式打开 ``ai.allow_mock_fallback`` 才会降级为占位报告，
  并且降级横幅必须写进报告正文（由 :mod:`ai.plant_analyzer` 负责）。

关于「图片被静默丢弃」这一最危险的场景：多数 OpenAI 兼容网关在**纯文本模型**
收到图片时**不报错**，而是把图片替换成占位文字后照常返回 200，模型看不到图，
于是**凭空编造**一段看起来很正常的植物分析。因此这里做了**发送前拦截**：
按 ``vision_models`` 白名单（或 ``ai.supports_vision``）判定模型是否有视觉能力，
不满足就直接失败，绝不把照片发出去。
"""
from __future__ import annotations

import base64
import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

import httpx

from ai.errors import KIND_HINTS, AIError

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SEC = 60.0
DEFAULT_MAX_TOKENS = 1500


# --------------------------------------------------------------------------- #
# 预设
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ProviderPreset:
    """一个供应商的默认参数。

    模型名会随厂商迭代而变化，这里的取值只是**当下可用的常见默认**；
    生产环境建议在 config.yaml 里显式写 ``ai.model``，以厂商文档为准。
    """

    api_style: str                      # "anthropic" | "openai"
    base_url: str
    default_model: str
    api_key_env: str
    supports_vision: bool = True
    # 给定则**严格按模型名**判定视觉能力（用于同一家厂商既有视觉模型又有纯文本模型的场景）。
    vision_models: Optional[frozenset] = None
    api_key_optional: bool = False      # 本地部署（如 Ollama）不需要 key
    note: str = ""


PRESETS: Dict[str, ProviderPreset] = {
    "anthropic": ProviderPreset(
        api_style="anthropic",
        base_url="https://api.anthropic.com",
        default_model="claude-opus-4-7",
        api_key_env="ANTHROPIC_API_KEY",
        note="Claude 官方。Claude 3 及以后的模型均支持图片输入。",
    ),
    "deepseek": ProviderPreset(
        api_style="openai",
        base_url="https://api.deepseek.com",
        default_model="deepseek-flash",
        api_key_env="DEEPSEEK_API_KEY",
        # DeepSeek 同一厂商内视觉能力并不统一：flash 支持图片，v4-pro 是纯文本。
        # 按模型名严格判定，避免把照片发给纯文本模型后拿到编造的分析。
        vision_models=frozenset({"deepseek-flash", "deepseek-v4-flash-vision-exp"}),
        note="DeepSeek 官方。视觉能力仅在 deepseek-flash 上提供；deepseek-v4-pro 为纯文本。",
    ),
    "openai": ProviderPreset(
        api_style="openai",
        base_url="https://api.openai.com/v1",
        default_model="gpt-4o",
        api_key_env="OPENAI_API_KEY",
        note="OpenAI 官方。",
    ),
    "openai_compatible": ProviderPreset(
        api_style="openai",
        base_url="",
        default_model="",
        api_key_env="OPENAI_API_KEY",
        note="任意 OpenAI 兼容端点：必须显式填写 ai.base_url 与 ai.model。",
    ),
    "ollama": ProviderPreset(
        api_style="openai",
        base_url="http://localhost:11434/v1",
        default_model="llama3.2-vision",
        api_key_env="OLLAMA_API_KEY",
        api_key_optional=True,
        note="本地 Ollama（OpenAI 兼容）。默认不校验 key；模型名以本地实际拉取的为准。",
    ),
}


# --------------------------------------------------------------------------- #
# 工具函数
# --------------------------------------------------------------------------- #
def _join_url(base: str, path: str) -> str:
    return (base or "").rstrip("/") + path


def _snippet(text: Optional[str], limit: int = 300) -> str:
    s = (text or "").strip().replace("\n", " ")
    return s if len(s) <= limit else s[:limit] + "…"


def _classify_http_error(status: int, text: str) -> Tuple[str, Optional[str]]:
    """按状态码 + 响应正文关键词归类，返回 (kind, hint)。"""
    low = (text or "").lower()
    if status in (401, 403):
        return "auth_failed", None
    if status == 429:
        return "rate_limited", None
    if status == 404:
        return "model_not_found", None
    if status in (400, 422):
        if any(k in low for k in (
            "supported api model", "model not", "invalid model",
            "unknown model", "does not exist", "no such model",
        )):
            return "model_not_found", None
        if any(k in low for k in ("image", "vision", "multimodal", "image_url")):
            return "vision_unsupported", None
        if any(k in low for k in ("max_tokens", "context length", "too long", "truncat")):
            return "truncated", None
        return "bad_response", None
    if status >= 500:
        return "unreachable", None
    return "unknown", None


# 显式降级时使用的占位报告。绝不是"看起来像真的"的分析 —— 内容本身就是模板。
MOCK_REPORT_TEXT = (
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


def _extract_openai_text(resp: httpx.Response) -> str:
    """从 OpenAI 兼容响应里取出正文，并把「空正文」的原因说清楚。"""
    try:
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        raise AIError(f"响应不是合法 JSON：{_snippet(resp.text)}", kind="bad_response") from exc

    err = data.get("error")
    if isinstance(err, dict):
        msg = err.get("message") or str(err)
        kind, hint = _classify_http_error(resp.status_code, msg)
        raise AIError(f"接口返回错误：{_snippet(msg)}", kind=kind, hint=hint)

    choices = data.get("choices") or []
    if not choices:
        raise AIError(f"响应缺少 choices 字段：{_snippet(resp.text)}", kind="bad_response")

    choice = choices[0] or {}
    finish = choice.get("finish_reason")
    message = choice.get("message") or {}
    text = message.get("content")

    if isinstance(text, list):  # 少数实现把 content 拆成内容块数组
        text = "".join(p.get("text", "") for p in text if isinstance(p, dict))
    text = (text or "").strip()

    if not text:
        reasoning = (message.get("reasoning_content") or "").strip()
        if finish == "length":
            extra = "（输出 token 全部消耗在思考内容上）" if reasoning else ""
            raise AIError(f"模型输出被 max_tokens 截断，未产出正文{extra}", kind="truncated")
        if reasoning:
            raise AIError("模型只返回了思考内容、没有正文", kind="empty_response")
        raise AIError("模型返回了空内容", kind="empty_response")
    return text


# --------------------------------------------------------------------------- #
# Provider
# --------------------------------------------------------------------------- #
class BaseProvider(ABC):
    """供应商适配基类。子类只需实现 :meth:`complete`。"""

    api_style: str = ""

    def __init__(self, name: str, *, model: str, api_key: str = "", base_url: str = "",
                 supports_vision: bool = True, api_key_env: str = "",
                 timeout_sec: float = DEFAULT_TIMEOUT_SEC) -> None:
        self.name = name
        self.model = model
        self.api_key = api_key or ""
        self.base_url = base_url or ""
        self.api_key_env = api_key_env or ""
        self.supports_vision = bool(supports_vision)
        self.timeout_sec = float(timeout_sec or DEFAULT_TIMEOUT_SEC)
        # 可用性。UnavailableProvider / MockProvider 会覆盖为 False 并填原因。
        self.available = True
        self.unavailable_kind = ""
        self.unavailable_reason = ""
        self.unavailable_hint = ""

    # ---- 子类实现 ----
    @abstractmethod
    def complete(self, *, image: Optional[bytes] = None, prompt: str = "",
                 system: Optional[str] = None, max_tokens: int = DEFAULT_MAX_TOKENS,
                 temperature: Optional[float] = None, image_media_type: str = "image/jpeg",
                 image_detail: Optional[str] = None,
                 extra_body: Optional[Dict[str, Any]] = None) -> str:
        """发起一次对话补全，返回正文文本。失败时必须抛 :class:`AIError`。"""

    # ---- 公共能力 ----
    def guard_vision(self, image: Optional[bytes]) -> None:
        """发送前拦截：把图片交给没有视觉能力的模型，等于让它编造分析。"""
        if image and not self.supports_vision:
            raise AIError(
                f"模型 {self.model} 被判定为不支持图片输入（provider={self.name}，"
                f"依据预设 vision_models 白名单或 ai.supports_vision），已拒绝发送照片",
                kind="vision_unsupported",
            )

    def describe(self) -> Dict[str, Any]:
        return {
            "provider": self.name,
            "api_style": self.api_style,
            "model": self.model,
            "base_url": self.base_url,
            "api_key_env": self.api_key_env,
            "api_key_set": bool(self.api_key),
            "supports_vision": self.supports_vision,
            "available": self.available,
            "unavailable_kind": self.unavailable_kind,
            "unavailable_reason": self.unavailable_reason,
        }

    def __repr__(self) -> str:  # noqa: D105
        return (f"<{type(self).__name__} name={self.name} model={self.model} "
                f"vision={self.supports_vision} available={self.available}>")


class OpenAICompatProvider(BaseProvider):
    """OpenAI Chat Completions 兼容端点（httpx 直连，不引入额外 SDK）。"""

    api_style = "openai"

    @property
    def chat_url(self) -> str:
        return _join_url(self.base_url, "/chat/completions")

    def build_body(self, *, image: Optional[bytes], prompt: str, system: Optional[str],
                   max_tokens: int, temperature: Optional[float],
                   image_media_type: str, image_detail: Optional[str],
                   extra_body: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """构造请求体（独立出来便于测试，不必真的发请求）。"""
        content: list = [{"type": "text", "text": prompt}]
        if image:
            image_url: Dict[str, Any] = {
                "url": f"data:{image_media_type};base64,"
                       + base64.standard_b64encode(image).decode("ascii")
            }
            if image_detail:
                image_url["detail"] = image_detail
            content.append({"type": "image_url", "image_url": image_url})

        messages: list = []
        if system:
            messages.append({"role": "system", "content": system})
        # 图片只能出现在 user 消息里（DeepSeek 对 system/assistant 里的图片直接 400）
        messages.append({"role": "user", "content": content})

        body: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": int(max_tokens),
        }
        if temperature is not None:
            body["temperature"] = float(temperature)
        if extra_body:
            body.update(extra_body)
        return body

    def complete(self, *, image: Optional[bytes] = None, prompt: str = "",
                 system: Optional[str] = None, max_tokens: int = DEFAULT_MAX_TOKENS,
                 temperature: Optional[float] = None, image_media_type: str = "image/jpeg",
                 image_detail: Optional[str] = None,
                 extra_body: Optional[Dict[str, Any]] = None) -> str:
        self.guard_vision(image)
        body = self.build_body(
            image=image, prompt=prompt, system=system, max_tokens=max_tokens,
            temperature=temperature, image_media_type=image_media_type,
            image_detail=image_detail, extra_body=extra_body,
        )
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = "Bearer " + self.api_key

        try:
            resp = httpx.post(self.chat_url, headers=headers, json=body,
                              timeout=self.timeout_sec)
        except httpx.TimeoutException as exc:
            raise AIError(
                f"请求 {self.chat_url} 超时（{self.timeout_sec:g}s）",
                kind="unreachable",
            ) from exc
        except httpx.HTTPError as exc:
            raise AIError(f"请求 {self.chat_url} 失败：{exc}", kind="unreachable") from exc

        if resp.status_code >= 400:
            kind, hint = _classify_http_error(resp.status_code, resp.text)
            raise AIError(
                f"{self.name} 返回 HTTP {resp.status_code}：{_snippet(resp.text)}",
                kind=kind, hint=hint,
            )
        return _extract_openai_text(resp)


class AnthropicProvider(BaseProvider):
    """Anthropic Messages API（用官方 SDK，支持 base_url 指向兼容端点）。"""

    api_style = "anthropic"

    def complete(self, *, image: Optional[bytes] = None, prompt: str = "",
                 system: Optional[str] = None, max_tokens: int = DEFAULT_MAX_TOKENS,
                 temperature: Optional[float] = None, image_media_type: str = "image/jpeg",
                 image_detail: Optional[str] = None,
                 extra_body: Optional[Dict[str, Any]] = None) -> str:
        self.guard_vision(image)
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - 依赖缺失时才会走到
            raise AIError(
                "未安装 anthropic 包，无法使用 api_style=anthropic",
                kind="invalid_config",
                hint="pip install -r requirements.txt（其中包含 anthropic）",
            ) from exc

        if not self.api_key:
            raise AIError(f"缺少 API Key（环境变量 {self.api_key_env or '未配置'}）",
                          kind="no_api_key")

        content: list = []
        if image:
            content.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": image_media_type,
                    "data": base64.standard_b64encode(image).decode("ascii"),
                },
            })
        content.append({"type": "text", "text": prompt})

        request: Dict[str, Any] = {
            "model": self.model,
            "max_tokens": int(max_tokens),
            "messages": [{"role": "user", "content": content}],
        }
        if system:
            request["system"] = system
        if temperature is not None:
            request["temperature"] = float(temperature)
        if extra_body:
            request.update(extra_body)

        kwargs: Dict[str, Any] = {"api_key": self.api_key, "timeout": self.timeout_sec}
        if self.base_url and self.base_url.rstrip("/") != "https://api.anthropic.com":
            kwargs["base_url"] = self.base_url

        try:
            client = anthropic.Anthropic(**kwargs)
            resp = client.messages.create(**request)
        except anthropic.AuthenticationError as exc:
            raise AIError(f"认证失败：{_snippet(str(exc))}", kind="auth_failed") from exc
        except anthropic.RateLimitError as exc:
            raise AIError(f"触发限流：{_snippet(str(exc))}", kind="rate_limited") from exc
        except anthropic.NotFoundError as exc:
            raise AIError(f"模型不存在：{_snippet(str(exc))}", kind="model_not_found") from exc
        except anthropic.APIStatusError as exc:
            kind, hint = _classify_http_error(getattr(exc, "status_code", 0), str(exc))
            raise AIError(f"Anthropic 返回错误：{_snippet(str(exc))}",
                          kind=kind, hint=hint) from exc
        except anthropic.APIConnectionError as exc:
            raise AIError(f"无法连接 Anthropic：{_snippet(str(exc))}",
                          kind="unreachable") from exc

        text = "\n".join(
            block.text for block in getattr(resp, "content", [])
            if getattr(block, "type", "") == "text"
        ).strip()
        if not text:
            raise AIError("模型返回了空内容", kind="empty_response")
        return text


class UnavailableProvider(BaseProvider):
    """配置不全或不可用：任何调用都抛 :class:`AIError`（fail-fast，不伪装成成功）。"""

    api_style = "none"

    def __init__(self, name: str, *, kind: str, reason: str, hint: Optional[str] = None,
                 model: str = "", base_url: str = "", supports_vision: bool = False) -> None:
        super().__init__(name, model=model or "(未配置)", base_url=base_url,
                         supports_vision=supports_vision)
        self.available = False
        self.unavailable_kind = kind
        self.unavailable_reason = reason
        self.unavailable_hint = hint if hint is not None else KIND_HINTS.get(kind, "")

    def complete(self, **_: Any) -> str:  # noqa: D102
        raise AIError(self.unavailable_reason, kind=self.unavailable_kind,
                      hint=self.unavailable_hint)


class MockProvider(BaseProvider):
    """显式降级：只在 ``ai.allow_mock_fallback=true`` 时使用。

    返回的是一眼可辨的模板文本，横幅由 :mod:`ai.plant_analyzer` 写进报告。
    """

    api_style = "mock"

    def __init__(self, *, kind: str = "mock", reason: str = "", model: str = "(mock)") -> None:
        super().__init__("mock", model=model, supports_vision=False)
        self.available = False
        self.unavailable_kind = kind
        self.unavailable_reason = reason or "已按 ai.allow_mock_fallback 降级为占位报告"
        self.unavailable_hint = KIND_HINTS.get(kind, "")

    @property
    def is_mock(self) -> bool:
        return True

    def complete(self, **_: Any) -> str:  # noqa: D102
        return MOCK_REPORT_TEXT


# --------------------------------------------------------------------------- #
# 工厂
# --------------------------------------------------------------------------- #
def resolve_settings(ai_cfg: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """把 config.yaml 的 ``ai:`` 段与内置预设合并成最终生效参数。"""
    cfg = ai_cfg or {}
    name = str(cfg.get("provider") or "anthropic").strip()
    preset = PRESETS.get(name)

    base_url = str(cfg.get("base_url") or (preset.base_url if preset else "")).strip()
    model = str(cfg.get("model") or (preset.default_model if preset else "")).strip()
    api_key_env = str(cfg.get("api_key_env") or (preset.api_key_env if preset else "")).strip()
    api_style = str(cfg.get("api_style") or (preset.api_style if preset else "openai")).strip()

    # 视觉能力判定优先级：显式配置 > 预设的模型白名单 > 预设默认
    if cfg.get("supports_vision") is not None:
        supports_vision = bool(cfg["supports_vision"])
    elif preset is not None and preset.vision_models is not None:
        supports_vision = model in preset.vision_models
    elif preset is not None:
        supports_vision = preset.supports_vision
    else:
        supports_vision = True

    api_key = os.environ.get(api_key_env, "").strip() if api_key_env else ""

    return {
        "name": name,
        "preset": preset,
        "known": preset is not None,
        "api_style": api_style,
        "base_url": base_url,
        "model": model,
        "api_key_env": api_key_env,
        "api_key": api_key,
        "supports_vision": supports_vision,
        "api_key_optional": bool(preset.api_key_optional) if preset else False,
        "timeout_sec": float(cfg.get("timeout_sec") or DEFAULT_TIMEOUT_SEC),
    }


def build_provider(ai_cfg: Optional[Dict[str, Any]], *,
                   allow_mock_fallback: bool = False) -> BaseProvider:
    """按配置构造 provider。

    参数不可用（缺 key、未知 provider、缺 base_url/model）时：
    * ``allow_mock_fallback=True`` → 返回 :class:`MockProvider`（带降级原因）
    * 否则 → 返回 :class:`UnavailableProvider`，调用时抛 :class:`AIError`
    """
    s = resolve_settings(ai_cfg)
    name = s["name"]

    if name == "mock":
        return MockProvider(kind="mock", reason="config.yaml 显式指定 ai.provider=mock")

    if not s["known"]:
        if not s["base_url"]:
            return UnavailableProvider(
                name, kind="invalid_config",
                reason=f"未知 provider「{name}」，且未配置 ai.base_url",
                hint=(f"内置可选：{', '.join(sorted(PRESETS))}。"
                      f"其它厂商请用 provider=openai_compatible 并填写 ai.base_url。"),
                model=s["model"],
            )
        logger.warning(
            "provider「%s」不在内置预设中，按 %s 兼容端点处理（base_url=%s）",
            name, s["api_style"], s["base_url"],
        )

    if not s["base_url"]:
        return UnavailableProvider(
            name, kind="invalid_config",
            reason=f"provider「{name}」需要 ai.base_url",
            hint="openai_compatible 必须显式提供 base_url，例如 https://api.moonshot.cn/v1",
            model=s["model"],
        )
    if not s["model"]:
        return UnavailableProvider(
            name, kind="invalid_config",
            reason=f"provider「{name}」需要 ai.model",
            hint="请显式指定 ai.model，例如 deepseek-flash / claude-opus-4-7 / gpt-4o",
        )

    if not s["api_key"] and not s["api_key_optional"]:
        reason = f"未设置环境变量 {s['api_key_env'] or '(ai.api_key_env 未配置)'}"
        if allow_mock_fallback:
            return MockProvider(kind="no_api_key",
                                reason=reason + "，已按 allow_mock_fallback 降级")
        return UnavailableProvider(
            name, kind="no_api_key", reason=reason,
            hint=f"请在 .env 中设置 {s['api_key_env']}，或运行 `python tools/ai_diag.py` 自检。",
            model=s["model"], base_url=s["base_url"],
            supports_vision=s["supports_vision"],
        )

    kwargs: Dict[str, Any] = {
        "model": s["model"],
        "api_key": s["api_key"],
        "base_url": s["base_url"],
        "supports_vision": s["supports_vision"],
        "api_key_env": s["api_key_env"],
        "timeout_sec": s["timeout_sec"],
    }
    if s["api_style"] == "anthropic":
        return AnthropicProvider(name, **kwargs)
    return OpenAICompatProvider(name, **kwargs)


__all__ = [
    "PRESETS", "ProviderPreset", "BaseProvider", "OpenAICompatProvider",
    "AnthropicProvider", "UnavailableProvider", "MockProvider",
    "build_provider", "resolve_settings", "MOCK_REPORT_TEXT",
]
