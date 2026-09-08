"""The LLM client is provider-abstracted and config-driven.

These prove get_llm_client() builds the client Settings asks for (Claude,
Ollama, or a local llama.cpp server), and that every client talks to an
injected transport so tests never hit the network, and as_model_call() produces
something run_model_boundary can call directly.
"""

from __future__ import annotations

import json

import anthropic
import httpx
import pytest

from app.config import Settings
from app.privacy.boundary import run_conversation_boundary, run_model_boundary
from app.privacy.llm_client import (
    AnthropicLLMClient,
    LlamaCppLLMClient,
    LLMClientError,
    OllamaLLMClient,
    ToolSpec,
    ToolUseRequest,
    as_converse_call,
    as_model_call,
    build_batch_request,
    get_agent_llm_client,
    get_anthropic_batch_client,
    get_briefing_llm_client,
    get_judge_llm_client,
    get_llm_client,
    resolve_agent_model_config,
    resolve_briefing_model_config,
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


class FakeMessages:
    def __init__(self, response: FakeResponse | Exception) -> None:
        self._response = response
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


class FakeSequentialMessages:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self._responses.pop(0)


class FakeAnthropic:
    def __init__(self, response: FakeResponse | Exception | list[FakeResponse]) -> None:
        if isinstance(response, list):
            self.messages: FakeMessages | FakeSequentialMessages = FakeSequentialMessages(response)
        else:
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
        "briefing_llm_provider": "",
        "briefing_llm_model": "",
        "briefing_llm_temperature": None,
        "briefing_llm_max_tokens": 1024,
        "agent_llm_provider": "",
        "agent_llm_model": "",
        "agent_llm_temperature": None,
        "agent_llm_max_tokens": 2048,
        "ollama_timeout_seconds": 120.0,
        "llamacpp_timeout_seconds": 120.0,
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


def test_converse_sends_the_message_history_and_declared_tools() -> None:
    fake = FakeAnthropic(FakeResponse("Dear {{first_name}}, come back."))
    client = AnthropicLLMClient(api_key="k", model="claude-opus-5", max_tokens=100, client=fake)
    tools = [
        ToolSpec(
            name="list_groups",
            description="List tonight's groups.",
            input_schema={"type": "object"},
        )
    ]
    messages = [{"role": "user", "content": "what needs attention tonight?"}]

    turn = client.converse(system="you are the agent", messages=messages, tools=tools)

    call = fake.messages.calls[0]
    assert call["messages"] == messages
    assert call["tools"] == [
        {
            "name": "list_groups",
            "description": "List tonight's groups.",
            "input_schema": {"type": "object"},
        }
    ]
    assert turn.text == "Dear {{first_name}}, come back."
    assert turn.tool_requests == ()
    assert turn.stop_reason == "end_turn"


def test_converse_omits_tools_from_the_request_when_none_are_given() -> None:
    fake = FakeAnthropic(FakeResponse("answer"))
    client = AnthropicLLMClient(api_key="k", model="claude-opus-5", max_tokens=100, client=fake)

    client.converse(system="s", messages=[{"role": "user", "content": "u"}], tools=[])

    assert "tools" not in fake.messages.calls[0]


def test_converse_returns_the_tools_the_model_wants_to_call() -> None:
    tool_use = FakeToolUseBlock("call_1", "list_groups", {"date": "today"})
    fake = FakeAnthropic(FakeResponse("", stop_reason="tool_use", tool_uses=[tool_use]))
    client = AnthropicLLMClient(api_key="k", model="claude-opus-5", max_tokens=100, client=fake)

    turn = client.converse(system="s", messages=[{"role": "user", "content": "u"}], tools=[])

    assert turn.stop_reason == "tool_use"
    assert turn.tool_requests == (
        ToolUseRequest(call_id="call_1", tool_name="list_groups", tool_input={"date": "today"}),
    )


def test_converse_sets_last_usage_the_same_way_generate_does() -> None:
    fake = FakeAnthropic(FakeResponse("answer", usage=FakeUsage(input_tokens=11, output_tokens=4)))
    client = AnthropicLLMClient(api_key="k", model="claude-opus-5", max_tokens=100, client=fake)

    turn = client.converse(system="s", messages=[{"role": "user", "content": "u"}], tools=[])

    assert turn.usage.input_tokens == 11
    assert turn.usage.output_tokens == 4
    assert client.last_usage == turn.usage


def test_converse_raises_llm_client_error_on_refusal() -> None:
    fake = FakeAnthropic(FakeResponse("", stop_reason="refusal"))
    client = AnthropicLLMClient(api_key="k", model="claude-opus-5", max_tokens=100, client=fake)
    with pytest.raises(LLMClientError, match="declined"):
        client.converse(system="s", messages=[{"role": "user", "content": "u"}], tools=[])


def test_run_conversation_returns_a_final_answer_when_no_tool_is_needed() -> None:
    fake = FakeAnthropic(FakeResponse("no action needed tonight"))
    client = AnthropicLLMClient(api_key="k", model="claude-opus-5", max_tokens=100, client=fake)
    tools = [ToolSpec(name="list_groups", description="lists groups", input_schema={})]

    result = client.run_conversation(
        system="s", user="anything to do tonight?", tools=tools, call_tool=lambda name, args: {}
    )

    assert result.stopped_reason == "final_answer"
    assert result.final_text == "no action needed tonight"
    assert len(result.turns) == 1
    assert len(fake.messages.calls) == 1


def test_run_conversation_calls_a_tool_then_returns_the_final_answer() -> None:
    tool_use = FakeToolUseBlock("call_1", "list_groups", {"date": "today"})
    fake = FakeAnthropic(
        [
            FakeResponse("", stop_reason="tool_use", tool_uses=[tool_use]),
            FakeResponse("three groups need attention"),
        ]
    )
    client = AnthropicLLMClient(api_key="k", model="claude-opus-5", max_tokens=100, client=fake)
    tool_calls: list[tuple[str, dict]] = []

    def call_tool(name: str, tool_input: dict) -> dict:
        tool_calls.append((name, tool_input))
        return {"groups": 3}

    tools = [ToolSpec(name="list_groups", description="lists groups", input_schema={})]
    result = client.run_conversation(
        system="s", user="what needs attention tonight?", tools=tools, call_tool=call_tool
    )

    assert tool_calls == [("list_groups", {"date": "today"})]
    assert result.stopped_reason == "final_answer"
    assert result.final_text == "three groups need attention"
    assert len(result.turns) == 2

    second_request = fake.messages.calls[1]
    assert second_request["messages"][1] == {
        "role": "assistant",
        "content": [
            {"type": "tool_use", "id": "call_1", "name": "list_groups", "input": {"date": "today"}}
        ],
    }
    assert second_request["messages"][2] == {
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": "call_1", "content": '{"groups": 3}'}],
    }


def test_run_conversation_handles_several_tool_calls_before_the_final_answer() -> None:
    first_tool = FakeToolUseBlock("call_1", "list_groups", {})
    second_tool = FakeToolUseBlock("call_2", "describe_group", {"group": "fees_will_empty"})
    fake = FakeAnthropic(
        [
            FakeResponse("", stop_reason="tool_use", tool_uses=[first_tool]),
            FakeResponse("", stop_reason="tool_use", tool_uses=[second_tool]),
            FakeResponse("send the fee warning"),
        ]
    )
    client = AnthropicLLMClient(api_key="k", model="claude-opus-5", max_tokens=100, client=fake)
    seen_tools: list[str] = []

    def call_tool(name: str, tool_input: dict) -> dict:
        seen_tools.append(name)
        return {"ok": True}

    tools = [
        ToolSpec(name="list_groups", description="lists groups", input_schema={}),
        ToolSpec(name="describe_group", description="describes a group", input_schema={}),
    ]
    result = client.run_conversation(system="s", user="u", tools=tools, call_tool=call_tool)

    assert seen_tools == ["list_groups", "describe_group"]
    assert result.stopped_reason == "final_answer"
    assert result.final_text == "send the fee warning"
    assert len(result.turns) == 3


def test_run_conversation_calls_every_tool_requested_in_one_turn() -> None:
    parallel_tools = [
        FakeToolUseBlock("call_1", "list_groups", {}),
        FakeToolUseBlock("call_2", "check_allowance", {}),
    ]
    fake = FakeAnthropic(
        [
            FakeResponse("", stop_reason="tool_use", tool_uses=parallel_tools),
            FakeResponse("here is what tonight looks like"),
        ]
    )
    client = AnthropicLLMClient(api_key="k", model="claude-opus-5", max_tokens=100, client=fake)
    seen_tools: list[str] = []

    def call_tool(name: str, tool_input: dict) -> dict:
        seen_tools.append(name)
        return {"ok": True}

    tools = [
        ToolSpec(name="list_groups", description="lists groups", input_schema={}),
        ToolSpec(name="check_allowance", description="checks the daily allowance", input_schema={}),
    ]
    result = client.run_conversation(system="s", user="u", tools=tools, call_tool=call_tool)

    assert seen_tools == ["list_groups", "check_allowance"]
    assert len(result.turns) == 2
    tool_result_message = fake.messages.calls[1]["messages"][2]
    tool_use_ids = [block["tool_use_id"] for block in tool_result_message["content"]]
    assert tool_use_ids == ["call_1", "call_2"]


def test_run_conversation_stops_at_the_turn_cap_without_calling_the_last_requested_tool() -> None:
    always_wants_a_tool = [
        FakeResponse(
            "", stop_reason="tool_use", tool_uses=[FakeToolUseBlock(f"call_{n}", "list_groups", {})]
        )
        for n in range(3)
    ]
    fake = FakeAnthropic(always_wants_a_tool)
    client = AnthropicLLMClient(api_key="k", model="claude-opus-5", max_tokens=100, client=fake)
    tool_calls: list[str] = []

    def call_tool(name: str, tool_input: dict) -> dict:
        tool_calls.append(name)
        return {}

    tools = [ToolSpec(name="list_groups", description="lists groups", input_schema={})]
    result = client.run_conversation(
        system="s", user="u", tools=tools, call_tool=call_tool, max_turns=3
    )

    assert result.stopped_reason == "turn_limit"
    assert result.final_text is None
    assert len(result.turns) == 3
    assert len(tool_calls) == 2
    assert len(fake.messages.calls) == 3


def test_run_conversation_uses_the_default_turn_cap_when_none_is_given() -> None:
    tool_use = FakeToolUseBlock("call_1", "list_groups", {})
    fake = FakeAnthropic(
        [FakeResponse("", stop_reason="tool_use", tool_uses=[tool_use]), FakeResponse("done")]
    )
    client = AnthropicLLMClient(api_key="k", model="claude-opus-5", max_tokens=100, client=fake)
    tools = [ToolSpec(name="list_groups", description="lists groups", input_schema={})]

    result = client.run_conversation(
        system="s", user="u", tools=tools, call_tool=lambda name, tool_input: {}
    )

    assert result.stopped_reason == "final_answer"
    assert result.final_text == "done"


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


def test_resolve_briefing_model_config_falls_back_to_generation_when_unset() -> None:
    settings = make_settings(llm_provider="ollama", llm_model="phi4-mini")
    provider, model, temperature, max_tokens = resolve_briefing_model_config(settings)
    assert (provider, model) == ("ollama", "phi4-mini")
    assert max_tokens == 1024


def test_resolve_briefing_model_config_uses_the_configured_briefing_model() -> None:
    settings = make_settings(
        llm_provider="ollama",
        llm_model="phi4-mini",
        briefing_llm_provider="ollama",
        briefing_llm_model="qwen3.5",
        briefing_llm_temperature=0.1,
        briefing_llm_max_tokens=256,
    )
    provider, model, temperature, max_tokens = resolve_briefing_model_config(settings)
    assert (provider, model, temperature, max_tokens) == ("ollama", "qwen3.5", 0.1, 256)


def test_get_briefing_llm_client_falls_back_to_the_generation_client_when_unset() -> None:
    settings = make_settings(llm_provider="ollama", llm_model="phi4-mini")
    client = get_briefing_llm_client(settings)
    assert isinstance(client, OllamaLLMClient)
    assert client.model == "phi4-mini"
    # The narrator is the one caller that wants prose back, not a JSON object.
    assert client.json_output is False
    assert get_llm_client(settings).json_output is True
    assert get_judge_llm_client(settings).json_output is True


def test_get_briefing_llm_client_uses_a_distinct_briefing_model_when_configured() -> None:
    settings = make_settings(
        llm_provider="ollama",
        llm_model="phi4-mini",
        briefing_llm_model="qwen3.5",
    )
    generation_client = get_llm_client(settings)
    judge_client = get_judge_llm_client(settings)
    briefing_client = get_briefing_llm_client(settings)
    assert generation_client.model == "phi4-mini"
    assert judge_client.model == "phi4-mini"
    assert briefing_client.model == "qwen3.5"


def test_resolve_agent_model_config_falls_back_to_generation_when_unset() -> None:
    settings = make_settings(llm_provider="ollama", llm_model="phi4-mini")
    provider, model, temperature, max_tokens = resolve_agent_model_config(settings)
    assert (provider, model) == ("ollama", "phi4-mini")
    assert max_tokens == 2048


def test_resolve_agent_model_config_uses_the_configured_agent_model() -> None:
    settings = make_settings(
        llm_provider="ollama",
        llm_model="phi4-mini",
        agent_llm_provider="ollama",
        agent_llm_model="qwen3.5",
        agent_llm_temperature=0.1,
        agent_llm_max_tokens=4096,
    )
    provider, model, temperature, max_tokens = resolve_agent_model_config(settings)
    assert (provider, model, temperature, max_tokens) == ("ollama", "qwen3.5", 0.1, 4096)


def test_get_agent_llm_client_falls_back_to_the_generation_client_when_unset() -> None:
    settings = make_settings(llm_provider="ollama", llm_model="phi4-mini")
    client = get_agent_llm_client(settings)
    assert isinstance(client, OllamaLLMClient)
    assert client.model == "phi4-mini"


def test_get_agent_llm_client_works_with_any_configured_provider() -> None:
    """The agent loop holds a tool calling conversation, but that is not a
    reason to lock it to one provider: Anthropic, Ollama and llama.cpp all
    hold one here.
    """
    assert isinstance(
        get_agent_llm_client(make_settings(llm_provider="anthropic")), AnthropicLLMClient
    )
    assert isinstance(get_agent_llm_client(make_settings(llm_provider="ollama")), OllamaLLMClient)
    assert isinstance(
        get_agent_llm_client(make_settings(llm_provider="llamacpp")), LlamaCppLLMClient
    )


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


def test_ollama_generate_asks_for_json_by_default_and_can_turn_it_off() -> None:
    """The email draft and the judge both parse a JSON object, so json_output
    stays on for them. The briefing narrator wants sentences: with the flag
    left on, Ollama returns a JSON object whatever the prompt says.
    """
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"message": {"content": "text"}})

    _ollama_client(handler).generate(system="s", user="u")
    assert seen["body"]["format"] == "json"

    _ollama_client(handler, json_output=False).generate(system="s", user="u")
    assert "format" not in seen["body"]


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


