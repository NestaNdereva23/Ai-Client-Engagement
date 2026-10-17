from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from app.llmops.tracing import NullTracer, Tracer, trace_scope
from app.privacy.llm_client import LLMClient


@dataclass(frozen=True)
class ActiveTrace:
    tracer: Tracer
    trace_id: str
    parent: Any


@dataclass
class OperationSpan:
    output: Any = None
    level: str | None = None
    status_message: str | None = None


_ACTIVE: ContextVar[ActiveTrace | None] = ContextVar("active_trace", default=None)


def active_tracer() -> Tracer:
    active = _ACTIVE.get()
    return active.tracer if active is not None else NullTracer()


@contextmanager
def trace_operation(
    name: str,
    *,
    tracer: Tracer | None = None,
    as_type: str = "span",
    input: Any = None,
    metadata: dict[str, Any] | None = None,
    tags: Sequence[str] = (),
    user_id: str | None = None,
    session_id: str | None = None,
) -> Iterator[OperationSpan]:
    active = _ACTIVE.get()
    if active is None:
        tracer = tracer or NullTracer()
        trace_id = uuid.uuid4().hex
        parent = None
        scope = trace_scope(tracer, name=name, tags=tags, user_id=user_id, session_id=session_id)
    else:
        tracer, trace_id, parent = active.tracer, active.trace_id, active.parent
        scope = nullcontext()

    operation = OperationSpan()
    with scope:
        handle = tracer.start_span(
            trace_id=trace_id,
            name=name,
            input=input,
            metadata=metadata,
            as_type=as_type,
            parent=parent,
        )
        token = _ACTIVE.set(ActiveTrace(tracer, trace_id, handle))
        try:
            yield operation
        except Exception as exc:
            tracer.end_span(
                handle, output={"error": str(exc)}, level="ERROR", status_message=str(exc)
            )
            raise
        else:
            tracer.end_span(
                handle,
                output=operation.output,
                level=operation.level,
                status_message=operation.status_message,
            )
        finally:
            _ACTIVE.reset(token)
            if active is None:
                tracer.flush()


def traced_model_call(
    model_call: Callable[[dict[str, Any]], str],
    *,
    name: str,
    llm_client: LLMClient,
    system: str,
    metadata: dict[str, Any] | None = None,
) -> Callable[[dict[str, Any]], str]:
    active = _ACTIVE.get()
    if active is None:
        return model_call

    def call(payload: dict[str, Any]) -> str:
        handle = active.tracer.start_span(
            trace_id=active.trace_id,
            name=name,
            input={"system": system, "payload": payload},
            metadata=metadata,
            as_type="generation",
            model=llm_client.model,
            parent=active.parent,
        )
        try:
            text = model_call(payload)
        except Exception as exc:
            active.tracer.end_span(
                handle, output={"error": str(exc)}, level="ERROR", status_message=str(exc)
            )
            raise
        active.tracer.end_span(handle, output=text)
        return text

    return call
