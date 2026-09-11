"""Every provider has an async twin of generate and converse.

These prove the async call builds the same request, reads back the same text,
tool calls and token counts as the blocking one, and that a whole tool calling
conversation run through the async boundary lands on the same answer.
"""

from __future__ import annotations

import json
from typing import Any

import anthropic
import httpx
import pytest

from app.privacy.boundary import run_conversation_boundary, run_conversation_boundary_async
from app.privacy.llm_client import (
    AnthropicLLMClient,
    LlamaCppLLMClient,
    LLMClientError,
    OllamaLLMClient,
    ToolSpec,
    as_async_converse_call,
    as_converse_call,
)

ALLOWLISTED = {
    "recency_band": "Over 6y",
    "value_band": "High",
    "cadence_band": "Regular",
    "hold_band": "Unknown",
}

TOOLS = (
    ToolSpec(
        name="get_contact_history",
        description="what was sent to a group recently",
        input_schema={"type": "object", "properties": {}},
    ),
)


class FakeTextBlock:
    def __init__(self, text: str) -> None:
        self.type = "text"
        self.text = text


class FakeToolUseBlock:
    def __init__(self, call_id: str, name: str, tool_input: dict) -> None:
        self.type = "tool_use"
        self.id = call_id
        self.name = name
        self.input = tool_input


class FakeUsage:
    def __init__(self, input_tokens: int = 10, output_tokens: int = 20) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class FakeResponse:
    def __init__(
        self,
        text: str,
        stop_reason: str = "end_turn",
        usage: FakeUsage | None = None,
        tool_uses: list[FakeToolUseBlock] | None = None,
    ) -> None:
        self.content = [FakeTextBlock(text)] if text else []
        self.content.extend(tool_uses or [])
        self.stop_reason = stop_reason
        self.usage = usage or FakeUsage()


class FakeAsyncMessages:
    def __init__(self, responses: list[FakeResponse] | Exception) -> None:
        self._responses = responses
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self._responses, Exception):
            raise self._responses
        return self._responses.pop(0)


class FakeAsyncAnthropic:
    def __init__(self, responses: list[FakeResponse] | Exception) -> None:
        self.messages = FakeAsyncMessages(responses)


def _anthropic_client(responses, **overrides) -> AnthropicLLMClient:
    kwargs: dict[str, Any] = {
        "api_key": "k",
        "model": "claude-opus-5",
        "max_tokens": 100,
        "async_client": FakeAsyncAnthropic(responses),
    }
    kwargs.update(overrides)
    return AnthropicLLMClient(**kwargs)


async def test_anthropic_agenerate_returns_the_text_and_counts_the_tokens() -> None:
    client = _anthropic_client([FakeResponse("Dear {{first_name}}", usage=FakeUsage(11, 22))])

    text = await client.agenerate(system="draft an email", user="archetype: One-and-done")

    assert text == "Dear {{first_name}}"
    assert client.last_usage.input_tokens == 11
    assert client.last_usage.output_tokens == 22


async def test_anthropic_agenerate_sends_the_same_request_as_generate() -> None:
    async_fake = FakeAsyncAnthropic([FakeResponse("draft")])
    client = AnthropicLLMClient(
        api_key="k",
        model="claude-sonnet-5",
        max_tokens=42,
        temperature=0.4,
        async_client=async_fake,
    )

    await client.agenerate(system="s", user="u")

    call = async_fake.messages.calls[0]
    assert call == client._generate_kwargs("s", "u")
    assert call["model"] == "claude-sonnet-5"
    assert call["max_tokens"] == 42
    assert call["temperature"] == 0.4


async def test_anthropic_aconverse_returns_the_tool_the_model_asked_for() -> None:
    client = _anthropic_client(
        [
            FakeResponse(
                "",
                stop_reason="tool_use",
                tool_uses=[FakeToolUseBlock("call_1", "get_contact_history", {"group": "quiet"})],
            )
        ]
    )

    turn = await client.aconverse(system="plan", messages=[{"role": "user", "content": "Begin."}])

    assert turn.stop_reason == "tool_use"
    assert turn.tool_requests[0].tool_name == "get_contact_history"
    assert turn.tool_requests[0].tool_input == {"group": "quiet"}
    assert turn.usage.input_tokens == 10


async def test_anthropic_aconverse_raises_on_a_refusal_like_converse_does() -> None:
    client = _anthropic_client([FakeResponse("", stop_reason="refusal")])

    with pytest.raises(LLMClientError):
        await client.aconverse(system="plan", messages=[{"role": "user", "content": "Begin."}])


async def test_anthropic_agenerate_wraps_a_provider_error() -> None:
    error = anthropic.APIError("boom", request=httpx.Request("POST", "https://x"), body=None)
    client = _anthropic_client(error)

    with pytest.raises(LLMClientError, match="model request failed"):
        await client.agenerate(system="s", user="u")


async def test_anthropic_arun_conversation_calls_the_tool_and_returns_the_answer() -> None:
    client = _anthropic_client(
        [
            FakeResponse(
                "",
                stop_reason="tool_use",
                tool_uses=[FakeToolUseBlock("call_1", "get_contact_history", {})],
            ),
            FakeResponse("Two groups matter tonight."),
        ]
    )
    called: list[str] = []

    async def call_tool(name: str, tool_input: dict) -> dict:
        called.append(name)
        return {"sent_last_30_days": 2}

    result = await client.arun_conversation(
        system="plan", user="Begin.", tools=TOOLS, call_tool=call_tool
    )

    assert called == ["get_contact_history"]
    assert result.stopped_reason == "final_answer"
    assert result.final_text == "Two groups matter tonight."
    assert len(result.turns) == 2


