"""Words a client email must never use, and the phrasing that sneaks them back in."""

from __future__ import annotations

import re

OFF_LIMITS_WORDS = (
    "balance, withdrawal, risk, warning, flagged, preservation, dormant, churn, fee, score, "
    "market, performance, return, tier, premium, transaction, recent, lately, activity"
)

_OFF_LIMITS = re.compile(
    r"\b(balance|withdraw\w*|risk\w*|warning|flagged|preservation|dormant|churn|fees?|score"
    r"|market|perform\w*|returns?|tiers?|premium|value status"
    r"|transactions?|recent\w*|lately|activit(?:y|ies))\b",
    re.IGNORECASE,
)
_SAYS_WHAT_TO_AVOID = re.compile(r"\b(without|avoid\w*|do not|don['’]t|never)\b", re.IGNORECASE)
_FUND_NAME = re.compile(r"money market", re.IGNORECASE)


def find_off_limits(text: str) -> str | None:
    match = _OFF_LIMITS.search(_FUND_NAME.sub(" ", text))
    return match.group(0) if match else None


def find_avoidance(text: str) -> str | None:
    match = _SAYS_WHAT_TO_AVOID.search(text)
    return match.group(0) if match else None
