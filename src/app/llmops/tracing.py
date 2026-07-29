"""Langfuse tracing: one trace per generation run, retrieve to guardrails.

Config-gated: get_tracer returns NullTracer whenever Langfuse isn't configured
(no host or keys set), so the graph and every test that builds one never needs
a live Langfuse instance. GenerationState's own trace_id (a 32-char hex id,
already the primary key app-side callers join generation_runs and audit_log
on) is reused unchanged as the Langfuse trace id, so a trace is one lookup
away from either: no separate id to store or derive.

A tracer must never be able to break a generation run. Every call into the
Langfuse SDK is caught and logged rather than raised; the SDK itself already
degrades gracefully when the server is unreachable (spans queue in memory,
export failures are logged internally), this only guards against local
misuse (a bad argument, an unpicklable value in state).
"""

from __future__ import annotations

from typing import Any, Protocol

import structlog
from langfuse import Langfuse

from app.config import Settings, get_settings

logger = structlog.get_logger(__name__)


class Tracer(Protocol):
    """Records one span per graph node; every span for a run shares its trace_id."""

    def start_span(
        self,
        *,
        trace_id: str,
        name: str,
        input: Any,
        metadata: dict[str, Any] | None = None,
    ) -> Any:
        """Open a span and return an opaque handle to pass to end_span."""
        ...

    def end_span(self, handle: Any, *, output: Any) -> None:
        """Close a span opened by start_span."""
        ...

    def get_trace_url(self, trace_id: str) -> str | None:
        """A link to the trace in the Langfuse UI, or None when unavailable."""
        ...

    def flush(self) -> None:
        """Send any queued spans now, rather than waiting for the batch interval."""
        ...


class NullTracer:
    """No-op tracer: the default, and what every test builds the graph with."""

    def start_span(
        self,
        *,
        trace_id: str,
        name: str,
        input: Any,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        return None

    def end_span(self, handle: Any, *, output: Any) -> None:
        return None

    def get_trace_url(self, trace_id: str) -> str | None:
        return None

    def flush(self) -> None:
        return None


class LangfuseTracer:
    """Sends spans to a self hosted Langfuse instance."""

    def __init__(self, *, host: str, public_key: str, secret_key: str) -> None:
        self._client = Langfuse(host=host, public_key=public_key, secret_key=secret_key)

    def start_span(
        self,
        *,
        trace_id: str,
        name: str,
        input: Any,
        metadata: dict[str, Any] | None = None,
    ) -> Any:
        try:
            return self._client.start_observation(
                trace_context={"trace_id": trace_id},
                name=name,
                input=input,
                metadata=metadata,
            )
        except Exception:
            logger.warning("langfuse_span_start_failed", name=name, exc_info=True)
            return None

    def end_span(self, handle: Any, *, output: Any) -> None:
        if handle is None:
            return
        try:
            handle.update(output=output)
            handle.end()
        except Exception:
            logger.warning("langfuse_span_end_failed", exc_info=True)

    def get_trace_url(self, trace_id: str) -> str | None:
        try:
            return self._client.get_trace_url(trace_id=trace_id)
        except Exception:
            logger.warning("langfuse_trace_url_failed", exc_info=True)
            return None

    def flush(self) -> None:
        try:
            self._client.flush()
        except Exception:
            logger.warning("langfuse_flush_failed", exc_info=True)


def get_tracer(settings: Settings | None = None) -> Tracer:
    """NullTracer unless a full Langfuse endpoint is configured."""
    settings = settings or get_settings()
    if not settings.langfuse_enabled:
        return NullTracer()
    return LangfuseTracer(
        host=settings.langfuse_base_url,
        public_key=settings.langfuse_public_key,
        secret_key=settings.langfuse_secret_key,
    )
