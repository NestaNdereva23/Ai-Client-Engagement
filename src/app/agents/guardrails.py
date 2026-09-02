"""Checks a draft must pass before it can reach a reviewer.

Each raises GuardrailFailure tagged with its own name, so the graph can
retry or reject
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from app.agents.email_agent import BANNED_WORDS
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

# Client money is KES everywhere. A dollar sign or the letters USD in a
# draft can only be the model inventing a figure in the wrong currency.
_NON_KES_CURRENCY = re.compile(r"\$|\bUSD\b")

_BANNED_WORD_PATTERN = re.compile(
    r"\b(?:" + "|".join(re.escape(word) for word in BANNED_WORDS) + r")\w*",
    re.IGNORECASE,
)

_PERCENT = re.compile(r"\d{1,3}(?:\.\d+)?\s*(?:%|per\s?cent)", re.IGNORECASE)
_VAGUE_RETURN = re.compile(
    r"\b(?:meaningful|competitive|attractive|solid|strong|healthy|generous|great|good|decent)"
    r"\s+(?:returns?|rates?|yields?)\b",
    re.IGNORECASE,
)


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


def default_currency_check(state: Mapping[str, Any]) -> None:
    """A draft's subject and body must never use $ or USD.

    Every client money figure the model is given is KES. There is no
    conversion anywhere in the request path, so a dollar sign or "USD"
    in the output is not a formatting choice, it is a wrong currency.
    """
    text = f"{state.get('subject') or ''}\n{state.get('body') or ''}"
    if _NON_KES_CURRENCY.search(text):
        raise GuardrailFailure(
            "draft used a non-KES currency ($ or USD); client money is KES only",
            guardrail="currency",
        )


def default_banned_words_check(state: Mapping[str, Any]) -> None:
    """A draft's subject and body must never use a word from BANNED_WORDS."""
    text = f"{state.get('subject') or ''}\n{state.get('body') or ''}"
    hits = sorted({match.group(0).lower() for match in _BANNED_WORD_PATTERN.finditer(text)})
    if hits:
        raise GuardrailFailure(
            f"draft used a banned word: {hits}",
            guardrail="banned_words",
        )


def default_sign_off_check(state: Mapping[str, Any]) -> None:
    """When a tier contract specifies a sign off, the body must carry it verbatim."""
    contract = state.get("contract")
    if contract is None:
        return
    body = state.get("body") or ""
    if contract.sign_off not in body:
        raise GuardrailFailure(
            f"body is missing the required sign off: {contract.sign_off!r}",
            guardrail="sign_off",
        )


def default_rate_specificity_check(state: Mapping[str, Any]) -> None:
    """A body must not describe a return vaguely when a real rate was retrieved.

    Only fires when a retrieved chunk actually carries a percentage and the
    body leans on a qualitative phrase like "meaningful returns" instead of
    citing it; a body that never raises the topic of returns, or one that
    already states a figure, is left alone.
    """
    body = state.get("body") or ""
    if _PERCENT.search(body):
        return
    if not _VAGUE_RETURN.search(body):
        return
    chunk_text = " ".join(getattr(chunk, "text", "") for chunk in state.get("chunks", ()))
    if _PERCENT.search(chunk_text):
        raise GuardrailFailure(
            "body describes a return vaguely while a specific rate was retrieved and available",
            guardrail="rate_specificity",
        )


DEFAULT_GUARDRAIL_CHECKS: Sequence[Any] = (
    default_grounding_check,
    default_numeric_traceability_check,
    default_format_check,
    default_currency_check,
    default_banned_words_check,
    default_sign_off_check,
    default_rate_specificity_check,
)


def check_no_unresolved_placeholders(subject: str, body: str) -> None:
    """Every placeholder token in a resolved message must have been filled in.
    A token still present is a substitution bug, not a hallucination.
    """
    leftover = sorted(set(_PLACEHOLDER.findall(f"{subject}\n{body}")))
    if leftover:
        raise GuardrailFailure(
            f"resolved message still carries unresolved placeholders: {leftover}",
            guardrail="unresolved_placeholder",
        )


def instance_numeric_traceability_check(
    *, template_body: str, resolved_body: str, client_facts: Mapping[str, Any] | None
) -> None:
    """The post-instantiation counterpart to default_numeric_traceability_check.

    Every number in the resolved body must either already have been in the
    template's own drafted text, or trace to this client's own substituted
    figure.
    """
    already_allowed = set(_numbers_in(_PLACEHOLDER.sub(" ", template_body)))
    allowed = already_allowed | traceable_numbers(client_facts)
    untraceable = sorted({number for number in _numbers_in(resolved_body) if number not in allowed})
    if untraceable:
        raise GuardrailFailure(
            "resolved body carries numbers that trace to no template fact or this "
            f"client's own facts: {untraceable}",
            guardrail="instance_numeric_traceability",
        )
