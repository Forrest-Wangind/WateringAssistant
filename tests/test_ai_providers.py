"""AI provider 适配层单元测试。

重点覆盖「绝不静默失败」相关的判定：
  * 视觉能力白名单（纯文本模型必须被拦截，而不是把图片丢给模型编造分析）
  * 缺 Key 时 fail-fast（不降级则为不可用 provider）
  * HTTP 错误分类（401/429/404/400-model/400-image/5xx）
  * 输出为空 / 被 max_tokens 截断时给出可定位的错误
"""
from __future__ import annotations

import json
from typing import Any, Dict

import pytest

import _bootstrap  # noqa: F401  必须在导入项目模块之前执行：把仓库根目录加入 sys.path
from ai import providers as pv
from ai.errors import AIError
from ai.providers import (
    AnthropicProvider,
    MockProvider,
    OpenAICompatProvider,
    UnavailableProvider,
    build_provider,
    resolve_settings,
)


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #
class FakeResp:
    def __init__(self, status: int, payload: Any, text: str | None = None) -> None:
        self.status_code = status
        self._payload = payload
        if text is not None:
            self.text = text
        elif isinstance(payload, (dict, list)):
            self.text = json.dumps(payload, ensure_ascii=False)
        else:
            self.text = str(payload)

    def json(self) -> Any:
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def ok_payload(text: str = "## 报告\n\n综合健康评分：5 / 5") -> Dict[str, Any]:
    return {
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": text}}]
    }


def install_post(monkeypatch, handler):
    monkeypatch.setattr(pv.httpx, "post", handler)


# --------------------------------------------------------------------------- #
# 预设与配置解析
# --------------------------------------------------------------------------- #
def test_preset_deepseek_defaults():
    s = resolve_settings({"provider": "deepseek"})
    assert s["api_style"] == "openai"
    assert s["base_url"] == "https://api.deepseek.com"
    assert s["model"] == "deepseek-flash"
    assert s["api_key_env"] == "DEEPSEEK_API_KEY"
    assert s["supports_vision"] is True


def test_vision_whitelist_rejects_text_only_model():
    """deepseek-v4-pro 是纯文本模型 —— 必须自动判定为不支持图片。"""
    s = resolve_settings({"provider": "deepseek", "model": "deepseek-v4-pro"})
    assert s["supports_vision"] is False


def test_explicit_supports_vision_overrides_preset():
    s = resolve_settings({"provider": "deepseek", "model": "deepseek-v4-pro",
                          "supports_vision": True})
    assert s["supports_vision"] is True


def test_anthropic_preset():
    s = resolve_settings({"provider": "anthropic"})
    assert s["api_style"] == "anthropic"
    assert s["base_url"] == "https://api.anthropic.com"
    assert s["api_key_env"] == "ANTHROPIC_API_KEY"


def test_unknown_provider_with_base_url_is_treated_as_openai(monkeypatch):
    monkeypatch.setenv("MY_GW_KEY", "sk-x")
    provider = build_provider({
        "provider": "my_gateway", "base_url": "https://gw.example.com/v1",
        "model": "some-vision-model", "api_key_env": "MY_GW_KEY",
    })
    assert isinstance(provider, OpenAICompatProvider)
    assert provider.model == "some-vision-model"