def test_ollama_converse_sends_the_translated_messages_and_tools() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"message": {"content": "three groups need attention"}})

    client = _ollama_client(handler)
    tools = [ToolSpec(name="list_groups", description="lists tonight's groups", input_schema={})]
    messages = [{"role": "user", "content": "what needs attention tonight?"}]

    turn = client.converse(system="you are the agent", messages=messages, tools=tools)

    assert turn.text == "three groups need attention"
    assert turn.tool_requests == ()
    assert turn.stop_reason == "end_turn"
    assert seen["body"]["messages"] == [
        {"role": "system", "content": "you are the agent"},
        {"role": "user", "content": "what needs attention tonight?"},
    ]
    assert seen["body"]["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "list_groups",
                "description": "lists tonight's groups",
                "parameters": {},
            },
        }
    ]


def test_ollama_converse_returns_the_tool_calls_the_model_wants_to_make() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "message": {
                    "content": "",
                    "tool_calls": [
                        {"function": {"name": "list_groups", "arguments": {"as_of": "today"}}}
                    ],
                }
            },
        )

    client = _ollama_client(handler)
    turn = client.converse(system="s", messages=[{"role": "user", "content": "u"}])

    assert turn.stop_reason == "tool_use"
    assert turn.tool_requests == (
        ToolUseRequest(call_id="0", tool_name="list_groups", tool_input={"as_of": "today"}),
    )


