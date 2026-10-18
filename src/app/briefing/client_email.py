from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

import structlog

from app.briefing.next_action import EMAIL_PURPOSES
from app.briefing.off_limits import OFF_LIMITS_WORDS, find_off_limits
from app.briefing.tone_cue import tone_cue
from app.llmops.operations import traced_model_call
from app.llmops.trace_names import MODEL_WRITE_CLIENT_EMAIL
from app.privacy.boundary import AuditSink, run_model_boundary
from app.privacy.fact_block import AssistantFactBlock
from app.privacy.llm_client import LLMClient, LLMClientError, as_model_call
from app.privacy.scanners import InboundLeak, OutboundLeak

logger = structlog.get_logger(__name__)

FIRST_NAME_PLACEHOLDER = "{{first_name}}"
ALLOWED_TOKENS = (FIRST_NAME_PLACEHOLDER, "{{fund_name}}")
MAX_SUBJECT_CHARS = 120
CONTACT_KEYS = ("last_contact_channel", "last_contact_outcome", "last_contact_age_band")
EMAIL_FACT_KEYS = ("fund_name", "holds_both_funds", "missed_meeting", *CONTACT_KEYS)

CONTACT_NOUNS = {
    "call": "a phone call",
    "email": "an email",
    "meeting": "a meeting",
    "whatsapp": "a WhatsApp message",
}
CONTACT_WHEN = {
    "this_week": "this week",
    "this_month": "this month",
    "last_three_months": "in the last three months",
    "older": "more than three months ago",
}
CONTACT_ENDINGS = {
    "follow_up": ", and we agreed to follow up",
    "no_answer": ", but we did not connect",
}

_DIGIT = re.compile(r"\d")
_TOKEN = re.compile(r"\{\{[^{}]*\}\}")
_LOOSE_TOKEN = re.compile(r"[\[{]{1,2}\s*(first_name|fund_name)\s*[\]}]{1,2}")
_GREETING = re.compile(r"^(?:Dear|Hi|Hello)\s+[^,\n{}]{1,40},", re.IGNORECASE)

INSTRUCTIONS = (
    "Write one short email from a client's financial advisor to that client. It is a warm, "
    "plain check in. A person will read and edit it before anything is sent.\n\n"
    "Rules:\n"
    f"- Open with 'Dear {FIRST_NAME_PLACEHOLDER},' exactly as written, braces included. "
    "It is swapped for the real first name after you write. Never invent a name.\n"
    "- Use only the facts below. Do not add any other fact about the client.\n"
    "- Write no digits at all. No amounts, dates, percentages or counts.\n"
    "- Never mention a balance, a risk, a warning sign, a score or that the client was "
    "flagged. The client must not feel watched.\n"
    "- Never tell the client what you know about their account. Do not say you noticed a "
    "deposit, a withdrawal, a gap or any change in activity, and do not say how long since "
    "their last deposit. The facts only help you pick a fitting tone.\n"
    "- Never promise a return, a rate or a guarantee.\n"
    "- Ask one easy question, such as whether they would like a short call.\n"
    "- Plain, everyday English. Under 110 words in the body. No sign off name; the advisor "
    "adds their own.\n\n"
    'Return only a JSON object with two string fields: {"subject": "...", "body": "..."}. '
    "No markdown, no code fences, no text outside the object."
)


class DraftRejected(Exception):
    pass


@dataclass(frozen=True)
class EmailDraft:
    subject: str
    body: str


def email_facts(facts: AssistantFactBlock) -> dict[str, Any]:
    shown = {key: value for key, value in facts.to_dict().items() if key in EMAIL_FACT_KEYS}
    if shown.get("last_contact_channel") == "none":
        for key in CONTACT_KEYS:
            shown.pop(key, None)
    return shown


def _contact_line(facts: dict[str, Any]) -> str | None:
    noun = CONTACT_NOUNS.get(facts.get("last_contact_channel"))
    when = CONTACT_WHEN.get(facts.get("last_contact_age_band"))
    if noun is None or when is None:
        return None
    ending = CONTACT_ENDINGS.get(facts.get("last_contact_outcome"), "")
    return f"- Our last contact with them was {noun} {when}{ending}"


def _checks(angle: str | None) -> str:
    rules = [
        "Write no digits at all. Spell any number as a word, for example 'ten minutes'.",
        f"The only placeholders you may use are {' and '.join(ALLOWED_TOKENS)}.",
    ]
    if angle:
        rules.append(
            f"Never use these words, except inside the fund's own name: {OFF_LIMITS_WORDS}."
        )
        rules.append(
            "Never say that anything is being left out, for example 'without mentioning ...'."
        )
    bullets = "\n".join(f"- {rule}" for rule in rules)
    return f"\n\nHOW YOUR REPLY IS CHECKED (a draft that breaks these is thrown away):\n{bullets}"


