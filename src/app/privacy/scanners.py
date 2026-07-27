"""Scanners run on the way into and out of the model boundary.

Inbound checks the outgoing payload, outbound checks the drafted response. Both
fail closed: a hit is logged and raised, and the model call is aborted.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

# The only keys a model payload may carry: tiers and buckets. The re-attachment
# key (client_id) and every real figure are kept out.
MODEL_ALLOWED_KEYS = frozenset({"archetype", "recency_bucket", "value_tier_label", "rhythm_band"})

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


class BoundaryLeak(Exception):
    """Raised when the PII boundary would be crossed; the model call is aborted."""


class InboundLeak(BoundaryLeak):
    """The outgoing payload carried something outside the allow-list."""


class OutboundLeak(BoundaryLeak):
    """The drafted response echoed an identifier it should not have."""


def _pattern_reasons(text: str, categories: Iterable[str]) -> list[str]:
    return [name for name in categories if _PATTERNS[name].search(text)]


def _literal_reasons(text: str, identifiers: Iterable[str]) -> list[str]:
    lowered = text.lower()
    return [
        "literal_identifier"
        for value in identifiers
        if value and len(value) >= 2 and value.lower() in lowered
    ]


def scan_inbound(payload: Mapping[str, Any], identifiers: Iterable[str] = ()) -> None:
    """Assert only allow-listed keys and block real values before the call.

    Fails closed on an extra key or on any name, contact, number, amount, or date
    found in the serialized payload.
    """
    extra = set(payload) - MODEL_ALLOWED_KEYS
    if extra:
        reason = f"keys outside the allow-list: {sorted(extra)}"
        logger.warning("inbound_blocked", reason=reason)
        raise InboundLeak(reason)

    text = " ".join(str(value) for value in payload.values())
    reasons = _pattern_reasons(text, _INBOUND_CATEGORIES) + _literal_reasons(text, identifiers)
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
