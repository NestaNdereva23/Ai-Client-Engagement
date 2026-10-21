from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

import structlog
from sqlalchemy.orm import Session

from app.agents.prompt_versioning import AGENT_ANGLE_BRIEF_GENERATE, prompt_text
from app.audit.log import record_audit
from app.briefing.client_email import DraftRejected, json_object
from app.briefing.email_options import OptionsRejected, check_angle_words, text_field
from app.briefing.off_limits import OFF_LIMITS_WORDS
from app.config import get_settings
from app.db.models.agent import AgentActionCatalog
from app.llmops.spans import traced_single_call
from app.llmops.trace_names import MODEL_GENERATE_ANGLE_BRIEF
from app.privacy.boundary import AuditSink, run_model_boundary
from app.privacy.fact_block import ProposalFactBlock, round_sig_figs
from app.privacy.llm_client import LLMClient, LLMClientError, as_model_call
from app.privacy.scanners import InboundLeak, OutboundLeak
from app.rules.catalog import load_angle, publish_single_angle

logger = structlog.get_logger(__name__)

MAX_ATTEMPTS = 3
MAX_HEADLINE_CHARS = 80
MAX_WHO_CHARS = 200
MAX_CLAIM_CHARS = 400
MAX_ASK_CHARS = 250
MAX_NEVER_CHARS = 500
MAX_USE_CHARS = 300

INSTRUCTIONS = (
    "You write the message brief a nightly campaign will follow for one group of active "
    "clients who share a situation tonight. A person approves the proposal before anything "
    "sends, and a separate writer follows your brief afterwards to draft the actual email "
    "client by client.\n\n"
    "You are given the group's situation as counts and bands, not any one client's details. "
    "Use only those facts.\n\n"
    "Fields:\n"
    "- headline: a short internal name for this brief. Never sent to a client.\n"
    "- who: which clients in the group this fits, in plain words.\n"
    "- claim: what the email may state as true about them. No exact figures.\n"
    "- ask: what the email asks them to do, in one sentence.\n"
    "- never: the one thing this email must not say, as a sentence starting with 'Never'.\n"
    "- use: how any retrieved product or market fact may be used, if at all.\n\n"
    "Lean toward warmth. Never alarm the client or make them feel watched.\n\n"
    'Return only a JSON object like {"headline": "...", "who": "...", "claim": "...", '
    '"ask": "...", "never": "...", "use": "..."}. No markdown, no code fences, no text '
    "outside the object."
)

CHECKS = (
    "\n\nHOW YOUR REPLY IS CHECKED (a brief that breaks these rules is thrown away):\n"
    "- Write no digits in any field. Spell numbers as words.\n"
    "- claim, ask and use must not use any of these words, in any form, except inside the "
    f"fund's own name: {OFF_LIMITS_WORDS}.\n"
    "- claim, ask and use say only what to talk about, not what to avoid, so do not use "
    "'without', 'avoid', 'do not' or 'never' in them.\n"
    "- never is the one field allowed to use the word 'never'; it is the prohibition, not an "
    "instruction.\n"
    f"- Limits: headline {MAX_HEADLINE_CHARS} characters, who {MAX_WHO_CHARS}, claim "
    f"{MAX_CLAIM_CHARS}, ask {MAX_ASK_CHARS}, never {MAX_NEVER_CHARS}, use {MAX_USE_CHARS}."
)


class AngleBriefRejected(Exception):
    pass


@dataclass(frozen=True)
class ProposalAngleBrief:
    headline: str
    who: str
    claim: str
    ask: str
    never: str
    use: str
    cta: str | None = None


def build_angle_brief_prompt(instructions: str | None = None) -> str:
    return f"{instructions or INSTRUCTIONS}{CHECKS}"


def _build_brief(item: dict[str, Any]) -> ProposalAngleBrief:
    claim = text_field(item, "claim", MAX_CLAIM_CHARS)
    check_angle_words(claim)
    ask = text_field(item, "ask", MAX_ASK_CHARS)
    check_angle_words(ask)
    use = text_field(item, "use", MAX_USE_CHARS)
    check_angle_words(use)
    return ProposalAngleBrief(
        headline=text_field(item, "headline", MAX_HEADLINE_CHARS),
        who=text_field(item, "who", MAX_WHO_CHARS),
        claim=claim,
        ask=ask,
        never=text_field(item, "never", MAX_NEVER_CHARS),
        use=use,
    )


