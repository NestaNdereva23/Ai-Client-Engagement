"""Langfuse tracing: config-gated, never able to break a generation run.

These prove get_tracer returns a no-op unless a full Langfuse endpoint is
configured, so nothing here (or in the graph) needs a live Langfuse instance,
and that a failure from the underlying SDK client degrades every call to a
logged warning rather than an exception. The SDK client is injected so these
run against a fake, synchronous double, never a real background export
thread.
"""

from __future__ import annotations

from app.config import Settings
from app.llmops.tracing import (
    LangfuseTracer,
    NullTracer,
    get_shared_tracer,
    get_tracer,
    shutdown_shared_tracer,
)


def make_settings(**overrides) -> Settings:
    defaults = {
        "llm_provider": "anthropic",
        "anthropic_api_key": "test-key",
        "langfuse_base_url": "",
        "langfuse_public_key": "",
        "langfuse_secret_key": "",
    }
    defaults.update(overrides)
    return Settings(**defaults)


class FakeObservation:
    def __init__(self) -> None:
        self.updated: dict | None = None
        self.ended = False

    def update(self, **kwargs):
        self.updated = kwargs
        return self

    def end(self) -> None:
        self.ended = True


class FakeLangfuseClient:
    """Stands in for langfuse.Langfuse: no network, no background thread."""

    def __init__(self, *, raise_on_start: bool = False) -> None:
        self.raise_on_start = raise_on_start
        self.start_calls: list[dict] = []
        self.flushed = False
        self.shut_down = False

    def start_observation(self, **kwargs):
        self.start_calls.append(kwargs)
        if self.raise_on_start:
            raise RuntimeError("boom")
        return FakeObservation()

    def get_trace_url(self, *, trace_id: str) -> str:
        return f"http://fake-langfuse/trace/{trace_id}"

    def flush(self) -> None:
        self.flushed = True

    def shutdown(self) -> None:
        self.shut_down = True


def test_null_tracer_is_a_complete_no_op() -> None:
    tracer = NullTracer()
    handle = tracer.start_span(trace_id="a" * 32, name="retrieve_context", input={"x": 1})
    tracer.end_span(handle, output={"y": 2})
    assert tracer.get_trace_url("a" * 32) is None
    tracer.flush()  # must not raise
    tracer.shutdown()  # must not raise


def test_get_tracer_is_null_when_langfuse_is_not_configured() -> None:
    assert isinstance(get_tracer(make_settings()), NullTracer)


def test_get_tracer_is_null_when_only_some_langfuse_settings_are_set() -> None:
    partial = make_settings(langfuse_base_url="http://localhost:3000", langfuse_public_key="pk")
    assert isinstance(get_tracer(partial), NullTracer)


def test_get_tracer_builds_a_langfuse_tracer_when_fully_configured() -> None:
    settings = make_settings(
        langfuse_base_url="http://localhost:1",
        langfuse_public_key="pk-test",
        langfuse_secret_key="sk-test",
    )
    tracer = get_tracer(settings)
    try:
        assert isinstance(tracer, LangfuseTracer)
    finally:
        tracer.shutdown()


def test_shared_tracer_is_built_once_and_reused() -> None:
    shutdown_shared_tracer()
    try:
        assert get_shared_tracer() is get_shared_tracer()
    finally:
        shutdown_shared_tracer()


def test_shutting_down_the_shared_tracer_never_built_is_a_no_op() -> None:
    shutdown_shared_tracer()
    shutdown_shared_tracer()


def test_start_span_and_end_span_reach_the_underlying_client() -> None:
    fake = FakeLangfuseClient()
    tracer = LangfuseTracer(client=fake)

    handle = tracer.start_span(
        trace_id="b" * 32,
        name="generate",
        input={"system_prompt": "x"},
        as_type="generation",
        model="claude-opus-5",
    )
    tracer.end_span(handle, output={"draft": "y"}, usage_details={"input": 10, "output": 20})

    assert fake.start_calls[0]["trace_context"] == {"trace_id": "b" * 32}
    assert fake.start_calls[0]["as_type"] == "generation"
    assert fake.start_calls[0]["model"] == "claude-opus-5"
    assert handle.updated == {
        "output": {"draft": "y"},
        "usage_details": {"input": 10, "output": 20},
    }
    assert handle.ended is True


def test_start_span_degrades_to_a_warning_when_the_client_raises() -> None:
    fake = FakeLangfuseClient(raise_on_start=True)
    tracer = LangfuseTracer(client=fake)

    handle = tracer.start_span(trace_id="c" * 32, name="generate", input={})
    assert handle is None
    tracer.end_span(handle, output={})  # a None handle must not crash end_span


def test_flush_and_shutdown_reach_the_underlying_client() -> None:
    fake = FakeLangfuseClient()
    tracer = LangfuseTracer(client=fake)
    tracer.flush()
    tracer.shutdown()
    assert fake.flushed is True
    assert fake.shut_down is True


def test_get_trace_url_reaches_the_underlying_client() -> None:
    fake = FakeLangfuseClient()
    tracer = LangfuseTracer(client=fake)
    assert tracer.get_trace_url("d" * 32) == "http://fake-langfuse/trace/" + "d" * 32
