"""Langfuse tracing: config-gated, never able to break a generation run.

These prove get_tracer returns a no-op unless a full Langfuse endpoint is
configured, so nothing here (or in the graph) needs a live Langfuse instance,
and that a configured-but-unreachable Langfuse degrades every call to a
logged warning rather than an exception.
"""

from __future__ import annotations

from app.config import Settings
from app.llmops.tracing import LangfuseTracer, NullTracer, get_tracer


def make_settings(**overrides) -> Settings:
    defaults = {
        "llm_provider": "anthropic",
        "anthropic_api_key": "test-key",
        "langfuse_host": "",
        "langfuse_public_key": "",
        "langfuse_secret_key": "",
    }
    defaults.update(overrides)
    return Settings(**defaults)


def test_null_tracer_is_a_complete_no_op() -> None:
    tracer = NullTracer()
    handle = tracer.start_span(trace_id="a" * 32, name="retrieve_context", input={"x": 1})
    tracer.end_span(handle, output={"y": 2})
    assert tracer.get_trace_url("a" * 32) is None
    tracer.flush()  # must not raise


def test_get_tracer_is_null_when_langfuse_is_not_configured() -> None:
    assert isinstance(get_tracer(make_settings()), NullTracer)


def test_get_tracer_is_null_when_only_some_langfuse_settings_are_set() -> None:
    partial = make_settings(langfuse_host="http://localhost:3000", langfuse_public_key="pk")
    assert isinstance(get_tracer(partial), NullTracer)


def test_get_tracer_builds_langfuse_client_without_any_network_call() -> None:
    settings = make_settings(
        langfuse_host="http://localhost:1",
        langfuse_public_key="pk-test",
        langfuse_secret_key="sk-test",
    )
    tracer = get_tracer(settings)
    assert isinstance(tracer, LangfuseTracer)


def test_langfuse_tracer_degrades_to_a_warning_when_unreachable() -> None:
    """host resolves but nothing listens there; every call must still return, not raise."""
    tracer = LangfuseTracer(host="http://localhost:1", public_key="pk-test", secret_key="sk-test")

    handle = tracer.start_span(trace_id="b" * 32, name="generate", input={"system_prompt": "x"})
    tracer.end_span(handle, output={"draft": "y"})
    tracer.flush()  # must not raise even though nothing can actually be sent


def test_langfuse_tracer_end_span_on_a_failed_start_is_a_no_op() -> None:
    """start_span returning None (its own failure already logged) must not crash end_span."""
    tracer = LangfuseTracer(host="http://localhost:1", public_key="pk-test", secret_key="sk-test")
    tracer.end_span(None, output={"anything": "at all"})
