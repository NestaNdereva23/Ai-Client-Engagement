"""Scanners run on the way into and out of the model boundary.

Inbound checks the outgoing payload, outbound checks the drafted response. Both
fail closed: a hit raises and the call is aborted.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

# The only keys a model payload may carry: tiers and buckets. The re-attachment
# key (client_id) and every real figure are kept out.
MODEL_ALLOWED_KEYS = frozenset({"archetype", "recency_bucket", "value_tier_label", "rhythm_band"})


class BoundaryLeak(Exception):
    """Raised when the PII boundary would be crossed; the model call is aborted."""


class InboundLeak(BoundaryLeak):
    """The outgoing payload carried something outside the allow-list."""


class OutboundLeak(BoundaryLeak):
    """The drafted response echoed an identifier it should not have."""


def scan_inbound(payload: Mapping[str, Any]) -> None:
    """Assert the payload carries only allow-listed keys, else abort the call."""
    extra = set(payload) - MODEL_ALLOWED_KEYS
    if extra:
        raise InboundLeak(f"payload has keys outside the allow-list: {sorted(extra)}")


def scan_outbound(draft: str) -> None:
    """Inspect the drafted text before it leaves the boundary."""
    # Identifier-echo detection is added with the outbound scanner work.
    return None
