from __future__ import annotations

from datetime import date
from string import Formatter

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.db.models.agent_prompt import AgentPrompt

AGENT_LOOP_PLAN = "agent_loop_plan"
AGENT_LOOP_CHOOSE = "agent_loop_choose"
ACTION_AGENT_CHOOSE = "action_agent_choose"
EMAIL_BASE_INSTRUCTIONS = "email_base_instructions"
SMS_BASE_INSTRUCTIONS = "sms_base_instructions"
INTELLIGENCE_INVESTIGATION = "intelligence_investigation"

PROMPT_KEYS = (
    AGENT_LOOP_PLAN,
    AGENT_LOOP_CHOOSE,
    ACTION_AGENT_CHOOSE,
    EMAIL_BASE_INSTRUCTIONS,
    SMS_BASE_INSTRUCTIONS,
    INTELLIGENCE_INVESTIGATION,
)

PROMPT_PLACEHOLDERS: dict[str, frozenset[str]] = {
    AGENT_LOOP_PLAN: frozenset({"as_of"}),
    AGENT_LOOP_CHOOSE: frozenset({"as_of", "plan_text", "groups", "menu", "tool_name"}),
    ACTION_AGENT_CHOOSE: frozenset(
        {
            "as_of",
            "title",
            "kind",
            "group_name",
            "client_count",
            "money",
            "why_now",
            "suggestion",
            "confidence",
            "confidence_reason",
            "avoid",
            "menu",
            "tool_name",
        }
    ),
    EMAIL_BASE_INSTRUCTIONS: frozenset(),
    SMS_BASE_INSTRUCTIONS: frozenset(),
    INTELLIGENCE_INVESTIGATION: frozenset(
        {
            "as_of",
            "group_name",
            "question",
            "size_lines",
            "last_contact_line",
            "past_findings_lines",
            "waiting_on_a_person",
            "field_names",
            "measures",
        }
    ),
}

FORMATTED_PROMPTS = frozenset(
    {AGENT_LOOP_PLAN, AGENT_LOOP_CHOOSE, ACTION_AGENT_CHOOSE, INTELLIGENCE_INVESTIGATION}
)


class PromptValidationError(ValueError):
    pass


def template_placeholders(template: str) -> set[str]:
    try:
        parsed = list(Formatter().parse(template))
    except ValueError as error:
        raise PromptValidationError(f"template is not valid: {error}") from None
    return {
        field_name for _, field_name, _, _ in parsed if field_name is not None and field_name != ""
    }


def validate_prompt_placeholders(prompt_name: str, template: str) -> None:
    if prompt_name not in PROMPT_PLACEHOLDERS:
        raise PromptValidationError(f"'{prompt_name}' is not a known prompt")
    if prompt_name not in FORMATTED_PROMPTS:
        return
    expected = PROMPT_PLACEHOLDERS[prompt_name]
    found = template_placeholders(template)
    missing = expected - found
    extra = found - expected
    if missing or extra:
        raise PromptValidationError(
            f"'{prompt_name}' template placeholders do not match: "
            f"missing {sorted(missing)}, unexpected {sorted(extra)}"
        )


def active_prompt(session: Session, prompt_key: str, as_of: date) -> AgentPrompt | None:
    return session.scalar(
        select(AgentPrompt)
        .where(
            AgentPrompt.prompt_name == prompt_key,
            AgentPrompt.valid_from <= as_of,
            or_(AgentPrompt.valid_to.is_(None), AgentPrompt.valid_to > as_of),
        )
        .order_by(AgentPrompt.valid_from.desc(), AgentPrompt.version.desc())
        .limit(1)
    )
