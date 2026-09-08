from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable

import anthropic
import httpx
import structlog
from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
from anthropic.types.messages.batch_create_params import Request as BatchRequest

from app.config import Settings, get_settings
from app.privacy.boundary import (
    ConverseCall,
    ModelCall,
    assistant_content_blocks,
    render_model_context,
    tool_result_block,
)

logger = structlog.get_logger(__name__)

DEFAULT_MAX_TURNS = 8


class LLMClientError(RuntimeError):
    """Raised when a model call fails after the provider's own retries."""


@dataclass(frozen=True)
class LLMUsage:
    """Token counts for the most recent generate() call."""

    input_tokens: int
    output_tokens: int


@dataclass(frozen=True)
class ToolSpec:
    """One tool a model may call, declared the same way whether it reads or writes."""

    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True)
class ToolUseRequest:
    """One tool the model wants to call, with the id its result must reference."""

    call_id: str
    tool_name: str
    tool_input: dict[str, Any]


@dataclass(frozen=True)
class ConversationTurn:
    """One round trip with the model: its text, any tools it wants to call, and its usage."""

    text: str
    tool_requests: tuple[ToolUseRequest, ...]
    usage: LLMUsage
    stop_reason: str


@dataclass(frozen=True)
class ConversationResult:
    """How a whole tool calling conversation ended, and every turn it took to get there."""

    final_text: str | None
    turns: tuple[ConversationTurn, ...]
    stopped_reason: Literal["final_answer", "turn_limit"]


ToolExecutor = Callable[[str, dict[str, Any]], Any]


@runtime_checkable
class LLMClient(Protocol):
    """Thin interface a draft generation call goes through, any provider."""

    model: str

    def generate(self, *, system: str, user: str) -> str:
        """Return the model's reply text for one system/user turn."""
        ...


@runtime_checkable
class ConversingLLMClient(Protocol):
    """A model client that can hold a tool calling conversation, any provider."""

    model: str

    def converse(
        self, *, system: str, messages: list[dict[str, Any]], tools: Sequence[ToolSpec] = ()
    ) -> ConversationTurn:
        """Send one turn of a tool calling conversation, a final answer or tool requests."""
        ...


def _openai_tool_specs(tools: Sequence[ToolSpec]) -> list[dict[str, Any]]:
    """ToolSpec, in the function calling shape both Ollama and llama.cpp expect."""
    return [
        {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.input_schema,
            },
        }
        for tool in tools
    ]


