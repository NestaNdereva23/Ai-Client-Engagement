"""Ground every rate or return claim in a draft against retrieved chunks.

A win back email may cite a figure like a fund yield, but only one that came
from the retrieved report. This pulls each percentage claim from the draft and
checks it appears in a retrieved chunk, recording which chunk supports it. A
claim found in no chunk is flagged so the caller can reject the draft before it
reaches review; it is fail closed, the same guardrail the agent output uses.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

# A percentage claim: the number before a % or "per cent".
_PCT = re.compile(r"(\d{1,3}(?:\.\d+)?)\s*(?:%|per\s?cent)", re.I)
_PLACEHOLDER = re.compile(r"\{\{[^}]*\}\}")


@runtime_checkable
class GroundingChunk(Protocol):
    chunk_id: int
    text: str


@dataclass(frozen=True)
class Claim:
    value: str  # e.g. "11.35%"
    supported: bool
    chunk_ids: list[int]


@dataclass(frozen=True)
class GroundingResult:
    claims: list[Claim]

    @property
    def ok(self) -> bool:
        """True when every rate claim traces to a chunk (or there are none)."""
        return all(c.supported for c in self.claims)

    @property
    def unsupported(self) -> list[str]:
        return [c.value for c in self.claims if not c.supported]


class UngroundedClaim(Exception):
    """A draft asserted a rate that no retrieved chunk supports."""


def _rates(text: str) -> set[str]:
    return {m.group(1) for m in _PCT.finditer(text)}


def check_grounding(draft: str, chunks: Sequence[GroundingChunk]) -> GroundingResult:
    """Return each rate claim in the draft with the chunk ids that support it.

    Placeholder tokens are ignored; they are filled server-side from a cited
    chunk, not asserted by the model.
    """
    text = _PLACEHOLDER.sub(" ", draft)
    claims: list[Claim] = []
    seen: set[str] = set()
    for match in _PCT.finditer(text):
        value = match.group(1)
        if value in seen:
            continue
        seen.add(value)
        supporting = [c.chunk_id for c in chunks if value in _rates(c.text)]
        claims.append(Claim(value=f"{value}%", supported=bool(supporting), chunk_ids=supporting))
    return GroundingResult(claims=claims)


def enforce_grounding(draft: str, chunks: Sequence[GroundingChunk]) -> GroundingResult:
    """Check grounding and raise if any rate claim is unsupported."""
    result = check_grounding(draft, chunks)
    if not result.ok:
        raise UngroundedClaim(f"unsupported rate claims: {result.unsupported}")
    return result
