from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, replace
from typing import Any

import structlog

from app.briefing.client_email import DraftRejected, json_object
from app.llmops.operations import traced_model_call
from app.llmops.trace_names import MODEL_SUGGEST_EMAIL_OPTIONS
from app.privacy.boundary import AuditSink, run_model_boundary
from app.privacy.fact_block import AssistantFactBlock
from app.privacy.llm_client import LLMClient, LLMClientError, as_model_call
from app.privacy.scanners import InboundLeak, OutboundLeak

logger = structlog.get_logger(__name__)

MIN_OPTIONS = 2
MAX_OPTIONS = 4
MAX_ATTEMPTS = 3
MAX_TAGS = 3
MAX_TITLE_CHARS = 70
MAX_SUMMARY_CHARS = 300
MAX_WHY_CHARS = 260
MAX_ANGLE_CHARS = 420
MAX_TAG_CHARS = 24

_DIGIT = re.compile(r"\d")
_OFF_LIMITS_IN_ANGLE = re.compile(
    r"\b(balance|withdraw\w*|risk\w*|warning|flagged|preservation|dormant|churn|fees?|score"
    r"|market|perform\w*|returns?|tiers?|premium|value status)\b",
    re.IGNORECASE,
)

INSTRUCTIONS = (
    "You help a financial advisor decide which email to send to one client. Suggest different "
    "kinds of email that could fit this client. A person reads every suggestion, picks one, and "
    "reviews the finished email before anything is sent.\n\n"
    "You are given facts about the client as bands, not exact figures. Use only those facts.\n\n"
    "Rules:\n"
    f"- Give between {MIN_OPTIONS} and {MAX_OPTIONS} options. They must differ in approach, not "
    "only in wording.\n"
    "- Mark exactly one option as recommended: the one that fits this client best.\n"
    "- Lean toward warmth and encouragement. Never alarm the client or make them feel watched.\n"
    "- Write no digits at all anywhere in your reply.\n"
    "- You know nothing about market events or how any investment has performed. Never claim or "
    "hint at any of that.\n"
    "- Never hint that the client's recent activity was noticed, for example with phrases like "
    "'now that things have settled' or 'after your recent move'.\n"
    "- summary and why speak to the advisor, for example 'The email thanks the client...'. Never "
    "address the client as 'you' in them.\n"
    "- title: a short plain name for the kind of email.\n"
    "- summary: one or two sentences telling the advisor what the email would say and do.\n"
    "- why: one sentence telling the advisor why this fits this client. You may name the "
    "client's situation in plain words here. This is shown to the advisor only.\n"
    "- angle: one to three sentences of instruction for the person who writes the email. Say "
    "the tone and what to talk about. The angle must never mention a balance, a withdrawal, a "
    "risk, a warning, a fee, a score or that anything about the client was noticed.\n"
    "- tags: up to three short words about the tone.\n\n"
    'Return only a JSON object like {"options": [{"title": "...", "summary": "...", '
    '"why": "...", "angle": "...", "tags": ["..."], "recommended": true}]}. '
    "No markdown, no code fences, no text outside the object."
)


class OptionsRejected(Exception):
    pass


@dataclass(frozen=True)
class EmailOption:
    option_id: str
    title: str
    summary: str
    why: str
    angle: str
    tags: tuple[str, ...]
    recommended: bool


def build_options_prompt(
    goal: str | None = None, approach: str | None = None, instructions: str | None = None
) -> str:
    lines = []
    if goal:
        lines.append(f"- What the team wants from this client: {goal}")
    if approach:
        lines.append(f"- How the team usually goes about it: {approach}")
    guidance = "\n\nGUIDANCE FROM THE TEAM:\n" + "\n".join(lines) if lines else ""
    return f"{instructions or INSTRUCTIONS}{guidance}"


def _text_field(item: dict[str, Any], name: str, limit: int) -> str:
    value = item.get(name)
    if not isinstance(value, str) or not value.strip():
        raise OptionsRejected(f"{name} was missing or empty")
    value = value.strip()
    if len(value) > limit:
        raise OptionsRejected(f"{name} was too long")
    if _DIGIT.search(value):
        raise OptionsRejected(f"{name} carried digits that came from no supplied fact")
    return value


def _off_limits_reason(word: str) -> str:
    return f"an angle used the word '{word}', which the client must not be told about"