def test_ollama_converse_translates_a_tool_use_reply_and_its_result_back_into_the_next_turn() -> (
    None
):
    seen_bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen_bodies.append(body)
        if len(seen_bodies) == 1:
            return httpx.Response(
                200,
                json={
                    "message": {
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "function": {"name": "list_groups", "arguments": {}},
                            }
                        ],
                    }
                },
            )
        return httpx.Response(200, json={"message": {"content": "three groups need attention"}})

    client = _ollama_client(handler)
    tools = [ToolSpec(name="list_groups", description="lists groups", input_schema={})]

    def call_tool(name: str, tool_input: dict) -> dict:
        return {"groups": 3}

    result = run_conversation_boundary(
        {},
        as_converse_call(client, system="s", tools=tools),
        call_tool,
        max_turns=4,
    )

    assert result.stopped_reason == "final_answer"
    assert result.final_text == "three groups need attention"
    second_request_messages = seen_bodies[1]["messages"]
    assert second_request_messages[2] == {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "list_groups", "arguments": "{}"},
            }
        ],
    }
    assert second_request_messages[3] == {
        "role": "tool",
        "tool_call_id": "call_1",
        "content": '{"groups": 3}',
    }


def _llamacpp_client(handler, **overrides) -> LlamaCppLLMClient:
    transport = httpx.MockTransport(handler)
    defaults = {
        "model": "local-model",
        "max_tokens": 256,
        "client": httpx.Client(transport=transport, base_url="http://localhost:8080"),
    }
    defaults.update(overrides)
    return LlamaCppLLMClient(**defaults)