def _ollama_reply(content: str, tool_calls: list[dict] | None = None) -> dict:
    message: dict[str, Any] = {"content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {"message": message, "prompt_eval_count": 7, "eval_count": 9}


def _llamacpp_reply(content: str, tool_calls: list[dict] | None = None) -> dict:
    message: dict[str, Any] = {"content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {
        "choices": [{"message": message}],
        "usage": {"prompt_tokens": 7, "completion_tokens": 9},
    }


def _async_transport(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url="http://localhost", transport=httpx.MockTransport(handler))


async def test_ollama_agenerate_matches_generate() -> None:
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json=_ollama_reply("a local draft"))

    client = OllamaLLMClient(model="qwen3", max_tokens=64, async_client=_async_transport(handler))

    text = await client.agenerate(system="s", user="u")

    assert text == "a local draft"
    assert client.last_usage.input_tokens == 7
    assert client.last_usage.output_tokens == 9
    assert bodies[0] == client._generate_body("s", "u")


async def test_ollama_aconverse_reads_back_a_tool_call() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_ollama_reply(
                "",
                [
                    {
                        "id": "call_1",
                        "function": {
                            "name": "get_contact_history",
                            "arguments": {"group": "quiet"},
                        },
                    }
                ],
            ),
        )

    client = OllamaLLMClient(model="qwen3", max_tokens=64, async_client=_async_transport(handler))

    turn = await client.aconverse(
        system="plan", messages=[{"role": "user", "content": "Begin."}], tools=TOOLS
    )

    assert turn.stop_reason == "tool_use"
    assert turn.tool_requests[0].tool_input == {"group": "quiet"}


async def test_ollama_agenerate_wraps_a_server_error() -> None:
    client = OllamaLLMClient(
        model="qwen3",
        max_tokens=64,
        async_client=_async_transport(lambda request: httpx.Response(500)),
    )

    with pytest.raises(LLMClientError, match="model request failed"):
        await client.agenerate(system="s", user="u")


async def test_llamacpp_agenerate_matches_generate() -> None:
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json=_llamacpp_reply("a local draft"))

    client = LlamaCppLLMClient(model="qwen3", max_tokens=64, async_client=_async_transport(handler))

    text = await client.agenerate(system="s", user="u")

    assert text == "a local draft"
    assert client.last_usage.input_tokens == 7
    assert bodies[0] == client._generate_body("s", "u")


async def test_llamacpp_aconverse_reads_back_a_tool_call() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_llamacpp_reply(
                "",
                [
                    {
                        "id": "call_1",
                        "function": {
                            "name": "get_contact_history",
                            "arguments": json.dumps({"group": "quiet"}),
                        },
                    }
                ],
            ),
        )

    client = LlamaCppLLMClient(model="qwen3", max_tokens=64, async_client=_async_transport(handler))

    turn = await client.aconverse(
        system="plan", messages=[{"role": "user", "content": "Begin."}], tools=TOOLS
    )

    assert turn.tool_requests[0].tool_input == {"group": "quiet"}
    assert turn.usage.output_tokens == 9


async def test_the_same_conversation_gives_the_same_result_on_both_paths() -> None:
    def responses() -> list[FakeResponse]:
        return [
            FakeResponse(
                "",
                stop_reason="tool_use",
                tool_uses=[FakeToolUseBlock("call_1", "get_contact_history", {})],
            ),
            FakeResponse("Two groups matter tonight."),
        ]

    class FakeMessages:
        def __init__(self, items: list[FakeResponse]) -> None:
            self._items = items
            self.calls: list[dict] = []

        def create(self, **kwargs):
            self.calls.append(kwargs)
            return self._items.pop(0)

    class FakeAnthropic:
        def __init__(self, items: list[FakeResponse]) -> None:
            self.messages = FakeMessages(items)

    blocking_fake = FakeAnthropic(responses())
    async_fake = FakeAsyncAnthropic(responses())
    blocking_client = AnthropicLLMClient(
        api_key="k", model="claude-opus-5", max_tokens=100, client=blocking_fake
    )
    async_client = AnthropicLLMClient(
        api_key="k", model="claude-opus-5", max_tokens=100, async_client=async_fake
    )

    def call_tool(name: str, tool_input: dict) -> dict:
        return {"sent_last_30_days": 2}

    async def acall_tool(name: str, tool_input: dict) -> dict:
        return call_tool(name, tool_input)

    blocking = run_conversation_boundary(
        ALLOWLISTED,
        as_converse_call(blocking_client, system="plan", tools=TOOLS),
        call_tool,
        max_turns=3,
    )
    awaited = await run_conversation_boundary_async(
        ALLOWLISTED,
        as_async_converse_call(async_client, system="plan", tools=TOOLS),
        acall_tool,
        max_turns=3,
    )

    assert awaited == blocking
    assert awaited.final_text == "Two groups matter tonight."
    assert async_fake.messages.calls == blocking_fake.messages.calls
    assert async_client.last_usage == blocking_client.last_usage
