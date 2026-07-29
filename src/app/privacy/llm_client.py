"""Provider-abstracted client for the model API, Claude (Anthropic) primary.

This is the only module allowed to import the Anthropic SDK (enforced by
test_only_privacy_imports_the_model_sdk). Everything outside app.privacy gets
a ModelCall-shaped callable from as_model_call() and hands it to
run_model_boundary(); nothing outside this module talks to the SDK directly.

Model id, temperature, and max tokens all come from Settings, never
hard coded, so switching models or tuning generation is a config change.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

import anthropic
import structlog

from app.config import Settings, get_settings
from app.privacy.boundary import ModelCall

logger = structlog.get_logger(__name__)


class LLMClientError(RuntimeError):
    """Raised when a model call fails after the provider's own retries."""


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

        text = "".join(block.text for block in response.content if block.type == "text")
        return text


def get_llm_client(settings: Settings | None = None) -> LLMClient:
    """Build the configured LLM client. The one place a provider is chosen."""
    settings = settings or get_settings()
    provider = settings.llm_provider
    if provider == "anthropic":
        return AnthropicLLMClient(
            api_key=settings.anthropic_api_key,
            model=settings.llm_model,
            max_tokens=settings.llm_max_tokens,
            temperature=settings.llm_temperature,
        )
    raise ValueError(f"unknown LLM provider: {provider!r}")


def as_model_call(client: LLMClient, *, system: str) -> ModelCall:
    """Adapt an LLMClient into the ModelCall run_model_boundary expects.

    The payload is already allow listed and bucketed by the time it reaches
    here (run_model_boundary scans it first), so it is safe to serialize
    as is into the user turn.
    """

    def call(payload: dict[str, Any]) -> str:
        user = "\n".join(f"{key}: {value}" for key, value in sorted(payload.items()))
        return client.generate(system=system, user=user)

    return call