def _llamacpp_reply(text: str, **extra) -> dict:
    reply = {"choices": [{"message": {"content": text}}]}
    reply.update(extra)
    return reply


def test_get_llm_client_builds_llamacpp_from_settings() -> None:
    settings = make_settings(
        llm_provider="llamacpp",
        llm_model="local-model",
        llm_max_tokens=256,
        llamacpp_base_url="http://localhost:9090",
        llamacpp_timeout_seconds=600.0,
    )
    client = get_llm_client(settings)

    assert isinstance(client, LlamaCppLLMClient)
    assert client.model == "local-model"
    assert client.max_tokens == 256
    assert str(client._client.base_url) == "http://localhost:9090"
    assert client._client.timeout.read == 600.0


def test_get_judge_llm_client_builds_llamacpp_from_settings() -> None:
    settings = make_settings(llm_provider="llamacpp", llm_model="local-model")
    assert isinstance(get_judge_llm_client(settings), LlamaCppLLMClient)


def test_get_briefing_llm_client_builds_llamacpp_without_json_mode() -> None:
    settings = make_settings(llm_provider="llamacpp", llm_model="local-model")
    client = get_briefing_llm_client(settings)

    assert isinstance(client, LlamaCppLLMClient)
    assert client.json_output is False


def test_llamacpp_generate_posts_the_configured_model_and_messages() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=_llamacpp_reply("Dear {{first_name}}"))

    client = _llamacpp_client(handler)
    text = client.generate(system="be brief", user="recency_band: Over 6y")

    assert text == "Dear {{first_name}}"
    assert seen["url"] == "http://localhost:8080/v1/chat/completions"
    assert seen["body"]["model"] == "local-model"
    assert seen["body"]["max_tokens"] == 256
    assert seen["body"]["stream"] is False
    assert seen["body"]["messages"] == [
        {"role": "system", "content": "be brief"},
        {"role": "user", "content": "recency_band: Over 6y"},
    ]


