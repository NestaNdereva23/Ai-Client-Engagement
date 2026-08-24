from __future__ import annotations

import re
from dataclasses import dataclass

import structlog

from app.briefing.narrative_prompt import build_narrative_prompt, narrative_facts
from app.privacy.boundary import AuditSink, run_model_boundary
from app.privacy.fact_block import RiskFactBlock
from app.privacy.llm_client import LLMClient, LLMClientError, as_model_call
from app.privacy.scanners import InboundLeak, OutboundLeak

logger = structlog.get_logger(__name__)

_DIGIT_RUN = re.compile(r"\d+")

# A quoted key followed by a colon: the giveaway that a reply is a JSON
# object or a field dump rather than the sentences the prompt asked for.
_JSON_FIELD = re.compile(r'"[\w ]+"\s*:')


class UngroundedNarrative(Exception):
    """Raised when a narrative asserts a digit absent from its own fact block."""


class MalformedNarrative(Exception):
    """Raised when a narrative comes back as data rather than as prose."""


@dataclass(frozen=True)
class NarrativeResult:
    """One narration attempt: the text actually returned, and which mode it is.

    mode is "narrative" only when the model's own text passed every check;
    "deterministic_fallback" whenever text is actually the caller's
    deterministic rendering instead -- the caller and the API response both
    read this rather than inferring it, so a fallback is never silently
    indistinguishable from a real narrative.
    """

    text: str
    mode: str


def _band_strings(facts: RiskFactBlock) -> list[str]:
    """Every string-valued (band) fact this block carries -- the only place a
    legitimate digit in a narrative could have come from, since every other
    field on RiskFactBlock is a boolean.
    """
    return [value for value in facts.to_dict().values() if isinstance(value, str)]


def narrative_traceable_digits_check(text: str, facts: RiskFactBlock) -> None:
    allowed = _band_strings(facts)
    untraceable = sorted(
        {
            digits
            for digits in _DIGIT_RUN.findall(text)
            if not any(digits in band for band in allowed)
        }
    )
    if untraceable:
        raise UngroundedNarrative(
            f"narrative carries digits that trace to no band this client's "
            f"RiskFactBlock carries: {untraceable}"
        )


def narrative_prose_check(text: str) -> None:
    """A narrative must read as prose, not as a record.

    A model asked for sentences can still answer with a JSON object or a
    list of field names, especially a small local one. That is not a
    briefing an advisor can read, so it is rejected here and the caller
    shows the deterministic text instead.
    """
    stripped = text.strip()
    if not stripped:
        raise MalformedNarrative("narrative came back empty")
    if stripped.startswith(("{", "[")) or _JSON_FIELD.search(stripped):
        raise MalformedNarrative("narrative came back as data, not prose")


def draft_narrative(
    facts: RiskFactBlock,
    llm_client: LLMClient,
    *,
    fallback_text: str,
    entity_id: str | None = None,
    run_id: str | None = None,
    trace_id: str | None = None,
    audit: AuditSink | None = None,
) -> NarrativeResult:
    """Generate one narrated briefing, or fall back to fallback_text.."""
    prompt = build_narrative_prompt(facts)
    model_call = as_model_call(llm_client, system=prompt)
    try:
        text = run_model_boundary(
            narrative_facts(facts),
            model_call,
            entity_id=entity_id,
            run_id=run_id,
            trace_id=trace_id,
            audit=audit,
        )
        narrative_prose_check(text)
        narrative_traceable_digits_check(text, facts)
    except (
        InboundLeak,
        OutboundLeak,
        UngroundedNarrative,
        MalformedNarrative,
        LLMClientError,
    ) as exc:
        logger.warning("narrative_fallback", entity_id=entity_id, reason=str(exc))
        return NarrativeResult(text=fallback_text, mode="deterministic_fallback")
    return NarrativeResult(text=text, mode="narrative")