def _openai_messages(system: str, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The Anthropic content block messages run_conversation_boundary builds,
    translated into the plain chat messages Ollama and llama.cpp expect.
    """
    translated: list[dict[str, Any]] = [{"role": "system", "content": system}]
    for message in messages:
        role = message["role"]
        content = message["content"]

        if isinstance(content, str):
            translated.append({"role": role, "content": content})
            continue

        if role == "assistant":
            text = "\n".join(block["text"] for block in content if block.get("type") == "text")
            tool_calls = [
                {
                    "id": block["id"],
                    "type": "function",
                    "function": {"name": block["name"], "arguments": json.dumps(block["input"])},
                }
                for block in content
                if block.get("type") == "tool_use"
            ]
            assistant_message: dict[str, Any] = {"role": "assistant", "content": text or None}
            if tool_calls:
                assistant_message["tool_calls"] = tool_calls
            translated.append(assistant_message)
            continue

        for block in content:
            if block.get("type") == "tool_result":
                translated.append(
                    {
                        "role": "tool",
                        "tool_call_id": block["tool_use_id"],
                        "content": block["content"],
                    }
                )
    return translated


def _tool_call_arguments(raw: Any) -> dict[str, Any]:
    """A tool call's arguments, whether the server sent them as a JSON string
    (llama.cpp, the OpenAI convention) or already parsed (Ollama).
    """
    if isinstance(raw, str):
        return json.loads(raw) if raw else {}
    return raw or {}


def _tool_requests_from_calls(calls: Sequence[Mapping[str, Any]]) -> tuple[ToolUseRequest, ...]:
    return tuple(
        ToolUseRequest(
            call_id=call.get("id") or str(index),
            tool_name=call["function"]["name"],
            tool_input=_tool_call_arguments(call["function"].get("arguments")),
        )
        for index, call in enumerate(calls)
    )


class AnthropicLLMClient:
    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        max_tokens: int,
        temperature: float | None = None,
        client: anthropic.Anthropic | None = None,
    ) -> None:
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.last_usage: LLMUsage | None = None
        self._client = client or anthropic.Anthropic(api_key=api_key)

    def generate(self, *, system: str, user: str) -> str:
        """Send one system/user turn to Claude and return the reply text."""
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        if self.temperature is not None:
            kwargs["temperature"] = self.temperature

        try:
            response = self._client.messages.create(**kwargs)
        except anthropic.APIError as exc:
            logger.warning("llm_client.request_failed", model=self.model, error=str(exc))
            raise LLMClientError(f"model request failed: {exc}") from exc

        if response.stop_reason == "refusal":
            raise LLMClientError("model declined the request")

        self.last_usage = LLMUsage(
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )
        text = "".join(block.text for block in response.content if block.type == "text")
        return text

    def converse(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: Sequence[ToolSpec] = (),
    ) -> ConversationTurn:
        """Send one turn of a conversation, returning a final answer or tool requests."""
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": system,
            "messages": messages,
        }
        if self.temperature is not None:
            kwargs["temperature"] = self.temperature
        if tools:
            kwargs["tools"] = [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "input_schema": tool.input_schema,
                }
                for tool in tools
            ]

        try:
            response = self._client.messages.create(**kwargs)
        except anthropic.APIError as exc:
            logger.warning("llm_client.request_failed", model=self.model, error=str(exc))
            raise LLMClientError(f"model request failed: {exc}") from exc

        if response.stop_reason == "refusal":
            raise LLMClientError("model declined the request")

        self.last_usage = LLMUsage(
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )
        text = "".join(block.text for block in response.content if block.type == "text")
        tool_requests = tuple(
            ToolUseRequest(call_id=block.id, tool_name=block.name, tool_input=block.input)
            for block in response.content
            if block.type == "tool_use"
        )
        return ConversationTurn(
            text=text,
            tool_requests=tool_requests,
            usage=self.last_usage,
            stop_reason=response.stop_reason,
        )

    def run_conversation(
        self,
        *,
        system: str,
        user: str,
        tools: Sequence[ToolSpec],
        call_tool: ToolExecutor,
        max_turns: int = DEFAULT_MAX_TURNS,
    ) -> ConversationResult:
        """Hold a conversation, calling tools as needed, until a final answer or the turn cap."""
        messages: list[dict[str, Any]] = [{"role": "user", "content": user}]
        turns: list[ConversationTurn] = []

        for turn_number in range(1, max_turns + 1):
            turn = self.converse(system=system, messages=messages, tools=tools)
            turns.append(turn)
            if not turn.tool_requests:
                return ConversationResult(
                    final_text=turn.text, turns=tuple(turns), stopped_reason="final_answer"
                )
            if turn_number == max_turns:
                break
            messages.append({"role": "assistant", "content": assistant_content_blocks(turn)})
            messages.append({"role": "user", "content": _tool_result_blocks(turn, call_tool)})

        return ConversationResult(final_text=None, turns=tuple(turns), stopped_reason="turn_limit")


def _tool_result_blocks(turn: ConversationTurn, call_tool: ToolExecutor) -> list[dict[str, Any]]:
    return [
        tool_result_block(request.call_id, call_tool(request.tool_name, request.tool_input))
        for request in turn.tool_requests
    ]


class OllamaLLMClient:
    def __init__(
        self,
        *,
        model: str,
        max_tokens: int,
        temperature: float | None = None,
        base_url: str = "http://localhost:11434",
        timeout: float = 120.0,
        json_output: bool = True,
        client: httpx.Client | None = None,
    ) -> None:
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.json_output = json_output
        self.last_usage: LLMUsage | None = None
        self._client = client or httpx.Client(base_url=base_url, timeout=timeout)

    def generate(self, *, system: str, user: str) -> str:
        """Send one system/user turn to the local model and return the reply text."""
        options: dict[str, Any] = {"num_predict": self.max_tokens}
        if self.temperature is not None:
            options["temperature"] = self.temperature

        body: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "think": False,
            "options": options,
        }
        if self.json_output:
            body["format"] = "json"

        try:
            response = self._client.post("/api/chat", json=body)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning("llm_client.request_failed", model=self.model, error=str(exc))
            raise LLMClientError(f"model request failed: {exc}") from exc

        reply = response.json()
        self.last_usage = LLMUsage(
            input_tokens=reply.get("prompt_eval_count", 0),
            output_tokens=reply.get("eval_count", 0),
        )
        return reply["message"]["content"]

    def converse(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: Sequence[ToolSpec] = (),
    ) -> ConversationTurn:
        """Send one turn of a tool calling conversation to the local model."""
        options: dict[str, Any] = {"num_predict": self.max_tokens}
        if self.temperature is not None:
            options["temperature"] = self.temperature

        body: dict[str, Any] = {
            "model": self.model,
            "messages": _openai_messages(system, messages),
            "stream": False,
            "think": False,
            "options": options,
        }
        if tools:
            body["tools"] = _openai_tool_specs(tools)

        try:
            response = self._client.post("/api/chat", json=body)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning("llm_client.request_failed", model=self.model, error=str(exc))
            raise LLMClientError(f"model request failed: {exc}") from exc

        reply = response.json()
        self.last_usage = LLMUsage(
            input_tokens=reply.get("prompt_eval_count", 0),
            output_tokens=reply.get("eval_count", 0),
        )
        message = reply.get("message") or {}
        tool_requests = _tool_requests_from_calls(message.get("tool_calls") or [])
        return ConversationTurn(
            text=message.get("content") or "",
            tool_requests=tool_requests,
            usage=self.last_usage,
            stop_reason="tool_use" if tool_requests else "end_turn",
        )


class LlamaCppLLMClient:
    """Talks to a local llama.cpp server over its OpenAI-style chat endpoint."""

    def __init__(
        self,
        *,
        model: str,
        max_tokens: int,
        temperature: float | None = None,
        base_url: str = "http://localhost:8080",
        timeout: float = 120.0,
        json_output: bool = True,
        client: httpx.Client | None = None,
    ) -> None:
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.json_output = json_output
        self.last_usage: LLMUsage | None = None
        self._client = client or httpx.Client(base_url=base_url, timeout=timeout)

    def generate(self, *, system: str, user: str) -> str:
        """Send one system/user turn to the local server and return the reply text."""
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": self.max_tokens,
            "stream": False,
            "think": False,
        }
        if self.temperature is not None:
            body["temperature"] = self.temperature
        if self.json_output:
            body["response_format"] = {"type": "json_object"}

        try:
            response = self._client.post("/v1/chat/completions", json=body)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning("llm_client.request_failed", model=self.model, error=str(exc))
            raise LLMClientError(f"model request failed: {exc}") from exc

        reply = response.json()
        usage = reply.get("usage") or {}
        self.last_usage = LLMUsage(
            input_tokens=usage.get("prompt_tokens", 0),
            output_tokens=usage.get("completion_tokens", 0),
        )
        choices = reply.get("choices") or []
        if not choices:
            raise LLMClientError("model returned no choices")
        return choices[0]["message"]["content"] or ""

    def converse(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: Sequence[ToolSpec] = (),
    ) -> ConversationTurn:
        """Send one turn of a tool calling conversation to the local server."""
        body: dict[str, Any] = {
            "model": self.model,
            "messages": _openai_messages(system, messages),
            "max_tokens": self.max_tokens,
            "stream": False,
            "think": False,
        }
        if self.temperature is not None:
            body["temperature"] = self.temperature
        if tools:
            body["tools"] = _openai_tool_specs(tools)

        try:
            response = self._client.post("/v1/chat/completions", json=body)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning("llm_client.request_failed", model=self.model, error=str(exc))
            raise LLMClientError(f"model request failed: {exc}") from exc

        reply = response.json()
        usage = reply.get("usage") or {}
        self.last_usage = LLMUsage(
            input_tokens=usage.get("prompt_tokens", 0),
            output_tokens=usage.get("completion_tokens", 0),
        )
        choices = reply.get("choices") or []
        if not choices:
            raise LLMClientError("model returned no choices")
        message = choices[0].get("message") or {}
        tool_requests = _tool_requests_from_calls(message.get("tool_calls") or [])
        return ConversationTurn(
            text=message.get("content") or "",
            tool_requests=tool_requests,
            usage=self.last_usage,
            stop_reason="tool_use" if tool_requests else "end_turn",
        )


def _build_llm_client(
    *,
    provider: str,
    model: str,
    max_tokens: int,
    temperature: float | None,
    anthropic_api_key: str,
    ollama_base_url: str,
    ollama_timeout: float,
    llamacpp_base_url: str,
    llamacpp_timeout: float,
    json_output: bool = True,
) -> AnthropicLLMClient | OllamaLLMClient | LlamaCppLLMClient:
    if provider == "anthropic":
        return AnthropicLLMClient(
            api_key=anthropic_api_key, model=model, max_tokens=max_tokens, temperature=temperature
        )
    if provider == "ollama":
        return OllamaLLMClient(
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            base_url=ollama_base_url,
            timeout=ollama_timeout,
            json_output=json_output,
        )
    if provider == "llamacpp":
        return LlamaCppLLMClient(
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            base_url=llamacpp_base_url,
            timeout=llamacpp_timeout,
            json_output=json_output,
        )
    raise ValueError(f"unknown LLM provider: {provider!r}")


def get_llm_client(settings: Settings | None = None) -> LLMClient:
    """Build the configured LLM client. The one place a provider is chosen."""
    settings = settings or get_settings()
    return _build_llm_client(
        provider=settings.llm_provider,
        model=settings.llm_model,
        max_tokens=settings.llm_max_tokens,
        temperature=settings.llm_temperature,
        anthropic_api_key=settings.anthropic_api_key,
        ollama_base_url=settings.ollama_base_url,
        ollama_timeout=settings.ollama_timeout_seconds,
        llamacpp_base_url=settings.llamacpp_base_url,
        llamacpp_timeout=settings.llamacpp_timeout_seconds,
    )


def resolve_judge_model_config(
    settings: Settings | None = None,
) -> tuple[str, str, float | None, int]:
    settings = settings or get_settings()
    provider = settings.judge_llm_provider or settings.llm_provider
    model = settings.judge_llm_model or settings.llm_model
    return provider, model, settings.judge_llm_temperature, settings.judge_llm_max_tokens


def get_judge_llm_client(settings: Settings | None = None) -> LLMClient:
    settings = settings or get_settings()
    provider, model, temperature, max_tokens = resolve_judge_model_config(settings)
    return _build_llm_client(
        provider=provider,
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
        anthropic_api_key=settings.anthropic_api_key,
        ollama_base_url=settings.ollama_base_url,
        ollama_timeout=settings.ollama_timeout_seconds,
        llamacpp_base_url=settings.llamacpp_base_url,
        llamacpp_timeout=settings.llamacpp_timeout_seconds,
    )


def resolve_briefing_model_config(
    settings: Settings | None = None,
) -> tuple[str, str, float | None, int]:
    settings = settings or get_settings()
    provider = settings.briefing_llm_provider or settings.llm_provider
    model = settings.briefing_llm_model or settings.llm_model
    return provider, model, settings.briefing_llm_temperature, settings.briefing_llm_max_tokens


def get_briefing_llm_client(settings: Settings | None = None) -> LLMClient:
    settings = settings or get_settings()
    provider, model, temperature, max_tokens = resolve_briefing_model_config(settings)
    return _build_llm_client(
        provider=provider,
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
        anthropic_api_key=settings.anthropic_api_key,
        ollama_base_url=settings.ollama_base_url,
        ollama_timeout=settings.ollama_timeout_seconds,
        llamacpp_base_url=settings.llamacpp_base_url,
        llamacpp_timeout=settings.llamacpp_timeout_seconds,
        json_output=False,
    )


def as_model_call(client: LLMClient, *, system: str) -> ModelCall:
    def call(payload: dict[str, Any]) -> str:
        return client.generate(system=system, user=render_model_context(payload))

    return call


def as_converse_call(
    client: ConversingLLMClient, *, system: str, tools: Sequence[ToolSpec] = ()
) -> ConverseCall:
    """Adapt a tool calling client into the shape run_conversation_boundary drives."""

    def call(messages: list[dict[str, Any]]) -> ConversationTurn:
        return client.converse(system=system, messages=messages, tools=tools)

    return call


def resolve_agent_model_config(
    settings: Settings | None = None,
) -> tuple[str, str, float | None, int]:
    settings = settings or get_settings()
    provider = settings.agent_llm_provider or settings.llm_provider
    model = settings.agent_llm_model or settings.llm_model
    return provider, model, settings.agent_llm_temperature, settings.agent_llm_max_tokens


def get_agent_llm_client(settings: Settings | None = None) -> ConversingLLMClient:
    """The model client the nightly agent loop plans and chooses with.

    Works with any configured provider: Anthropic, Ollama and llama.cpp all
    hold the tool calling conversation the loop needs.
    """
    settings = settings or get_settings()
    provider, model, temperature, max_tokens = resolve_agent_model_config(settings)
    return _build_llm_client(
        provider=provider,
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
        anthropic_api_key=settings.anthropic_api_key,
        ollama_base_url=settings.ollama_base_url,
        ollama_timeout=settings.ollama_timeout_seconds,
        llamacpp_base_url=settings.llamacpp_base_url,
        llamacpp_timeout=settings.llamacpp_timeout_seconds,
    )


def get_anthropic_batch_client(settings: Settings | None = None) -> anthropic.Anthropic:
    """The raw Anthropic client, for the batch endpoints the LLMClient"""
    settings = settings or get_settings()
    if settings.llm_provider != "anthropic":
        raise ValueError(
            f"batch generation needs the anthropic provider, not {settings.llm_provider!r}"
        )
    return anthropic.Anthropic(api_key=settings.anthropic_api_key)


def build_batch_request(
    *,
    custom_id: str,
    system_cached: str,
    system_dynamic: str = "",
    user: str,
    settings: Settings | None = None,
) -> BatchRequest:
    """One client's entry for client.messages.batches.create(requests=[...]).

    system_cached carries the ephemeral cache_control breakpoint, so every
    request in a batch that shares the same angle, tier, and product (the
    only things system_cached depends on) can hit the same cache entry
    the Message Batches API caches per request, best-effort
    """
    settings = settings or get_settings()
    system_blocks: list[dict[str, Any]] = [
        {"type": "text", "text": system_cached, "cache_control": {"type": "ephemeral"}}
    ]
    if system_dynamic:
        system_blocks.append({"type": "text", "text": system_dynamic})

    params: dict[str, Any] = {
        "model": settings.llm_model,
        "max_tokens": settings.llm_max_tokens,
        "system": system_blocks,
        "messages": [{"role": "user", "content": user}],
    }
    if settings.llm_temperature is not None:
        params["temperature"] = settings.llm_temperature
    return BatchRequest(custom_id=custom_id, params=MessageCreateParamsNonStreaming(**params))
