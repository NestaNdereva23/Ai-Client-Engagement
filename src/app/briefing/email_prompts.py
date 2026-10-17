from __future__ import annotations

from datetime import date

from sqlalchemy.orm import Session

from app.agents.prompt_versioning import (
    CLIENT_EMAIL_DRAFT,
    EMAIL_OPTIONS_SUGGEST,
    EMAIL_PURPOSE_PREFIX,
    active_prompt,
)


def purpose_key(purpose: str) -> str:
    return f"{EMAIL_PURPOSE_PREFIX}{purpose}"


def default_email_prompts() -> dict[str, str]:
    from app.briefing.client_email import INSTRUCTIONS as DRAFT_INSTRUCTIONS
    from app.briefing.email_options import INSTRUCTIONS as OPTIONS_INSTRUCTIONS
    from app.briefing.next_action import EMAIL_PURPOSES

    return {
        CLIENT_EMAIL_DRAFT: DRAFT_INSTRUCTIONS,
        EMAIL_OPTIONS_SUGGEST: OPTIONS_INSTRUCTIONS,
        **{purpose_key(code): text for code, text in EMAIL_PURPOSES.items()},
    }


def email_prompt_text(session: Session, key: str, as_of: date) -> str:
    row = active_prompt(session, key, as_of)
    if row is not None and row.template.strip():
        return row.template
    return default_email_prompts()[key]
