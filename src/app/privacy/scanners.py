"""Scanners run on the way into and out of the model boundary.

Inbound checks the outgoing payload, outbound checks the drafted response. Both
fail closed: a hit is logged and raised, and the model call is aborted.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

import structlog
from pydantic import BaseModel, ValidationError

from app.privacy.fact_block import ModelFactBlock, RiskFactBlock

logger = structlog.get_logger(__name__)

# Closed schemas a payload outside the legacy bucket set may validate
# against, tried in order. Each guarantees no key outside its own declared
# fields and no band value outside its own vocabulary, so validating
# against either is an equally strong inbound guarantee -- this is not
# "prefer ModelFactBlock", just the set of shapes scan_inbound currently
# knows about. Add a new FactBlock schema here, not a parallel code path.
_FACT_BLOCK_SCHEMAS: tuple[type[BaseModel], ...] = (ModelFactBlock, RiskFactBlock)

# The bucket allow-list: every non-numeric column llm_client_context exposes,
# minus client_id. A payload shaped exactly like this is checked the same way
# it always was, a pattern sweep; anything else (a raw figure payload) validates
# against ModelFactBlock instead, the wider, typed path for real numbers.
MODEL_ALLOWED_KEYS = frozenset(
    {
        "recency_band",
        "value_band",
        "cadence_band",
        "hold_band",
        "purchase_depth",
        "trend_band",
        "exit_reason",
        "fund_type",
        "in_wave",
        "has_depth",
        "staged_exit",
        "stale_contact",
        "newly_dormant",
    }
)

# Patterns for values that could only come from real client data. Tuned not to
# fire on the fixed bucket vocabulary, which carries only small bare integers.
_PATTERNS: dict[str, re.Pattern[str]] = {
    "email": re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"),
    # Seven or more digits, optionally spaced or dashed: a phone or account number.
    "account_or_phone": re.compile(r"(?:\d[\s().+-]?){7,}"),
    # Currency-tagged, thousands-grouped, or two-decimal amounts.
    "money": re.compile(
        r"(?:KES|KSh|Ksh|USD|\$)\s?\d[\d,]*(?:\.\d+)?|\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+\.\d{2}"
    ),
    # ISO or slash-separated calendar dates.
    "date": re.compile(r"\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}/\d{1,2}/\d{2,4}\b"),
}
_INBOUND_CATEGORIES = ("email", "account_or_phone", "money", "date")
# A placeholder-only draft may not carry a live contact channel.
_OUTBOUND_CATEGORIES = ("email", "account_or_phone")
_PLACEHOLDER = re.compile(r"\{\{[^}]*\}\}")

# Fields ModelFactBlock's validator may silently correct rather than reject:
# an amount rounds, and a cadence fact nulls out when there is no real
# cadence. A raw payload that does not already match its own validated form
# carries the thing the correction exists to remove, not a validation gap.
_VALIDATOR_CORRECTED_FIELDS = (
    "typical_contribution_kes",
    "largest_contribution_kes",
    "years_since_exit",
    "invested_every_n_days",
)


class BoundaryLeak(Exception):
    """Raised when the PII boundary would be crossed; the model call is aborted."""


class InboundLeak(BoundaryLeak):
    """The outgoing payload carried something outside the allow-list."""


class OutboundLeak(BoundaryLeak):
    """The drafted response echoed an identifier it should not have."""


def _pattern_reasons(text: str, categories: Iterable[str]) -> list[str]:
    return [name for name in categories if _PATTERNS[name].search(text)]


def _validate_against_known_schema(payload: Mapping[str, Any]) -> BaseModel:
    """The first schema in _FACT_BLOCK_SCHEMAS this payload validates against.

    A payload that matches none of them raises the last schema's own
    ValidationError, since that is the only diagnostic available -- there is
    no single "correct" schema to blame when a payload matches neither.
    """
    last_exc: ValidationError | None = None
    for schema in _FACT_BLOCK_SCHEMAS:
        try:
            return schema(**payload)
        except ValidationError as exc:
            last_exc = exc
    assert last_exc is not None
    raise last_exc


def _literal_reasons(text: str, identifiers: Iterable[str]) -> list[str]:
    lowered = text.lower()
    return [
        "literal_identifier"
        for value in identifiers
        if value and len(value) >= 2 and value.lower() in lowered
    ]


def scan_inbound(payload: Mapping[str, Any], identifiers: Iterable[str] = ()) -> None:
    """Validate the payload's shape, then check it for a literal identifier.

    A payload keyed exactly to the legacy bucket set is checked the way it
    always was: a pattern sweep across its values. Any other shape validates
    against the first schema in _FACT_BLOCK_SCHEMAS it matches, whose
    declared, typed fields make that sweep unnecessary for it. Either way, a
    literal identifier still fails closed.
    """
    text = " ".join(str(value) for value in payload.values())

    if set(payload) <= MODEL_ALLOWED_KEYS:
        reasons = _pattern_reasons(text, _INBOUND_CATEGORIES)
        if reasons:
            logger.warning("inbound_blocked", reasons=reasons)
            raise InboundLeak(f"payload matched {reasons}")
    else:
        try:
            block = _validate_against_known_schema(payload)
        except ValidationError as exc:
            reason = f"payload failed fact-block validation: {exc}"
            logger.warning("inbound_blocked", reason=reason)
            raise InboundLeak(reason) from exc

        # A caller that built the payload by hand rather than through the
        # matched schema could still hand scan_inbound a value that schema's
        # own validator would have corrected: an exact figure, or a cadence
        # fact for a client the schema itself says has none. Only a payload
        # that already matches its own validated form may pass, so neither
        # shortcut leaks. Harmless no-op for a schema with nothing to
        # correct (RiskFactBlock carries none of these fields at all).
        uncorrected = [
            field
            for field in _VALIDATOR_CORRECTED_FIELDS
            if payload.get(field) is not None and payload[field] != getattr(block, field, None)
        ]
        if uncorrected:
            reason = f"payload carried a value the schema would have corrected: {uncorrected}"
            logger.warning("inbound_blocked", reason=reason)
            raise InboundLeak(reason)

    reasons = _literal_reasons(text, identifiers)
    if reasons:
        logger.warning("inbound_blocked", reasons=reasons)
        raise InboundLeak(f"payload matched {reasons}")


def scan_outbound(draft: str, identifiers: Iterable[str] = ()) -> None:
    """Block a draft that echoes an identifier.

    Placeholders like {{first_name}} are allowed; a literal contact channel or a
    real client value is not.
    """
    text = _PLACEHOLDER.sub(" ", draft)
    reasons = _pattern_reasons(text, _OUTBOUND_CATEGORIES) + _literal_reasons(text, identifiers)
    if reasons:
        logger.warning("outbound_blocked", reasons=reasons)
        raise OutboundLeak(f"draft echoed {reasons}")
