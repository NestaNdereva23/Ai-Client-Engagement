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

import functools
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
        as_type: str = "span",
        model: str | None = None,
    ) -> Any: ...

    def end_span(
        self, handle: Any, *, output: Any, usage_details: dict[str, int] | None = None
    ) -> None:
        """Close a span opened by start_span, optionally attaching token usage."""
        ...

    def get_trace_url(self, trace_id: str) -> str | None:
        """A link to the trace in the Langfuse UI, or None when unavailable."""
        ...

    def flush(self) -> None:
        """Send any queued spans now, rather than waiting for the batch interval."""
        ...

    def shutdown(self) -> None:
        """Release background export resources; call once when done tracing."""
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
        as_type: str = "span",
        model: str | None = None,
    ) -> None:
        return None

    def end_span(
        self, handle: Any, *, output: Any, usage_details: dict[str, int] | None = None
    ) -> None:
        return None

    def get_trace_url(self, trace_id: str) -> str | None:
        return None

    def flush(self) -> None:
        return None

    def shutdown(self) -> None:
        return None


class LangfuseTracer:
    """Sends spans to a self hosted Langfuse instance.

    Pass in a Langfuse instance to make tests fast, offline, and free of a
    real background export thread; by default it builds one from host/keys.
    """

    def __init__(
        self,
        *,
        host: str = "",
        public_key: str = "",
        secret_key: str = "",
        client: Langfuse | None = None,
    ) -> None:
        self._client = client or Langfuse(host=host, public_key=public_key, secret_key=secret_key)

    def start_span(
        self,
        *,
        trace_id: str,
        name: str,
        input: Any,
        metadata: dict[str, Any] | None = None,
        as_type: str = "span",
        model: str | None = None,
    ) -> Any:
        try:
            return self._client.start_observation(
                trace_context={"trace_id": trace_id},
                name=name,
                input=input,
                metadata=metadata,
                as_type=as_type,
                model=model,
            )
        except Exception:
            logger.warning("langfuse_span_start_failed", name=name, exc_info=True)
            return None

    def end_span(
        self, handle: Any, *, output: Any, usage_details: dict[str, int] | None = None
    ) -> None:
        if handle is None:
            return
        try:
            handle.update(output=output, usage_details=usage_details)
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

    def shutdown(self) -> None:
        try:
            self._client.shutdown()
        except Exception:
            logger.warning("langfuse_shutdown_failed", exc_info=True)


def get_tracer(settings: Settings | None = None) -> Tracer:
    """NullTracer unless a full Langfuse endpoint is configured.

    Builds a new client on every call, which suits a script that traces one
    run and exits. Long-lived callers want get_shared_tracer instead.
    """
    settings = settings or get_settings()
    if not settings.langfuse_enabled:
        return NullTracer()
    return LangfuseTracer(
        host=settings.langfuse_base_url,
        public_key=settings.langfuse_public_key,
        secret_key=settings.langfuse_secret_key,
    )


@functools.lru_cache(maxsize=1)
def get_shared_tracer() -> Tracer:
    """The one tracer a long-lived process traces every run through.

    Each LangfuseTracer owns a Langfuse client with its own background
    export thread, so building one per request would leak a thread per
    request. Request handlers depend on this instead and never shut it
    down themselves; the agent's own flush is enough to get a run's spans
    out, and the process releases the thread once at shutdown.
    """
    return get_tracer()


def shutdown_shared_tracer() -> None:
    """Release the shared tracer's export thread, if one was ever built."""
    if get_shared_tracer.cache_info().currsize == 0:
        return
    get_shared_tracer().shutdown()
    get_shared_tracer.cache_clear()
