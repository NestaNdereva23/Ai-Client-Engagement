"""Checks a draft must pass before it can reach a reviewer.

Each raises GuardrailFailure tagged with its own name, so the graph can
retry or reject and the reason survives into the run record.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from app.rag.grounding import UngroundedClaim, enforce_grounding

# A short win back email: long enough to be a real message, short enough to
# stay skimmable, and a subject line that will not truncate in an inbox.
MAX_SUBJECT_LENGTH = 100
MIN_BODY_LENGTH = 20
MAX_BODY_LENGTH = 2000

# Any run of digits, with grouping or a decimal part, as it would be written
# in a sentence. Trailing sentence punctuation is stripped off the match.
_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")
_PLACEHOLDER = re.compile(r"\{\{[^}]*\}\}")


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


def _numbers_in(text: str) -> list[str]:
    return [match.group(0).rstrip(",.") for match in _NUMBER.finditer(text)]


def _written_forms(value: float | int) -> set[str]:
    """The ways one permitted figure could legitimately be written in a sentence."""
    forms = {f"{value:,}", f"{value:g}", str(value)}
    if float(value).is_integer():
        forms |= {f"{value:,.0f}", f"{value:.0f}"}
    else:
        forms |= {f"{value:,.1f}", f"{value:.1f}", f"{value:,.2f}", f"{value:.2f}"}
    return {form.rstrip(",.") for form in forms}


def traceable_numbers(facts: Mapping[str, Any] | None, chunks: Sequence[Any] = ()) -> set[str]:
    """Every number a draft is allowed to contain, from the facts and the chunks."""
    allowed: set[str] = set()
    for value in (facts or {}).values():
        if isinstance(value, bool):
            continue
        if isinstance(value, int | float):
            allowed |= _written_forms(value)
        elif isinstance(value, str):
            allowed |= set(_numbers_in(value))
    for chunk in chunks:
        allowed |= set(_numbers_in(getattr(chunk, "text", "")))
    return allowed


def default_numeric_traceability_check(state: Mapping[str, Any]) -> None:
    """Every number in the body must trace to a supplied fact or retrieved chunk.

    This is what separates personalising from fabricating: a figure the model
    invented, or derived by combining two it was given, traces to neither.
    """
    body = _PLACEHOLDER.sub(" ", state.get("body") or "")
    allowed = traceable_numbers(state.get("facts"), state.get("chunks", []))
    untraceable = sorted({number for number in _numbers_in(body) if number not in allowed})
    if untraceable:
        raise GuardrailFailure(
            f"body carries numbers that trace to no fact or retrieved chunk: {untraceable}",
            guardrail="numeric_traceability",
        )


def _word_count(text: str) -> int:
    return len(text.split())


def default_format_check(state: Mapping[str, Any]) -> None:
    """The subject and body must fit a short win back email, not run on or off.

    A message whose tier sets a word cap is held to that; one without falls
    back to the character bounds every draft shared before tiers existed.
    """
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

    contract = state.get("contract")
    if contract is not None:
        words = _word_count(body)
        if words > contract.max_words:
            raise GuardrailFailure(
                f"body is {words} words, over the {contract.max_words} this tier allows",
                guardrail="format_length",
            )
        return

    if len(body) > MAX_BODY_LENGTH:
        raise GuardrailFailure(
            f"body is {len(body)} characters, over the {MAX_BODY_LENGTH} limit",
            guardrail="format_length",
        )


# The checks agents.graph runs by default, in order: grounding, then numeric
# traceability, then format and length. Pass a different sequence to
# build_generation_graph to change or extend this.
DEFAULT_GUARDRAIL_CHECKS: Sequence[Any] = (
    default_grounding_check,
    default_numeric_traceability_check,
    default_format_check,
)