def build_email_prompt(
    facts: dict[str, Any],
    purpose: str | None = None,
    angle: str | None = None,
    instructions: str | None = None,
    purpose_text: str | None = None,
    tone: str | None = None,
) -> str:
    lines = []
    if "fund_name" in facts:
        lines.append(f"- The fund they hold: {facts['fund_name']}")
    if facts.get("holds_both_funds"):
        lines.append("- They also hold the other fund")
    contact = _contact_line(facts)
    if contact:
        lines.append(contact)
    if facts.get("missed_meeting"):
        lines.append("- A meeting we had planned with them did not take place")
    shown = "\n".join(lines) if lines else "(no facts are available for this client)"
    why = angle or purpose_text or (EMAIL_PURPOSES[purpose] if purpose else None)
    reason = f"\n\nWHY YOU ARE WRITING:\n{why}" if why else ""
    guidance = f"\n\nAPPROVED COMMUNICATION GUIDANCE:\n{tone}" if tone else ""
    return (
        f"{instructions or INSTRUCTIONS}{reason}{guidance}"
        f"\n\nTHE ONLY FACTS YOU MAY USE:\n{shown}{_checks(angle)}"
    )


def json_object(text: str) -> dict[str, Any]:
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise DraftRejected("the reply was not a JSON object")
    try:
        parsed = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        raise DraftRejected("the reply was not valid JSON") from None
    if not isinstance(parsed, dict):
        raise DraftRejected("the reply was not a JSON object")
    return parsed


def _fix_token_braces(text: str) -> str:
    return _LOOSE_TOKEN.sub(lambda match: "{{" + match.group(1) + "}}", text)


def _restore_first_name(body: str) -> str:
    if FIRST_NAME_PLACEHOLDER in body:
        return body
    return _GREETING.sub(f"Dear {FIRST_NAME_PLACEHOLDER},", body, count=1)


def parse_email_draft(text: str) -> EmailDraft:
    parsed = json_object(text)
    subject, body = parsed.get("subject"), parsed.get("body")
    if not isinstance(subject, str) or not isinstance(body, str):
        raise DraftRejected("subject and body must both be text")
    subject = _fix_token_braces(subject.strip())
    body = _restore_first_name(_fix_token_braces(body.strip()))
    if not subject or not body:
        raise DraftRejected("subject or body was empty")
    if len(subject) > MAX_SUBJECT_CHARS:
        raise DraftRejected("subject was too long")
    if FIRST_NAME_PLACEHOLDER not in body:
        raise DraftRejected("the body did not keep the first name placeholder")
    if _DIGIT.search(subject) or _DIGIT.search(body):
        raise DraftRejected("the draft carried digits that came from no supplied fact")
    if any(token not in ALLOWED_TOKENS for token in _TOKEN.findall(subject + body)):
        raise DraftRejected("the draft carried a token that is not allowed")
    return EmailDraft(subject=subject, body=body)


def check_off_limits_words(draft: EmailDraft) -> None:
    word = find_off_limits(f"{draft.subject} {draft.body}")
    if word:
        raise DraftRejected(
            f"the draft used the word '{word}', which the client must not be told about"
        )


def draft_client_email(
    facts: AssistantFactBlock,
    llm_client: LLMClient,
    *,
    entity_id: str,
    audit: AuditSink,
    purpose: str | None = None,
    angle: str | None = None,
    instructions: str | None = None,
    purpose_text: str | None = None,
) -> EmailDraft:
    allowed = email_facts(facts)
    system = build_email_prompt(
        allowed, purpose, angle, instructions, purpose_text, tone_cue(facts)
    )
    model_call = traced_model_call(
        as_model_call(llm_client, system=system),
        name=MODEL_WRITE_CLIENT_EMAIL,
        llm_client=llm_client,
        system=system,
    )
    text = ""
    try:
        text = run_model_boundary(allowed, model_call, entity_id=entity_id, audit=audit)
        draft = parse_email_draft(text)
        if angle:
            check_off_limits_words(draft)
        return draft
    except (InboundLeak, OutboundLeak, LLMClientError, DraftRejected) as exc:
        logger.warning(
            "client_email_draft_failed",
            entity_id=entity_id,
            reason=str(exc),
            reply=text[:600],
        )
        raise DraftRejected(str(exc)) from exc