def test_llamacpp_generate_asks_for_json_by_default_and_can_turn_it_off() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=_llamacpp_reply("{}"))

    _llamacpp_client(handler).generate(system="s", user="u")
    assert seen["body"]["response_format"] == {"type": "json_object"}

    _llamacpp_client(handler, json_output=False).generate(system="s", user="u")
    assert "response_format" not in seen["body"]


def test_llamacpp_generate_passes_temperature_only_when_configured() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=_llamacpp_reply("ok"))

    _llamacpp_client(handler, temperature=0.3).generate(system="s", user="u")
    assert seen["body"]["temperature"] == 0.3

    _llamacpp_client(handler).generate(system="s", user="u")
    assert "temperature" not in seen["body"]


def test_llamacpp_generate_sets_last_usage_from_the_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        usage = {"prompt_tokens": 31, "completion_tokens": 12}
        return httpx.Response(200, json=_llamacpp_reply("ok", usage=usage))

    client = _llamacpp_client(handler)
    client.generate(system="s", user="u")

    assert client.last_usage.input_tokens == 31
    assert client.last_usage.output_tokens == 12


def test_llamacpp_generate_defaults_usage_when_the_response_omits_it() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_llamacpp_reply("ok"))

    client = _llamacpp_client(handler)
    client.generate(system="s", user="u")

    assert client.last_usage.input_tokens == 0
    assert client.last_usage.output_tokens == 0


