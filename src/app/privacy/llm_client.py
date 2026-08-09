# Provider-abstracted client for the model API

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

import anthropic
import httpx
import structlog
from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
from anthropic.types.messages.batch_create_params import Request as BatchRequest

from app.config import Settings, get_settings
from app.privacy.boundary import ModelCall

logger = structlog.get_logger(__name__)


class LLMClientError(RuntimeError):
    """Raised when a model call fails after the provider's own retries."""


@dataclass(frozen=True)
class LLMUsage:
    """Token counts for the most recent generate() call."""

    input_tokens: int
    output_tokens: int


@runtime_checkable
class LLMClient(Protocol):
    """Thin interface a draft generation call goes through, any provider."""

    model: str

    def generate(self, *, system: str, user: str) -> str:
        """Return the model's reply text for one system/user turn."""
        ...


class AnthropicLLMClient:
    """Claude implementation of LLMClient.

    Pass in an anthropic.Anthropic instance to make tests fast and offline;
    by default it builds one from the configured API key.
    """

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
        # Newer Claude models reject a non-default temperature outright, so
        # it is only sent when explicitly configured.
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


class OllamaLLMClient:
    """Local Ollama implementation of LLMClient, for testing against an open model."""

    def __init__(
        self,
        *,
        model: str,
        max_tokens: int,
        temperature: float | None = None,
        base_url: str = "http://localhost:11434",
        timeout: float = 120.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.last_usage: LLMUsage | None = None
        self._client = client or httpx.Client(base_url=base_url, timeout=timeout)

    def generate(self, *, system: str, user: str) -> str:
        """Send one system/user turn to the local model and return the reply text."""
        options: dict[str, Any] = {"num_predict": self.max_tokens}
        if self.temperature is not None:
            options["temperature"] = self.temperature

        try:
            response = self._client.post(
                "/api/chat",
                json={
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    "format": "json",
                    "stream": False,
                    "think": False,
                    "options": options,
                },
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning("llm_client.request_failed", model=self.model, error=str(exc))
            raise LLMClientError(f"model request failed: {exc}") from exc

        body = response.json()
        logger.info("ollama_response_body", model=self.model, body=body)
        self.last_usage = LLMUsage(
            input_tokens=body.get("prompt_eval_count", 0),
            output_tokens=body.get("eval_count", 0),
        )
        return body["message"]["content"]


def _build_llm_client(
    *,
    provider: str,
    model: str,
    max_tokens: int,
    temperature: float | None,
    anthropic_api_key: str,
    ollama_base_url: str,
    ollama_timeout: float,
) -> LLMClient:
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
    )


def resolve_judge_model_config(
    settings: Settings | None = None,
) -> tuple[str, str, float | None, int]:
    """The (provider, model, temperature, max_tokens) judge_draft actually runs with.

    judge_llm_provider/judge_llm_model fall back to llm_provider/llm_model when
    unset, so a caller (llmops.versions.persist_evaluation) can stamp the exact
    judge model without re-deriving the same fallback separately.
    """
    settings = settings or get_settings()
    provider = settings.judge_llm_provider or settings.llm_provider
    model = settings.judge_llm_model or settings.llm_model
    return provider, model, settings.judge_llm_temperature, settings.judge_llm_max_tokens


def get_judge_llm_client(settings: Settings | None = None) -> LLMClient:
    """Build the LLM client llmops.judge scores drafts with.

    Independent of get_llm_client: judge_llm_provider/judge_llm_model let the
    judge run a different model than generation (e.g. a second local Ollama
    model), falling back to the generation model when not configured.
    """
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
    )


def render_model_context(payload: dict[str, Any]) -> str:
    """The user-turn text for one allow-listed, already-scanned payload.

    The one place this rendering happens, so the synchronous path (through
    as_model_call) and the batch path (through campaigns.batch_generation)
    send the model byte-for-byte the same framing for the same facts.
    """
    return "\n".join(f"{key}: {value}" for key, value in sorted(payload.items()))


def as_model_call(client: LLMClient, *, system: str) -> ModelCall:
    """Adapt an LLMClient into the ModelCall run_model_boundary expects.

    The payload is already allow listed and bucketed by the time it reaches
    here (run_model_boundary scans it first), so it is safe to serialize
    as is into the user turn.
    """

    def call(payload: dict[str, Any]) -> str:
        return client.generate(system=system, user=render_model_context(payload))

    return call


def get_anthropic_batch_client(settings: Settings | None = None) -> anthropic.Anthropic:
    """The raw Anthropic client, for the batch endpoints the LLMClient
    protocol has no concept of (no other provider here has a batch API).

    Only the client construction is shared with get_llm_client; the
    resulting object is used solely by campaigns.batch_generation to call
    client.messages.batches.*, never to draft outside the model boundary.
    """
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

    The one place a batch request is shaped, so campaigns.batch_generation
    (and anything else that submits a batch) never imports the provider SDK
    directly -- the same rule the rest of this module already enforces for
    a single synchronous call.

    system_cached carries the ephemeral cache_control breakpoint, so every
    request in a batch that shares the same angle, tier, and product (the
    only things system_cached depends on) can hit the same cache entry --
    the Message Batches API caches per request, best-effort, only when the
    marked block is byte-for-byte identical across requests. It is always
    the first system block, since caching covers everything up to and
    including the marked block: put system_dynamic (this one client's own
    facts) after it, never before, or nothing before it would be cached
    either. system_dynamic is omitted entirely when empty, rather than
    sent as an empty text block.
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
