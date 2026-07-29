"""The LLM client is provider-abstracted and config-driven.

These prove get_llm_client() builds the client Settings asks for (Claude or
Ollama), AnthropicLLMClient and OllamaLLMClient both talk to an injected
transport so tests never hit the network, and as_model_call() produces
something run_model_boundary can call directly.
"""

from __future__ import annotations

import json

import anthropic
import httpx
import pytest

from app.config import Settings
from app.privacy.boundary import run_model_boundary
from app.privacy.llm_client import (
    AnthropicLLMClient,
    LLMClientError,
    OllamaLLMClient,
    as_model_call,
    get_llm_client,
)

ALLOWLISTED = {
    "archetype": "One-and-done",
    "recency_bucket": "Exited 3y plus",
    "value_tier_label": "High",
    "rhythm_band": "Unknown",
}


class FakeTextBlock:
    def __init__(self, text: str) -> None:
        self.type = "text"
        self.text = text


class FakeResponse:
    def __init__(self, text: str, stop_reason: str = "end_turn") -> None:
        self.content = [FakeTextBlock(text)]
        self.stop_reason = stop_reason


class FakeMessages:
    def __init__(self, response: FakeResponse | Exception) -> None:
        self._response = response
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


class FakeAnthropic:
    def __init__(self, response: FakeResponse | Exception) -> None:
        self.messages = FakeMessages(response)


def make_settings(**overrides) -> Settings:
    defaults = {
        "llm_provider": "anthropic",
        "anthropic_api_key": "test-key",
        "llm_model": "claude-opus-5",
        "llm_temperature": None,
        "llm_max_tokens": 1024,
    }
    defaults.update(overrides)
    return Settings(**defaults)


def test_get_llm_client_reads_model_choice_from_config_not_hardcoded() -> None:
    settings = make_settings(llm_model="claude-sonnet-5", llm_max_tokens=256, llm_temperature=0.4)
    client = get_llm_client(settings)
    assert isinstance(client, AnthropicLLMClient)
    assert client.model == "claude-sonnet-5"
    assert client.max_tokens == 256
    assert client.temperature == 0.4


def test_get_llm_client_rejects_unknown_provider() -> None:
    settings = make_settings(llm_provider="not-a-real-provider")
    with pytest.raises(ValueError, match="not-a-real-provider"):
        get_llm_client(settings)


def test_generate_omits_temperature_when_not_configured() -> None:
    fake = FakeAnthropic(FakeResponse("Dear {{first_name}}"))
    client = AnthropicLLMClient(
        api_key="k", model="claude-opus-5", max_tokens=100, temperature=None, client=fake
    )
    client.generate(system="draft an email", user="archetype: One-and-done")
    assert "temperature" not in fake.messages.calls[0]


def test_generate_passes_temperature_when_configured() -> None:
    fake = FakeAnthropic(FakeResponse("Dear {{first_name}}"))
    client = AnthropicLLMClient(
        api_key="k", model="claude-opus-5", max_tokens=100, temperature=0.7, client=fake
    )
    client.generate(system="draft an email", user="archetype: One-and-done")
    assert fake.messages.calls[0]["temperature"] == 0.7


def test_generate_uses_configured_model_and_max_tokens() -> None:
    fake = FakeAnthropic(FakeResponse("draft"))
    client = AnthropicLLMClient(api_key="k", model="claude-sonnet-5", max_tokens=42, client=fake)
    client.generate(system="s", user="u")
    call = fake.messages.calls[0]
    assert call["model"] == "claude-sonnet-5"
    assert call["max_tokens"] == 42


def test_generate_returns_the_text_content() -> None:
    fake = FakeAnthropic(FakeResponse("Dear {{first_name}}, come back."))
    client = AnthropicLLMClient(api_key="k", model="claude-opus-5", max_tokens=100, client=fake)
    assert client.generate(system="s", user="u") == "Dear {{first_name}}, come back."


def test_generate_raises_llm_client_error_on_refusal() -> None:
    fake = FakeAnthropic(FakeResponse("", stop_reason="refusal"))
    client = AnthropicLLMClient(api_key="k", model="claude-opus-5", max_tokens=100, client=fake)
    with pytest.raises(LLMClientError, match="declined"):
        client.generate(system="s", user="u")


def test_generate_wraps_sdk_errors() -> None:
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    error = anthropic.APIConnectionError(message="boom", request=request)
    fake = FakeAnthropic(error)
    client = AnthropicLLMClient(api_key="k", model="claude-opus-5", max_tokens=100, client=fake)
    with pytest.raises(LLMClientError, match="boom"):
        client.generate(system="s", user="u")


def test_get_llm_client_builds_ollama_from_settings() -> None:
    settings = make_settings(
        llm_provider="ollama",
        llm_model="phi4-mini",
        llm_max_tokens=512,
        llm_temperature=0.2,
    )
    client = get_llm_client(settings)
    assert isinstance(client, OllamaLLMClient)
    assert client.model == "phi4-mini"
    assert client.max_tokens == 512
    assert client.temperature == 0.2


def _ollama_client(handler, **overrides) -> OllamaLLMClient:
    transport = httpx.MockTransport(handler)
    defaults = {
        "model": "phi4-mini",
        "max_tokens": 256,
        "client": httpx.Client(transport=transport, base_url="http://localhost:11434"),
    }
    defaults.update(overrides)
    return OllamaLLMClient(**defaults)


def test_ollama_generate_posts_the_configured_model_and_messages() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"message": {"content": "Dear {{first_name}}"}})

    client = _ollama_client(handler)
    result = client.generate(system="draft an email", user="archetype: One-and-done")

    assert result == "Dear {{first_name}}"
    assert seen["url"] == "http://localhost:11434/api/chat"
    assert seen["body"]["model"] == "phi4-mini"
    assert seen["body"]["messages"] == [
        {"role": "system", "content": "draft an email"},
        {"role": "user", "content": "archetype: One-and-done"},
    ]
    assert seen["body"]["options"]["num_predict"] == 256
    assert "temperature" not in seen["body"]["options"]


def test_ollama_generate_passes_temperature_when_configured() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"message": {"content": "draft"}})

    client = _ollama_client(handler, temperature=0.3)
    client.generate(system="s", user="u")

    assert seen["body"]["options"]["temperature"] == 0.3


def test_ollama_generate_requests_json_mode() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"message": {"content": "{}"}})

    client = _ollama_client(handler)
    client.generate(system="s", user="u")

    assert seen["body"]["format"] == "json"
    assert seen["body"]["stream"] is False


def test_ollama_generate_wraps_transport_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    client = _ollama_client(handler)
    with pytest.raises(LLMClientError, match="connection refused"):
        client.generate(system="s", user="u")


def test_ollama_generate_wraps_a_non_2xx_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "model not found"})

    client = _ollama_client(handler)
    with pytest.raises(LLMClientError):
        client.generate(system="s", user="u")


class StubLLMClient:
    """A minimal LLMClient double for exercising as_model_call()."""

    model = "stub"

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def generate(self, *, system: str, user: str) -> str:
        self.calls.append({"system": system, "user": user})
        return "Dear {{first_name}}, come back."


def test_as_model_call_is_usable_directly_by_run_model_boundary() -> None:
    stub = StubLLMClient()
    model_call = as_model_call(stub, system="draft a win-back email")

    draft = run_model_boundary(ALLOWLISTED, model_call)

    assert draft == "Dear {{first_name}}, come back."
    assert stub.calls[0]["system"] == "draft a win-back email"
    for key, value in ALLOWLISTED.items():
        assert f"{key}: {value}" in stub.calls[0]["user"]