def _read_brief(text: str) -> ProposalAngleBrief:
    try:
        parsed = json_object(text)
    except DraftRejected as exc:
        raise AngleBriefRejected(str(exc)) from exc
    try:
        return _build_brief(parsed)
    except OptionsRejected as exc:
        raise AngleBriefRejected(str(exc)) from exc


def _with_feedback(prompt: str, reason: str) -> str:
    if not reason:
        return prompt
    return (
        f"{prompt}\n\nYOUR PREVIOUS REPLY WAS REFUSED: {reason}. "
        "Write the whole reply again with that fixed."
    )


def generate_angle_brief(
    facts: ProposalFactBlock,
    llm_client: LLMClient,
    *,
    entity_id: str,
    audit: AuditSink,
    instructions: str,
    tracer: Any,
    trace_id: str,
    run_id: str | None = None,
    parent: Any = None,
) -> ProposalAngleBrief:
    payload = facts.to_dict()
    last_reason = ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        text = ""
        system = _with_feedback(instructions, last_reason)
        model_call = traced_single_call(
            as_model_call(llm_client, system=system),
            tracer=tracer,
            trace_id=trace_id,
            model=llm_client.model,
            parent=parent,
            name=MODEL_GENERATE_ANGLE_BRIEF,
            system=system,
            metadata={"attempt": attempt},
        )
        try:
            text = run_model_boundary(
                payload,
                model_call,
                entity_id=entity_id,
                run_id=run_id,
                trace_id=trace_id,
                audit=audit,
            )
        except (InboundLeak, OutboundLeak, LLMClientError) as exc:
            logger.warning("angle_brief_failed", entity_id=entity_id, reason=str(exc))
            raise AngleBriefRejected(str(exc)) from exc
        try:
            return _read_brief(text)
        except AngleBriefRejected as exc:
            last_reason = str(exc)
            logger.warning(
                "angle_brief_rejected",
                entity_id=entity_id,
                attempt=attempt,
                reason=last_reason,
                reply=text[:600],
            )
    raise AngleBriefRejected(last_reason)


def _slug(action_code: str, group_name: str, run_id: int) -> str:
    return f"{action_code}__{group_name}__run{run_id}"


def angle_for_proposal(
    session: Session,
    *,
    action: AgentActionCatalog,
    group_name: str,
    client_count: int,
    money_total_kes: float | None,
    evidence: str,
    evidence_detail: dict[str, Any] | None,
    run_id: int,
    as_of: date,
    llm_client: LLMClient,
    audit: AuditSink,
    tracer: Any,
    trace_id: str,
    parent: Any = None,
) -> str | None:
    if action.message_angle is None:
        return None
    if not get_settings().agent_angle_generation_enabled:
        return action.message_angle

    fallback = load_angle(session, action.message_angle, as_of)
    facts = ProposalFactBlock(
        group_name=group_name,
        action_code=action.action_code,
        action_title=action.title,
        action_who=action.who,
        client_count=client_count,
        money_total_kes=round_sig_figs(money_total_kes),
        evidence=evidence,
        evidence_detail=evidence_detail,
    )
    instructions = build_angle_brief_prompt(prompt_text(session, AGENT_ANGLE_BRIEF_GENERATE, as_of))
    try:
        brief = generate_angle_brief(
            facts,
            llm_client,
            entity_id=f"run{run_id}:{group_name}",
            audit=audit,
            instructions=instructions,
            tracer=tracer,
            trace_id=trace_id,
            run_id=str(run_id),
            parent=parent,
        )
    except AngleBriefRejected as exc:
        record_audit(
            session,
            entity_type="agent_proposal_angle",
            action="fallback",
            entity_id=group_name,
            run_id=str(run_id),
            detail={
                "action_code": action.action_code,
                "group_name": group_name,
                "reason": str(exc),
            },
        )
        return action.message_angle

    slug = _slug(action.action_code, group_name, run_id)
    values = {
        "headline": brief.headline,
        "who": brief.who,
        "claim": brief.claim,
        "ask": brief.ask,
        "never": brief.never,
        "use": brief.use,
        "cta": fallback.cta if fallback is not None else None,
    }
    publish_single_angle(session, slug, values, as_of=as_of, by="agent_loop")
    return slug
