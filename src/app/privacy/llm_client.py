from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable

import anthropic
import httpx
import structlog
from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
from anthropic.types.messages.batch_create_params import Request as BatchRequest

from app.config import Settings, get_settings
from app.privacy.boundary import (
    AsyncConverseCall,
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

AsyncToolExecutor = Callable[[str, dict[str, Any]], Awaitable[Any]]


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


@runtime_checkable
class AsyncLLMClient(Protocol):
    """The same drafting call as LLMClient, awaited instead of blocking."""

    model: str

    async def agenerate(self, *, system: str, user: str) -> str:
        """Return the model's reply text for one system/user turn."""
        ...


@runtime_checkable
class AsyncConversingLLMClient(Protocol):
    """The same tool calling turn as ConversingLLMClient, awaited."""

    model: str

    async def aconverse(
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
    """Talks to Claude. Holds a blocking client and, beside it, an async one.

    The blocking client is what drafting and everything already working use.
    The async twin is for a caller that would otherwise sit on a thread while
    the model thinks. Both build the same request and read the same reply, so
    token counts and stop reasons come out the same either way.
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        max_tokens: int,
        temperature: float | None = None,
        client: anthropic.Anthropic | None = None,
        async_client: anthropic.AsyncAnthropic | None = None,
    ) -> None:
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.last_usage: LLMUsage | None = None
        self._api_key = api_key
        self._client = client or anthropic.Anthropic(api_key=api_key)
        self._async_client = async_client

    @property
    def async_client(self) -> anthropic.AsyncAnthropic:
        """The async client, built the first time something asks for it."""
        if self._async_client is None:
            self._async_client = anthropic.AsyncAnthropic(api_key=self._api_key)
        return self._async_client

    async def aclose(self) -> None:
        """Close the async client, if one was ever built."""
        if self._async_client is not None:
            await self._async_client.close()

    def _generate_kwargs(self, system: str, user: str) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        if self.temperature is not None:
            kwargs["temperature"] = self.temperature
        return kwargs

    def _converse_kwargs(
        self, system: str, messages: list[dict[str, Any]], tools: Sequence[ToolSpec]
    ) -> dict[str, Any]:
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
        return kwargs

    def _record_usage(self, response: Any) -> LLMUsage:
        if response.stop_reason == "refusal":
            raise LLMClientError("model declined the request")
        self.last_usage = LLMUsage(
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )
        return self.last_usage

    def _reply_text(self, response: Any) -> str:
        self._record_usage(response)
        return "".join(block.text for block in response.content if block.type == "text")

    def _reply_turn(self, response: Any) -> ConversationTurn:
        usage = self._record_usage(response)
        text = "".join(block.text for block in response.content if block.type == "text")
        tool_requests = tuple(
            ToolUseRequest(call_id=block.id, tool_name=block.name, tool_input=block.input)
            for block in response.content
            if block.type == "tool_use"
        )
        return ConversationTurn(
            text=text,
            tool_requests=tool_requests,
            usage=usage,
            stop_reason=response.stop_reason,
        )

    def generate(self, *, system: str, user: str) -> str:
        """Send one system/user turn to Claude and return the reply text."""
        try:
            response = self._client.messages.create(**self._generate_kwargs(system, user))
        except anthropic.APIError as exc:
            logger.warning("llm_client.request_failed", model=self.model, error=str(exc))
            raise LLMClientError(f"model request failed: {exc}") from exc
        return self._reply_text(response)

    async def agenerate(self, *, system: str, user: str) -> str:
        """Await one system/user turn to Claude and return the reply text."""
        try:
            response = await self.async_client.messages.create(
                **self._generate_kwargs(system, user)
            )
        except anthropic.APIError as exc:
            logger.warning("llm_client.request_failed", model=self.model, error=str(exc))
            raise LLMClientError(f"model request failed: {exc}") from exc
        return self._reply_text(response)

    def converse(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: Sequence[ToolSpec] = (),
    ) -> ConversationTurn:
        """Send one turn of a conversation, returning a final answer or tool requests."""
        try:
            response = self._client.messages.create(
                **self._converse_kwargs(system, messages, tools)
            )
        except anthropic.APIError as exc:
            logger.warning("llm_client.request_failed", model=self.model, error=str(exc))
            raise LLMClientError(f"model request failed: {exc}") from exc
        return self._reply_turn(response)

    async def aconverse(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: Sequence[ToolSpec] = (),
    ) -> ConversationTurn:
        """Await one turn of a conversation, the async twin of converse."""
        try:
            response = await self.async_client.messages.create(
                **self._converse_kwargs(system, messages, tools)
            )
        except anthropic.APIError as exc:
            logger.warning("llm_client.request_failed", model=self.model, error=str(exc))
            raise LLMClientError(f"model request failed: {exc}") from exc
        return self._reply_turn(response)

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
            results = [
                tool_result_block(request.call_id, call_tool(request.tool_name, request.tool_input))
                for request in turn.tool_requests
            ]
            messages.append({"role": "assistant", "content": assistant_content_blocks(turn)})
            messages.append({"role": "user", "content": results})

        return ConversationResult(final_text=None, turns=tuple(turns), stopped_reason="turn_limit")

    async def arun_conversation(
        self,
        *,
        system: str,
        user: str,
        tools: Sequence[ToolSpec],
        call_tool: AsyncToolExecutor,
        max_turns: int = DEFAULT_MAX_TURNS,
    ) -> ConversationResult:
        """The async twin of run_conversation, turn for turn."""
        messages: list[dict[str, Any]] = [{"role": "user", "content": user}]
        turns: list[ConversationTurn] = []

        for turn_number in range(1, max_turns + 1):
            turn = await self.aconverse(system=system, messages=messages, tools=tools)
            turns.append(turn)
            if not turn.tool_requests:
                return ConversationResult(
                    final_text=turn.text, turns=tuple(turns), stopped_reason="final_answer"
                )
            if turn_number == max_turns:
                break
            results = []
            for request in turn.tool_requests:
                output = await call_tool(request.tool_name, request.tool_input)
                results.append(tool_result_block(request.call_id, output))
            messages.append({"role": "assistant", "content": assistant_content_blocks(turn)})
            messages.append({"role": "user", "content": results})

        return ConversationResult(final_text=None, turns=tuple(turns), stopped_reason="turn_limit")


class OllamaLLMClient:
    """Talks to a local Ollama server, with a blocking client and an async one."""

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
        async_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.json_output = json_output
        self.last_usage: LLMUsage | None = None
        self._base_url = base_url
        self._timeout = timeout
        self._client = client or httpx.Client(base_url=base_url, timeout=timeout)
        self._async_client = async_client

    @property
    def async_client(self) -> httpx.AsyncClient:
        """The async client, built the first time something asks for it."""
        if self._async_client is None:
            self._async_client = httpx.AsyncClient(base_url=self._base_url, timeout=self._timeout)
        return self._async_client

    async def aclose(self) -> None:
        """Close the async client, if one was ever built."""
        if self._async_client is not None:
            await self._async_client.aclose()

    def _options(self) -> dict[str, Any]:
        options: dict[str, Any] = {"num_predict": self.max_tokens}
        if self.temperature is not None:
            options["temperature"] = self.temperature
        return options

    def _generate_body(self, system: str, user: str) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "think": False,
            "options": self._options(),
        }
        if self.json_output:
            body["format"] = "json"
        return body

    def _converse_body(
        self, system: str, messages: list[dict[str, Any]], tools: Sequence[ToolSpec]
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": _openai_messages(system, messages),
            "stream": False,
            "think": False,
            "options": self._options(),
        }
        if tools:
            body["tools"] = _openai_tool_specs(tools)
        return body

    def _record_usage(self, reply: Mapping[str, Any]) -> LLMUsage:
        self.last_usage = LLMUsage(
            input_tokens=reply.get("prompt_eval_count", 0),
            output_tokens=reply.get("eval_count", 0),
        )
        return self.last_usage

    def _reply_text(self, reply: Mapping[str, Any]) -> str:
        self._record_usage(reply)
        return reply["message"]["content"]

    def _reply_turn(self, reply: Mapping[str, Any]) -> ConversationTurn:
        usage = self._record_usage(reply)
        message = reply.get("message") or {}
        tool_requests = _tool_requests_from_calls(message.get("tool_calls") or [])
        return ConversationTurn(
            text=message.get("content") or "",
            tool_requests=tool_requests,
            usage=usage,
            stop_reason="tool_use" if tool_requests else "end_turn",
        )

    def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        try:
            response = self._client.post("/api/chat", json=body)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning("llm_client.request_failed", model=self.model, error=str(exc))
            raise LLMClientError(f"model request failed: {exc}") from exc
        return response.json()

    async def _apost(self, body: dict[str, Any]) -> dict[str, Any]:
        try:
            response = await self.async_client.post("/api/chat", json=body)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning("llm_client.request_failed", model=self.model, error=str(exc))
            raise LLMClientError(f"model request failed: {exc}") from exc
        return response.json()

    def generate(self, *, system: str, user: str) -> str:
        """Send one system/user turn to the local model and return the reply text."""
        return self._reply_text(self._post(self._generate_body(system, user)))

    async def agenerate(self, *, system: str, user: str) -> str:
        """Await one system/user turn to the local model and return the reply text."""
        return self._reply_text(await self._apost(self._generate_body(system, user)))

    def converse(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: Sequence[ToolSpec] = (),
    ) -> ConversationTurn:
        """Send one turn of a tool calling conversation to the local model."""
        return self._reply_turn(self._post(self._converse_body(system, messages, tools)))

    async def aconverse(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: Sequence[ToolSpec] = (),
    ) -> ConversationTurn:
        """Await one turn of a tool calling conversation, the async twin of converse."""
        return self._reply_turn(await self._apost(self._converse_body(system, messages, tools)))


class LlamaCppLLMClient:
    """Talks to a local llama.cpp server over its OpenAI-style chat endpoint.

    Holds a blocking client and an async one, the same way the others do.
    """

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
        async_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.json_output = json_output
        self.last_usage: LLMUsage | None = None
        self._base_url = base_url
        self._timeout = timeout
        self._client = client or httpx.Client(base_url=base_url, timeout=timeout)
        self._async_client = async_client

    @property
    def async_client(self) -> httpx.AsyncClient:
        """The async client, built the first time something asks for it."""
        if self._async_client is None:
            self._async_client = httpx.AsyncClient(base_url=self._base_url, timeout=self._timeout)
        return self._async_client

    async def aclose(self) -> None:
        """Close the async client, if one was ever built."""
        if self._async_client is not None:
            await self._async_client.aclose()

    def _generate_body(self, system: str, user: str) -> dict[str, Any]:
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
        return body

    def _converse_body(
        self, system: str, messages: list[dict[str, Any]], tools: Sequence[ToolSpec]
    ) -> dict[str, Any]:
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
        return body

    def _first_choice(self, reply: Mapping[str, Any]) -> dict[str, Any]:
        usage = reply.get("usage") or {}
        self.last_usage = LLMUsage(
            input_tokens=usage.get("prompt_tokens", 0),
            output_tokens=usage.get("completion_tokens", 0),
        )
        choices = reply.get("choices") or []
        if not choices:
            raise LLMClientError("model returned no choices")
        return choices[0]

    def _reply_text(self, reply: Mapping[str, Any]) -> str:
        return self._first_choice(reply)["message"]["content"] or ""

    def _reply_turn(self, reply: Mapping[str, Any]) -> ConversationTurn:
        message = self._first_choice(reply).get("message") or {}
        tool_requests = _tool_requests_from_calls(message.get("tool_calls") or [])
        return ConversationTurn(
            text=message.get("content") or "",
            tool_requests=tool_requests,
            usage=self.last_usage,
            stop_reason="tool_use" if tool_requests else "end_turn",
        )

    def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        try:
            response = self._client.post("/v1/chat/completions", json=body)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning("llm_client.request_failed", model=self.model, error=str(exc))
            raise LLMClientError(f"model request failed: {exc}") from exc
        return response.json()

    async def _apost(self, body: dict[str, Any]) -> dict[str, Any]:
        try:
            response = await self.async_client.post("/v1/chat/completions", json=body)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning("llm_client.request_failed", model=self.model, error=str(exc))
            raise LLMClientError(f"model request failed: {exc}") from exc
        return response.json()

    def generate(self, *, system: str, user: str) -> str:
        """Send one system/user turn to the local server and return the reply text."""
        return self._reply_text(self._post(self._generate_body(system, user)))

    async def agenerate(self, *, system: str, user: str) -> str:
        """Await one system/user turn to the local server and return the reply text."""
        return self._reply_text(await self._apost(self._generate_body(system, user)))

    def converse(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: Sequence[ToolSpec] = (),
    ) -> ConversationTurn:
        """Send one turn of a tool calling conversation to the local server."""
        return self._reply_turn(self._post(self._converse_body(system, messages, tools)))

    async def aconverse(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: Sequence[ToolSpec] = (),
    ) -> ConversationTurn:
        """Await one turn of a tool calling conversation, the async twin of converse."""
        return self._reply_turn(await self._apost(self._converse_body(system, messages, tools)))


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


def as_async_converse_call(
    client: AsyncConversingLLMClient, *, system: str, tools: Sequence[ToolSpec] = ()
) -> AsyncConverseCall:
    """Adapt a tool calling client into the shape run_conversation_boundary_async drives."""

    async def call(messages: list[dict[str, Any]]) -> ConversationTurn:
        return await client.aconverse(system=system, messages=messages, tools=tools)

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
