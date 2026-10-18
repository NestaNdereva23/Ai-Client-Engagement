from __future__ import annotations

import json
from datetime import date

import pytest

from app.agents.prompt_versioning import (
    EMAIL_PURPOSE_KEYS,
    PROMPT_KEYS,
    default_prompt_texts,
    prompt_text,
)
from app.briefing.client_email import (
    INSTRUCTIONS,
    DraftRejected,
    build_email_prompt,
    draft_client_email,
)
from app.briefing.email_options import build_options_prompt
from app.briefing.email_prompts import default_email_prompts, purpose_key
from app.briefing.next_action import EMAIL_PURPOSES
from app.db.session import SessionLocal
from app.privacy.fact_block import AssistantFactBlock


class _WriterStub:
    model = "writer-stub"

    def __init__(self, body: str) -> None:
        self.reply = json.dumps({"subject": "Checking in", "body": body})
        self.seen: list[str] = []

    def generate(self, *, system: str, user: str) -> str:
        self.seen.extend([system, user])
        return self.reply


def _write(body: str, facts: AssistantFactBlock | None = None, **kwargs):
    facts = facts or AssistantFactBlock(fund_name="Cytonn Money Market Fund", recency_band="<=1m")
    llm = _WriterStub(f"Dear {{{{first_name}}}},\n\n{body}")
    draft = draft_client_email(facts, llm, entity_id="1", audit=lambda record: None, **kwargs)
    return draft, llm


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


def test_a_chosen_option_draft_that_hints_at_past_activity_is_refused() -> None:
    body = "Growth builds up without highlighting specific recent transactions. A call?"

    with pytest.raises(DraftRejected, match="the word 'recent'"):
        _write(body, angle="Talk about steady growth.")


def test_the_fund_name_does_not_trip_the_word_check() -> None:
    draft, llm = _write(
        "Your Cytonn Money Market Fund keeps working for you. Would you like a call?",
        angle="Talk about steady growth.",
    )

    assert "Money Market Fund" in draft.body
    assert "Never use these words" in llm.seen[0]


def test_the_writer_gets_a_tone_cue_and_the_contact_but_no_withdrawal_or_risk_facts() -> None:
    facts = AssistantFactBlock(
        fund_name="Cytonn Money Market Fund",
        recency_band="<=1m",
        risk_band="Watch",
        sig_heavy_withdrawal=True,
        largest_withdrawal_size_band="large",
        last_withdrawal_recency_band="this_week",
        deposit_rhythm_band="weekly",
        last_contact_channel="call",
        last_contact_outcome="follow_up",
        last_contact_age_band="this_month",
        missed_meeting=True,
    )

    _, llm = _write("Would you like a short call?", facts, instructions="STUDIO RULES")

    system, everything = llm.seen[0], " ".join(llm.seen)
    assert "Be gentle and unhurried. Do not ask them to add money." in system
    assert "Our last contact with them was a phone call this month, and we agreed" in system
    assert "A meeting we had planned with them did not take place" in system
    for hidden in ("withdraw", "risk", "Watch", "weekly", "recency", "<=1m", "large", "this_week"):
        assert hidden not in everything


def test_a_client_never_contacted_adds_nothing_about_contact() -> None:
    facts = AssistantFactBlock(
        fund_name="Cytonn Money Market Fund",
        last_contact_channel="none",
        last_contact_outcome="none",
        last_contact_age_band="never",
    )

    _, llm = _write("Would you like a short call?", facts, instructions="STUDIO RULES")

    assert "contact" not in " ".join(llm.seen).lower()


def test_the_checks_are_added_even_when_the_studio_text_leaves_the_rules_out() -> None:
    _, llm = _write("Would you like a short call?", instructions="STUDIO RULES")

    assert "Write no digits at all" in llm.seen[0]
    assert "{{first_name}} and {{fund_name}}" in llm.seen[0]


def test_a_purpose_draft_may_use_the_word_its_own_purpose_uses() -> None:
    draft, llm = _write(
        "Has anything changed for you lately? Would you like a call?", purpose="ask_what_changed"
    )

    assert "lately" in draft.body
    assert "Never use these words" not in llm.seen[0]


def test_the_options_prompt_takes_the_studio_text() -> None:
    assert build_options_prompt(instructions="STUDIO OPTIONS").startswith("STUDIO OPTIONS")


@pytest.mark.parametrize("key", sorted(default_prompt_texts()))
def test_with_no_live_studio_version_the_built_in_text_is_used(db, key) -> None:
    with SessionLocal() as session:
        text = prompt_text(session, key, date(2019, 1, 1))

    assert text == default_prompt_texts()[key]