def test_llamacpp_generate_wraps_transport_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("server is down")

    client = _llamacpp_client(handler)
    with pytest.raises(LLMClientError, match="model request failed"):
        client.generate(system="s", user="u")


def test_llamacpp_generate_wraps_a_non_2xx_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    client = _llamacpp_client(handler)
    with pytest.raises(LLMClientError, match="model request failed"):
        client.generate(system="s", user="u")


def test_llamacpp_generate_fails_when_the_response_has_no_choices() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": []})

    client = _llamacpp_client(handler)
    with pytest.raises(LLMClientError, match="no choices"):
        client.generate(system="s", user="u")


def test_llamacpp_converse_sends_the_translated_messages_and_tools() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=_llamacpp_reply("three groups need attention"))

    client = _llamacpp_client(handler)
    tools = [ToolSpec(name="list_groups", description="lists tonight's groups", input_schema={})]
    messages = [{"role": "user", "content": "what needs attention tonight?"}]

    turn = client.converse(system="you are the agent", messages=messages, tools=tools)

    assert turn.text == "three groups need attention"
    assert turn.tool_requests == ()
    assert turn.stop_reason == "end_turn"
    assert seen["body"]["messages"] == [
        {"role": "system", "content": "you are the agent"},
        {"role": "user", "content": "what needs attention tonight?"},
    ]
    assert seen["body"]["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "list_groups",
                "description": "lists tonight's groups",
                "parameters": {},
            },
        }
    ]


def test_llamacpp_converse_returns_the_tool_calls_the_model_wants_to_make() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_llamacpp_reply(
                "",
                choices=[
                    {
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "function": {
                                        "name": "check_allowance",
                                        "arguments": '{"action_code": "fee_warning"}',
                                    },
                                }
                            ],
                        }
                    }
                ],
            ),
        )

    client = _llamacpp_client(handler)
    turn = client.converse(system="s", messages=[{"role": "user", "content": "u"}])

    assert turn.stop_reason == "tool_use"
    assert turn.tool_requests == (
        ToolUseRequest(
            call_id="call_1",
            tool_name="check_allowance",
            tool_input={"action_code": "fee_warning"},
        ),
    )


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