# --------------------------------------------------------------------------- #
# 工厂：可用性判定
# --------------------------------------------------------------------------- #
def test_missing_key_without_fallback_is_unavailable(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    provider = build_provider({"provider": "deepseek"}, allow_mock_fallback=False)
    assert isinstance(provider, UnavailableProvider)
    assert provider.available is False
    assert provider.unavailable_kind == "no_api_key"
    with pytest.raises(AIError) as ei:
        provider.complete(prompt="hi")
    assert ei.value.kind == "no_api_key"
    assert ei.value.hint  # 必须带可执行建议


def test_missing_key_with_fallback_is_mock(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    provider = build_provider({"provider": "deepseek"}, allow_mock_fallback=True)
    assert isinstance(provider, MockProvider)
    assert provider.unavailable_kind == "no_api_key"
    assert "综合健康评分" in provider.complete()


def test_explicit_mock_provider():
    provider = build_provider({"provider": "mock"})
    assert isinstance(provider, MockProvider)


def test_unknown_provider_without_base_url_is_invalid_config():
    provider = build_provider({"provider": "no-such-vendor"}, allow_mock_fallback=False)
    assert isinstance(provider, UnavailableProvider)
    assert provider.unavailable_kind == "invalid_config"
    assert "base_url" in provider.unavailable_hint


def test_openai_compatible_requires_model(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
    provider = build_provider({
        "provider": "openai_compatible", "base_url": "https://gw.example.com/v1",
    })
    assert isinstance(provider, UnavailableProvider)
    assert provider.unavailable_kind == "invalid_config"


def test_ollama_does_not_require_key(monkeypatch):
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    provider = build_provider({"provider": "ollama"}, allow_mock_fallback=False)
    assert isinstance(provider, OpenAICompatProvider)
    assert provider.available is True


# --------------------------------------------------------------------------- #
# 视觉拦截
# --------------------------------------------------------------------------- #
def test_text_only_model_rejects_image_before_sending(monkeypatch):
    """核心防护：不能把照片发给纯文本模型，否则它会编造分析。"""
    provider = OpenAICompatProvider(
        "deepseek", model="deepseek-v4-pro", api_key="k",
        base_url="https://api.deepseek.com", supports_vision=False,
    )
    called = {"n": 0}
    install_post(monkeypatch, lambda *a, **k: called.__setitem__("n", called["n"] + 1))
    with pytest.raises(AIError) as ei:
        provider.complete(image=b"\xff\xd8\xff", prompt="看图")
    assert ei.value.kind == "vision_unsupported"
    assert called["n"] == 0, "必须在发出请求之前就拦截"


# --------------------------------------------------------------------------- #
# 请求体构造
# --------------------------------------------------------------------------- #
def test_build_body_with_image_and_detail():
    provider = OpenAICompatProvider(
        "deepseek", model="deepseek-flash", api_key="k",
        base_url="https://api.deepseek.com", supports_vision=True,
    )
    body = provider.build_body(
        image=b"\x01\x02", prompt="看图", system="sys",
        max_tokens=100, temperature=None, image_media_type="image/jpeg",
        image_detail="low", extra_body={"thinking": {"type": "disabled"}},
    )
    assert body["model"] == "deepseek-flash"
    assert body["max_tokens"] == 100
    assert "temperature" not in body, "temperature 为 None 时不应下发"
    assert body["thinking"] == {"type": "disabled"}, "厂商私有参数应原样透传"
    assert body["messages"][0] == {"role": "system", "content": "sys"}
    content = body["messages"][1]["content"]
    assert content[0]["type"] == "text"
    img = content[1]["image_url"]
    assert img["url"].startswith("data:image/jpeg;base64,")
    assert img["detail"] == "low"


def test_build_body_text_only_has_no_image_block():
    provider = OpenAICompatProvider(
        "openai", model="gpt-4o", api_key="k",
        base_url="https://api.openai.com/v1", supports_vision=True,
    )
    body = provider.build_body(
        image=None, prompt="hi", system=None, max_tokens=10,
        temperature=0.5, image_media_type="image/jpeg",
        image_detail=None, extra_body=None,
    )
    assert len(body["messages"]) == 1
    assert body["temperature"] == 0.5
    assert all(c["type"] != "image_url" for c in body["messages"][0]["content"])


def test_chat_url_join():
    p1 = OpenAICompatProvider("d", model="m", api_key="k", base_url="https://api.deepseek.com")
    p2 = OpenAICompatProvider("o", model="m", api_key="k", base_url="https://api.openai.com/v1/")
    assert p1.chat_url == "https://api.deepseek.com/chat/completions"
    assert p2.chat_url == "https://api.openai.com/v1/chat/completions"


# --------------------------------------------------------------------------- #
# 响应解析与错误分类
# --------------------------------------------------------------------------- #
def test_complete_success(monkeypatch):
    install_post(monkeypatch, lambda *a, **k: FakeResp(200, ok_payload("分析完毕")))
    provider = OpenAICompatProvider("deepseek", model="deepseek-flash", api_key="k",
                                    base_url="https://api.deepseek.com")
    assert provider.complete(image=b"x", prompt="hi") == "分析完毕"


@pytest.mark.parametrize("status,body,kind", [
    (401, {"error": {"message": "Authentication Fails, Your api key: ****0000 is invalid"}},
     "auth_failed"),
    (429, {"error": {"message": "rate limit exceeded"}}, "rate_limited"),
    (404, {"error": {"message": "not found"}}, "model_not_found"),
    (400, {"error": {"message": "The supported API model names are deepseek-flash, "
                                "deepseek-v4-pro, but you passed foo"}}, "model_not_found"),
    (400, {"error": {"message": "image_url is not supported by this model"}},
     "vision_unsupported"),
    (500, {"error": {"message": "internal server error"}}, "unreachable"),
])
def test_http_error_classification(monkeypatch, status, body, kind):
    install_post(monkeypatch, lambda *a, **k: FakeResp(status, body))
    provider = OpenAICompatProvider("deepseek", model="deepseek-flash", api_key="k",
                                    base_url="https://api.deepseek.com")
    with pytest.raises(AIError) as ei:
        provider.complete(prompt="hi")
    assert ei.value.kind == kind


def test_timeout_is_unreachable(monkeypatch):
    def boom(*a, **k):
        raise pv.httpx.TimeoutException("timed out")

    install_post(monkeypatch, boom)
    provider = OpenAICompatProvider("deepseek", model="deepseek-flash", api_key="k",
                                    base_url="https://api.deepseek.com")
    with pytest.raises(AIError) as ei:
        provider.complete(prompt="hi")
    assert ei.value.kind == "unreachable"


def test_missing_choices_is_bad_response(monkeypatch):
    install_post(monkeypatch, lambda *a, **k: FakeResp(200, {"id": "x"}))
    provider = OpenAICompatProvider("deepseek", model="deepseek-flash", api_key="k",
                                    base_url="https://api.deepseek.com")
    with pytest.raises(AIError) as ei:
        provider.complete(prompt="hi")
    assert ei.value.kind == "bad_response"


def test_invalid_json_is_bad_response(monkeypatch):
    install_post(monkeypatch, lambda *a, **k: FakeResp(200, ValueError("not json"), text="<html>"))
    provider = OpenAICompatProvider("deepseek", model="deepseek-flash", api_key="k",
                                    base_url="https://api.deepseek.com")
    with pytest.raises(AIError) as ei:
        provider.complete(prompt="hi")
    assert ei.value.kind == "bad_response"


def test_truncated_output_is_reported_as_truncated(monkeypatch):
    """思考模式下 max_tokens 太小：content 空、finish_reason=length，必须说清原因。"""
    payload = {"choices": [{"index": 0, "finish_reason": "length",
                            "message": {"role": "assistant", "content": "",
                                        "reasoning_content": "让我想想这张图…"}}]}
    install_post(monkeypatch, lambda *a, **k: FakeResp(200, payload))
    provider = OpenAICompatProvider("deepseek", model="deepseek-flash", api_key="k",
                                    base_url="https://api.deepseek.com")
    with pytest.raises(AIError) as ei:
        provider.complete(prompt="hi")
    assert ei.value.kind == "truncated"


def test_empty_content_is_empty_response(monkeypatch):
    payload = {"choices": [{"index": 0, "finish_reason": "stop",
                            "message": {"role": "assistant", "content": "   "}}]}
    install_post(monkeypatch, lambda *a, **k: FakeResp(200, payload))
    provider = OpenAICompatProvider("deepseek", model="deepseek-flash", api_key="k",
                                    base_url="https://api.deepseek.com")
    with pytest.raises(AIError) as ei:
        provider.complete(prompt="hi")
    assert ei.value.kind == "empty_response"


# --------------------------------------------------------------------------- #
# Anthropic provider
# --------------------------------------------------------------------------- #
def test_anthropic_provider_builds_messages_request(monkeypatch):
    import anthropic

    captured: Dict[str, Any] = {}

    class FakeMessages:
        def create(self, **kwargs):
            captured.update(kwargs)

            class Block:
                type = "text"
                text = "## ok\n\n综合健康评分：5 / 5"

            class Resp:
                content = [Block()]

            return Resp()

    class FakeClient:
        def __init__(self, **kwargs):
            captured["_client_kwargs"] = kwargs
            self.messages = FakeMessages()

    monkeypatch.setattr(anthropic, "Anthropic", FakeClient)

    provider = AnthropicProvider("anthropic", model="claude-opus-4-7", api_key="sk-test",
                                 base_url="https://api.anthropic.com", supports_vision=True)
    out = provider.complete(image=b"\xff\xd8", prompt="看图", system="sys", max_tokens=200)

    assert "ok" in out
    assert captured["model"] == "claude-opus-4-7"
    assert captured["max_tokens"] == 200
    assert captured["system"] == "sys"
    content = captured["messages"][0]["content"]
    assert content[0]["type"] == "image"
    assert content[0]["source"]["media_type"] == "image/jpeg"
    assert content[1]["type"] == "text"
    # 默认 base_url 不应被显式传给 SDK
    assert "base_url" not in captured["_client_kwargs"]


def test_anthropic_provider_passes_custom_base_url(monkeypatch):
    import anthropic

    captured: Dict[str, Any] = {}

    class FakeMessages:
        def create(self, **kwargs):
            captured.update(kwargs)

            class Block:
                type = "text"
                text = "ok"

            class Resp:
                content = [Block()]

            return Resp()

    class FakeClient:
        def __init__(self, **kwargs):
            captured["_client_kwargs"] = kwargs
            self.messages = FakeMessages()

    monkeypatch.setattr(anthropic, "Anthropic", FakeClient)
    provider = AnthropicProvider("deepseek-anthropic", model="deepseek-flash", api_key="k",
                                 base_url="https://api.deepseek.com/anthropic")
    provider.complete(prompt="hi")
    assert captured["_client_kwargs"]["base_url"] == "https://api.deepseek.com/anthropic"


if __name__ == "__main__":
    _bootstrap.run(__file__)
