from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from app.rag.grounding import UngroundedClaim, enforce_grounding

# A short win back email: long enough to be a real message, short enough to
# stay skimmable, and a subject line that will not truncate in an inbox.
MAX_SUBJECT_LENGTH = 100
MIN_BODY_LENGTH = 20
MAX_BODY_LENGTH = 2000


class GuardrailFailure(Exception):
    """Raised by a guardrail check when a draft fails it, tagged with its name."""

    def __init__(self, message: str, *, guardrail: str = "unknown") -> None:
        super().__init__(message)
        self.guardrail = guardrail


def default_grounding_check(state: Mapping[str, Any]) -> None:
    """Every rate or return claim in the email body must trace to a retrieved chunk."""
    try:
        enforce_grounding(state.get("body") or "", state.get("chunks", []))
    except UngroundedClaim as exc:
        raise GuardrailFailure(str(exc), guardrail="grounding") from exc


def default_format_check(state: Mapping[str, Any]) -> None:
    """The subject and body must fit a short win back email, not run on or off."""
    subject = state.get("subject") or ""
    body = state.get("body") or ""

    if len(subject) > MAX_SUBJECT_LENGTH:
        raise GuardrailFailure(
            f"subject is {len(subject)} characters, over the {MAX_SUBJECT_LENGTH} limit",
            guardrail="format_length",
        )
    if len(body) < MIN_BODY_LENGTH:
        raise GuardrailFailure(
            f"body is {len(body)} characters, under the {MIN_BODY_LENGTH} minimum",
            guardrail="format_length",
        )
    if len(body) > MAX_BODY_LENGTH:
        raise GuardrailFailure(
            f"body is {len(body)} characters, over the {MAX_BODY_LENGTH} limit",
            guardrail="format_length",
        )


# The checks agents.graph runs by default, in order: grounding, then format
# and length. Pass a different sequence to build_generation_graph to change
# or extend this.
DEFAULT_GUARDRAIL_CHECKS: Sequence[Any] = (default_grounding_check, default_format_check)
