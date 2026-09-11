"""Checks that no async endpoint reaches the database through a blocking session."""

from __future__ import annotations

import inspect
import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute

BLOCKING_CALLABLES = frozenset(
    {
        "get_session",
        "safe_session",
        "restricted_session",
        "SessionLocal",
        "check_connection",
    }
)


@dataclass(frozen=True)
class Breach:
    path: str
    methods: tuple[str, ...]
    endpoint: str
    reason: str

    def describe(self) -> str:
        methods = ",".join(sorted(self.methods))
        return f"{methods} {self.path} ({self.endpoint}): {self.reason}"


def _dependency_calls(dependant: Dependant) -> list[Any]:
    calls: list[Any] = []
    for sub in dependant.dependencies:
        if sub.call is not None:
            calls.append(sub.call)
        calls.extend(_dependency_calls(sub))
    return calls


def _blocking_dependency(route: APIRoute) -> str | None:
    for call in _dependency_calls(route.dependant):
        name = getattr(call, "__name__", type(call).__name__)
        if name in BLOCKING_CALLABLES and not inspect.iscoroutinefunction(call):
            return f"depends on the blocking {name}"
    return None


def _blocking_body(endpoint: Any) -> str | None:
    try:
        source = inspect.getsource(endpoint)
    except (OSError, TypeError):
        return None
    for name in sorted(BLOCKING_CALLABLES):
        # On a word boundary, so AsyncSessionLocal is not read as SessionLocal.
        if re.search(rf"\b{name}\(", source):
            return f"calls the blocking {name} in its body"
    return None


def _iter_api_routes(routes: Any, prefix: str = "") -> Iterator[tuple[str, APIRoute]]:
    for route in routes:
        if isinstance(route, APIRoute):
            yield prefix + route.path, route
            continue
        included = getattr(route, "original_router", None)
        if included is not None:
            context = getattr(route, "include_context", None)
            yield from _iter_api_routes(included.routes, prefix + getattr(context, "prefix", ""))
            continue
        nested = getattr(route, "routes", None)
        if nested:
            yield from _iter_api_routes(nested, prefix + getattr(route, "path", ""))


def find_blocking_async_routes(app: FastAPI) -> list[Breach]:
    breaches: list[Breach] = []
    for path, route in _iter_api_routes(app.routes):
        if not inspect.iscoroutinefunction(route.endpoint):
            continue
        reason = _blocking_dependency(route) or _blocking_body(route.endpoint)
        if reason is None:
            continue
        breaches.append(
            Breach(
                path=path,
                methods=tuple(route.methods or ()),
                endpoint=route.endpoint.__qualname__,
                reason=reason,
            )
        )
    return breaches
