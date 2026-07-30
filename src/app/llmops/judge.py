"""LLM-as-judge: score an already-generated draft against a fixed rubric.

Offline/batch only, never called from agents.graph or the send path. The
judge only ever sees a placeholder-only draft (ai_draft_content, already past
the outbound PII scan at generation time) and the facts retrieved for it, so
it cannot judge personalization to a real person, only whether the draft
reads as though it will personalize well once filled in; the rubric says so
explicitly rather than leaving the model to guess.
"""

from __future__ import annotations

from collections.abc import Sequence

import structlog

from app.privacy.llm_client import LLMClient
from app.privacy.scanners import scan_outbound
from app.rag.grounding import GroundingChunk
from app.schemas.evaluation import EvaluationScores, parse_evaluation_scores

logger = structlog.get_logger(__name__)

_RUBRIC_INSTRUCTIONS = (
    "You are a quality judge for AI drafted win back investment emails. "
    "You will be shown the facts available at generation time, then a "
    "placeholder-only draft (e.g. {{first_name}}, {{fund_name}}); no real "
    "client name, amount, or contact detail was ever available to you or the "
    "model that wrote it. "
    "Score the draft on four dimensions, each an integer from 1 (poor) to 5 "
    "(excellent). "
    "TONE: warm, professional, low-pressure; not pushy, not cold or robotic. "
    "COMPLIANCE: makes no guaranteed-return or unsupported promise, states no "
    "invented number, date, or name, uses appropriately cautious language for "
    "financial content. "
    "GROUNDING: every rate, return, or figure mentioned appears verbatim in "
    "the supplied facts; 5 means no unsupported figures at all, and also 5 "
    "when the draft cites no figures. "
    "PERSONALIZATION: you cannot see who the draft is for, so do not judge "
    "whether it suits a real person. Judge only whether the placeholders are "
    "used naturally and the draft reads as though it will feel personal once "
    "filled in, versus generic boilerplate. "
    "OUTPUT FORMAT: "
    "Return ONLY one valid JSON object with exactly these fields: "
    '{"tone": 1-5, "compliance": 1-5, "grounding": 1-5, "personalization": 1-5, '
    '"notes": "one or two sentences explaining the scores"}. '
    "No markdown, no code fences, no text before or after the JSON object."
)


def rubric_text() -> str:
    """The stable rubric instructions; llmops.versions hashes exactly this to
    register a rubric_versions row, so unchanged wording reuses the same
    version and a genuine wording change registers a new one.
    """
    return _RUBRIC_INSTRUCTIONS


def _render_facts(chunks: Sequence[GroundingChunk]) -> str:
    if not chunks:
        return "(no facts were retrieved for this draft)"
    return "\n".join(f"- {chunk.text}" for chunk in chunks)


def build_judge_prompt(*, chunks: Sequence[GroundingChunk] = ()) -> str:
    """The system prompt for one judging call: the rubric plus the facts."""
    return f"{rubric_text()}\n\nFacts available at generation time:\n{_render_facts(chunks)}"


def judge_draft(
    llm_client: LLMClient, *, draft: str, chunks: Sequence[GroundingChunk] = ()
) -> EvaluationScores:
    """Call the judge model once and return validated scores. Never touches a database."""
    system = build_judge_prompt(chunks=chunks)
    logger.info("judge_request", model=llm_client.model, system=system, user=draft)
    raw = llm_client.generate(system=system, user=draft)
    logger.info("judge_response", model=llm_client.model, raw_output=raw)
    scan_outbound(raw)
    scores = parse_evaluation_scores(raw)
    logger.info(
        "judge_scored",
        model=llm_client.model,
        tone=scores.tone,
        compliance=scores.compliance,
        grounding=scores.grounding,
        personalization=scores.personalization,
        notes=scores.notes,
    )
    return scores
