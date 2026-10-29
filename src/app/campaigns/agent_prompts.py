from __future__ import annotations

import re
from datetime import date

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.agents.campaign_prompt import DEFAULTS, PLACEHOLDERS
from app.db.models.campaign_agent_prompt import CampaignAgentPrompt

COMPONENT_TYPE = "campaign_agent_prompt"
PROMPT_KEYS = tuple(DEFAULTS)
MAX_PROMPT_LENGTH = 20_000
TOKEN = re.compile(r"\{([a-z_]+)\}")


class CampaignPromptError(ValueError):
    pass


def validate_campaign_prompt(prompt_name: str, template: object) -> None:
    if prompt_name not in PROMPT_KEYS:
        raise CampaignPromptError(f"'{prompt_name}' is not a campaign agent prompt")
    if not isinstance(template, str) or not template.strip():
        raise CampaignPromptError(f"'{prompt_name}' needs some text")
    if len(template) > MAX_PROMPT_LENGTH:
        raise CampaignPromptError(f"'{prompt_name}' is longer than {MAX_PROMPT_LENGTH} characters")
    found = set(TOKEN.findall(template))
    expected = PLACEHOLDERS[prompt_name]
    if found != expected:
        raise CampaignPromptError(
            f"'{prompt_name}' must keep these fields exactly: {sorted(expected)}; "
            f"it has {sorted(found)}"
        )


def active_prompt(session: Session, prompt_name: str, as_of: date) -> CampaignAgentPrompt | None:
    return session.scalar(
        select(CampaignAgentPrompt)
        .where(
            CampaignAgentPrompt.prompt_name == prompt_name,
            CampaignAgentPrompt.valid_from <= as_of,
            or_(CampaignAgentPrompt.valid_to.is_(None), CampaignAgentPrompt.valid_to > as_of),
        )
        .order_by(CampaignAgentPrompt.valid_from.desc(), CampaignAgentPrompt.version.desc())
        .limit(1)
    )


def campaign_prompt_text(session: Session, prompt_name: str, as_of: date) -> str:
    row = active_prompt(session, prompt_name, as_of)
    if row is not None and row.template.strip():
        return row.template
    return DEFAULTS[prompt_name]


def campaign_prompt_texts(session: Session, as_of: date) -> dict[str, str]:
    return {key: campaign_prompt_text(session, key, as_of) for key in PROMPT_KEYS}
