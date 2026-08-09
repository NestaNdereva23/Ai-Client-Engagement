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
    build_batch_request,
    get_anthropic_batch_client,
    get_judge_llm_client,
    get_llm_client,
    resolve_judge_model_config,
)

ALLOWLISTED = {
    "recency_band": "Over 6y",
    "value_band": "High",
    "cadence_band": "Regular",
    "hold_band": "Unknown",
}


class FakeTextBlock:
    def __init__(self, text: str) -> None:
        self.type = "text"
        self.text = text


class FakeUsage:
    def __init__(self, input_tokens: int = 10, output_tokens: int = 20) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class FakeResponse:
    def __init__(
        self, text: str, stop_reason: str = "end_turn", usage: FakeUsage | None = None
    ) -> None:
        self.content = [FakeTextBlock(text)]
        self.stop_reason = stop_reason
        self.usage = usage or FakeUsage()


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
        "judge_llm_provider": "",
        "judge_llm_model": "",
        "judge_llm_temperature": None,
        "judge_llm_max_tokens": 512,
        "ollama_timeout_seconds": 120.0,
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


def test_generate_sets_last_usage_from_the_response() -> None:
    fake = FakeAnthropic(FakeResponse("draft", usage=FakeUsage(input_tokens=42, output_tokens=7)))
    client = AnthropicLLMClient(api_key="k", model="claude-opus-5", max_tokens=100, client=fake)
    assert client.last_usage is None
    client.generate(system="s", user="u")
    assert client.last_usage.input_tokens == 42
    assert client.last_usage.output_tokens == 7


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


def test_get_llm_client_builds_ollama_with_the_configured_timeout() -> None:
    settings = make_settings(
        llm_provider="ollama", llm_model="phi4-mini", ollama_timeout_seconds=600.0
    )
    client = get_llm_client(settings)
    assert client._client.timeout == httpx.Timeout(600.0)


def test_resolve_judge_model_config_falls_back_to_generation_when_unset() -> None:
    settings = make_settings(llm_provider="ollama", llm_model="phi4-mini")
    provider, model, temperature, max_tokens = resolve_judge_model_config(settings)
    assert (provider, model) == ("ollama", "phi4-mini")
    assert max_tokens == 512


def test_resolve_judge_model_config_uses_the_configured_judge_model() -> None:
    settings = make_settings(
        llm_provider="ollama",
        llm_model="phi4-mini",
        judge_llm_provider="ollama",
        judge_llm_model="qwen3.5",
        judge_llm_temperature=0.1,
        judge_llm_max_tokens=256,
    )
    provider, model, temperature, max_tokens = resolve_judge_model_config(settings)
    assert (provider, model, temperature, max_tokens) == ("ollama", "qwen3.5", 0.1, 256)


def test_get_judge_llm_client_falls_back_to_the_generation_client_when_unset() -> None:
    settings = make_settings(llm_provider="ollama", llm_model="phi4-mini")
    client = get_judge_llm_client(settings)
    assert isinstance(client, OllamaLLMClient)
    assert client.model == "phi4-mini"


def test_get_judge_llm_client_uses_a_distinct_judge_model_when_configured() -> None:
    settings = make_settings(
        llm_provider="ollama",
        llm_model="phi4-mini",
        judge_llm_model="qwen3.5",
    )
    generation_client = get_llm_client(settings)
    judge_client = get_judge_llm_client(settings)
    assert generation_client.model == "phi4-mini"
    assert judge_client.model == "qwen3.5"


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


def test_ollama_generate_sets_last_usage_from_the_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"message": {"content": "draft"}, "prompt_eval_count": 15, "eval_count": 30},
        )

    client = _ollama_client(handler)
    assert client.last_usage is None
    client.generate(system="s", user="u")
    assert client.last_usage.input_tokens == 15
    assert client.last_usage.output_tokens == 30


def test_ollama_generate_defaults_usage_when_the_response_omits_it() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"message": {"content": "draft"}})

    client = _ollama_client(handler)
    client.generate(system="s", user="u")
    assert client.last_usage.input_tokens == 0
    assert client.last_usage.output_tokens == 0


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


def test_ollama_generate_disables_thinking() -> None:
    """A thinking-capable model must answer directly, not spend the token
    budget reasoning in a separate field and never reach content."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"message": {"content": "{}"}})

    client = _ollama_client(handler)
    client.generate(system="s", user="u")

    assert seen["body"]["think"] is False


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


def test_get_anthropic_batch_client_refuses_a_non_anthropic_provider() -> None:
    settings = make_settings(llm_provider="ollama")
    with pytest.raises(ValueError, match="anthropic"):
        get_anthropic_batch_client(settings)


def test_build_batch_request_puts_the_cache_breakpoint_on_the_first_system_block() -> None:
    settings = make_settings()
    request = build_batch_request(
        custom_id="run-1",
        system_cached="shared instructions",
        system_dynamic="this client's own caveats",
        user="recency_band: Over 6y",
        settings=settings,
    )

    assert request["custom_id"] == "run-1"
    system = request["params"]["system"]
    assert len(system) == 2
    assert system[0] == {
        "type": "text",
        "text": "shared instructions",
        "cache_control": {"type": "ephemeral"},
    }
    assert system[1] == {"type": "text", "text": "this client's own caveats"}
    assert request["params"]["model"] == settings.llm_model
    assert request["params"]["max_tokens"] == settings.llm_max_tokens
    assert request["params"]["messages"] == [{"role": "user", "content": "recency_band: Over 6y"}]


def test_build_batch_request_omits_an_empty_dynamic_block() -> None:
    request = build_batch_request(
        custom_id="run-2",
        system_cached="shared instructions",
        system_dynamic="",
        user="recency_band: Over 6y",
        settings=make_settings(),
    )

    assert len(request["params"]["system"]) == 1


def test_build_batch_request_is_identical_across_clients_sharing_the_same_cached_half() -> None:
    """The whole point of the cache_control breakpoint: two different
    clients on the same angle, tier, and product must produce byte-for-byte
    the same first system block, or the provider has nothing to cache a hit
    against. Only the dynamic block and the user turn may differ.
    """
    settings = make_settings()
    first = build_batch_request(
        custom_id="run-a",
        system_cached="shared instructions",
        system_dynamic="client A's caveats",
        user="recency_band: Over 6y",
        settings=settings,
    )
    second = build_batch_request(
        custom_id="run-b",
        system_cached="shared instructions",
        system_dynamic="client B's caveats",
        user="recency_band: Under 1y",
        settings=settings,
    )

    assert first["params"]["system"][0] == second["params"]["system"][0]
    assert first["params"]["system"][1] != second["params"]["system"][1]