def check_angle(angle: str) -> str:
    angle = angle.strip()
    if not angle or len(angle) > MAX_ANGLE_CHARS:
        raise OptionsRejected("the angle was empty or too long")
    if _DIGIT.search(angle):
        raise OptionsRejected("the angle carried digits")
    off_limits = _OFF_LIMITS_IN_ANGLE.search(angle)
    if off_limits:
        raise OptionsRejected(_off_limits_reason(off_limits.group(0)))
    return angle


def _tags(item: dict[str, Any]) -> tuple[str, ...]:
    raw = item.get("tags", [])
    if not isinstance(raw, list):
        raise OptionsRejected("tags must be a list")
    tags = []
    for tag in raw[:MAX_TAGS]:
        if not isinstance(tag, str) or not tag.strip() or len(tag.strip()) > MAX_TAG_CHARS:
            raise OptionsRejected("a tag was not a short word")
        if _DIGIT.search(tag):
            raise OptionsRejected("a tag carried digits")
        tags.append(tag.strip())
    return tuple(tags)


def _option_id(title: str, angle: str) -> str:
    return hashlib.sha256(f"{title}|{angle}".encode()).hexdigest()[:12]


def _build_option(item: dict[str, Any]) -> EmailOption:
    title = _text_field(item, "title", MAX_TITLE_CHARS)
    angle = _text_field(item, "angle", MAX_ANGLE_CHARS)
    off_limits = _OFF_LIMITS_IN_ANGLE.search(angle)
    if off_limits:
        raise OptionsRejected(_off_limits_reason(off_limits.group(0)))
    return EmailOption(
        option_id=_option_id(title, angle),
        title=title,
        summary=_text_field(item, "summary", MAX_SUMMARY_CHARS),
        why=_text_field(item, "why", MAX_WHY_CHARS),
        angle=angle,
        tags=_tags(item),
        recommended=item.get("recommended") is True,
    )


def parse_email_options(text: str) -> list[EmailOption]:
    try:
        parsed = json_object(text)
    except DraftRejected as exc:
        raise OptionsRejected(str(exc)) from exc
    items = parsed.get("options")
    if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
        raise OptionsRejected("options must be a list of objects")
    if not MIN_OPTIONS <= len(items) <= MAX_OPTIONS:
        raise OptionsRejected(f"expected {MIN_OPTIONS} to {MAX_OPTIONS} options")

    kept: list[EmailOption] = []
    first_problem = ""
    for item in items:
        try:
            option = _build_option(item)
        except OptionsRejected as exc:
            first_problem = first_problem or str(exc)
            logger.warning("email_option_dropped", reason=str(exc))
            continue
        if option.title.lower() in {kept_option.title.lower() for kept_option in kept}:
            raise OptionsRejected("two options had the same title")
        kept.append(option)
    if len(kept) < MIN_OPTIONS:
        raise OptionsRejected(first_problem or "too few usable options")
    pick = next((index for index, option in enumerate(kept) if option.recommended), 0)
    return [replace(option, recommended=index == pick) for index, option in enumerate(kept)]


def _with_feedback(prompt: str, reason: str) -> str:
    if not reason:
        return prompt
    return (
        f"{prompt}\n\nYOUR PREVIOUS REPLY WAS REFUSED: {reason}. "
        "Write the whole reply again with that fixed."
    )


def suggest_email_options(
    facts: AssistantFactBlock,
    llm_client: LLMClient,
    *,
    entity_id: str,
    audit: AuditSink,
    goal: str | None = None,
    approach: str | None = None,
    instructions: str | None = None,
) -> list[EmailOption]:
    payload = facts.to_dict()
    base_prompt = build_options_prompt(goal, approach, instructions)
    last_reason = ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        text = ""
        system = _with_feedback(base_prompt, last_reason)
        model_call = traced_model_call(
            as_model_call(llm_client, system=system),
            name=MODEL_SUGGEST_EMAIL_OPTIONS,
            llm_client=llm_client,
            system=system,
            metadata={"attempt": attempt},
        )
        try:
            text = run_model_boundary(payload, model_call, entity_id=entity_id, audit=audit)
            return parse_email_options(text)
        except (InboundLeak, OutboundLeak, LLMClientError) as exc:
            logger.warning("email_options_failed", entity_id=entity_id, reason=str(exc))
            raise OptionsRejected(str(exc)) from exc
        except OptionsRejected as exc:
            last_reason = str(exc)
            logger.warning(
                "email_options_rejected",
                entity_id=entity_id,
                attempt=attempt,
                reason=last_reason,
                reply=text[:600],
            )
    raise OptionsRejected(last_reason)
