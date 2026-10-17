from __future__ import annotations

from datetime import date

import pytest

from app.agents.prompt_versioning import EMAIL_PURPOSE_KEYS, PROMPT_KEYS
from app.briefing.client_email import INSTRUCTIONS, build_email_prompt
from app.briefing.email_options import build_options_prompt
from app.briefing.email_prompts import default_email_prompts, email_prompt_text, purpose_key
from app.briefing.next_action import EMAIL_PURPOSES
from app.db.session import SessionLocal


def test_every_default_email_prompt_is_a_known_studio_prompt() -> None:
    defaults = default_email_prompts()

    assert set(defaults) <= set(PROMPT_KEYS)
    assert set(EMAIL_PURPOSE_KEYS) == {purpose_key(code) for code in EMAIL_PURPOSES}
    assert all(text.strip() for text in defaults.values())


def test_the_studio_text_replaces_the_built_in_instructions() -> None:
    prompt = build_email_prompt({"fund_name": "Money Market Fund"}, instructions="STUDIO RULES")

    assert prompt.startswith("STUDIO RULES")
    assert INSTRUCTIONS not in prompt


def test_the_studio_purpose_replaces_the_built_in_one_but_an_angle_wins() -> None:
    facts = {"fund_name": "Money Market Fund"}

    from_studio = build_email_prompt(facts, purpose="review_goals", purpose_text="STUDIO PURPOSE")
    from_angle = build_email_prompt(
        facts, purpose="review_goals", purpose_text="STUDIO PURPOSE", angle="THE ANGLE"
    )

    assert "STUDIO PURPOSE" in from_studio
    assert EMAIL_PURPOSES["review_goals"] not in from_studio
    assert "THE ANGLE" in from_angle and "STUDIO PURPOSE" not in from_angle


def test_the_options_prompt_takes_the_studio_text() -> None:
    assert build_options_prompt(instructions="STUDIO OPTIONS").startswith("STUDIO OPTIONS")


@pytest.mark.parametrize("key", sorted(default_email_prompts()))
def test_with_no_live_studio_version_the_built_in_text_is_used(db, key) -> None:
    with SessionLocal() as session:
        text = email_prompt_text(session, key, date(2019, 1, 1))

    assert text == default_email_prompts()[key]
