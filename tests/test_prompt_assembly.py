"""Prompt assembly: three slots, the prohibitions, and the call brief.

The client's own figures must never appear in the prompt text, since only the
payload is scanned. That is asserted directly rather than left to the shape of
the code, because it is the property the whole boundary depends on.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date

import pytest

from app.agents.email_agent import (
    CAMPAIGN_PROHIBITIONS,
    build_system_prompt,
    conditional_prohibitions,
    has_required_placeholders,
    render_call_brief,
)
from app.agents.email_channel import EmailAgent
from app.agents.graph import ClientContext
from app.db.session import SessionLocal
from app.rules.catalog import load_active_angles

IN_FORCE = date(2026, 12, 15)


@dataclass(frozen=True)
class FakeBrief:
    headline: str = "Restart the standing order"
    who: str = "Five or more purchases on a tight cadence"
    claim: str = "A genuine, measurable savings rhythm that stopped"
    ask: str = "Resume the exact cadence they already had"
    never: str = "Never state an exact purchase count"
    use: str = "Reference the cadence placeholder if given; support with product info"


@dataclass(frozen=True)
class FakeContract:
    max_words: int = 120
    sign_off: str = "named relationship manager"
    secondary_channel: str | None = "call_brief"


FACTS = {
    "fund_name": "Cytonn Money Market Fund",
    "typical_contribution_kes": 150_000,
    "invested_every_n_days": 30,
    "cadence_band": "Tight",
    "years_since_exit": 2.5,
}


def _context_loader(*, brief, contract=None, facts=FACTS):
    def load(client_id: int, product: str) -> ClientContext:
        return ClientContext(
            raw_context={},
            angle=getattr(brief, "angle", "an_angle"),
            prompt_variant=getattr(brief, "angle", "an_angle"),
            chunks=(),
            brief=brief,
            contract=contract or FakeContract(secondary_channel=None),
            facts=facts,
        )

    return load


# --- the three slots ---


def test_the_angle_brief_reaches_the_prompt() -> None:
    prompt = build_system_prompt(angle="back_on_schedule", prompt_variant=None, brief=FakeBrief())
    assert "Restart the standing order" in prompt
    assert "A genuine, measurable savings rhythm that stopped" in prompt
    assert "Resume the exact cadence they already had" in prompt


def test_the_format_contract_reaches_the_prompt() -> None:
    prompt = build_system_prompt(
        angle="back_on_schedule", prompt_variant=None, contract=FakeContract()
    )
    assert "120 words" in prompt
    assert "named relationship manager" in prompt


def test_without_a_brief_the_prompt_is_what_it_always_was() -> None:
    """A caller that predates the catalogue keeps its old prompt exactly."""
    prompt = build_system_prompt(angle="winback_habit", prompt_variant="habit_standard")
    assert "Angle: winback_habit" in prompt


def test_the_client_figures_never_appear_in_the_prompt_text() -> None:
    """Only the payload is scanned, so a figure in the prompt would bypass it."""
    prompt = build_system_prompt(
        angle="back_on_schedule",
        prompt_variant=None,
        brief=FakeBrief(),
        contract=FakeContract(),
        facts=FACTS,
    )
    assert "150000" not in prompt
    assert "150,000" not in prompt
    assert "Cytonn Money Market Fund" not in prompt
    assert "2.5" not in prompt


def test_the_prompt_points_the_model_at_the_payload_for_figures() -> None:
    prompt = build_system_prompt(angle="a", prompt_variant=None, brief=FakeBrief(), facts=FACTS)
    assert "user message" in prompt


# --- campaign-wide prohibitions ---


@pytest.mark.parametrize("prohibition", CAMPAIGN_PROHIBITIONS)
def test_every_campaign_prohibition_rides_on_every_prompt(prohibition: str) -> None:
    with_brief = build_system_prompt(angle="a", prompt_variant=None, brief=FakeBrief())
    without_brief = build_system_prompt(angle="a", prompt_variant=None)
    assert prohibition in with_brief
    assert prohibition in without_brief


def test_the_five_campaign_prohibitions_cover_what_the_data_cannot_support() -> None:
    joined = " ".join(CAMPAIGN_PROHIBITIONS).lower()
    assert "how many times" in joined
    assert "balance" in joined
    assert "first invested" in joined
    assert "not in the facts" in joined
    assert "guarantee" in joined


# --- per-angle and conditional prohibitions ---


def test_the_angles_own_prohibition_reaches_the_prompt() -> None:
    prompt = build_system_prompt(angle="back_on_schedule", prompt_variant=None, brief=FakeBrief())
    assert "Never state an exact purchase count" in prompt


def test_no_cadence_forbids_referencing_a_rhythm() -> None:
    lines = conditional_prohibitions({"cadence_band": "None"})
    assert any("no measurable cadence" in line for line in lines)


def test_a_real_cadence_adds_no_such_prohibition() -> None:
    lines = conditional_prohibitions(FACTS)
    assert not any("no measurable cadence" in line for line in lines)


def test_stale_contact_adds_no_client_facing_instruction() -> None:
    """Stale contact is internal targeting context; the global rule against
    contact-verification openers covers the client-facing side instead."""
    lines = conditional_prohibitions({**FACTS, "stale_contact": True})
    assert not any("confirm" in line.lower() for line in lines)


def test_an_exit_that_was_not_a_choice_forbids_calling_it_one() -> None:
    lines = conditional_prohibitions({**FACTS, "exit_reason": "charge_settled"})
    assert any("decision to leave" in line for line in lines)
    assert any("settling to zero" in line for line in lines)


def test_a_client_who_chose_to_leave_adds_no_such_prohibition() -> None:
    lines = conditional_prohibitions({**FACTS, "exit_reason": "client_sale"})
    assert not any("decision to leave" in line for line in lines)


def test_conditional_prohibitions_reach_the_assembled_prompt() -> None:
    prompt = build_system_prompt(
        angle="a",
        prompt_variant=None,
        brief=FakeBrief(),
        facts={"cadence_band": "None", "exit_reason": "charge_settled"},
    )
    assert "no measurable cadence" in prompt
    assert "settling to zero" in prompt


# --- placeholders ---


def test_a_draft_given_the_fund_name_need_not_leave_it_a_placeholder() -> None:
    draft = "Dear {{first_name}}, your Cytonn Money Market Fund is waiting."
    assert has_required_placeholders(draft, FACTS) is True


def test_a_draft_without_facts_still_needs_both_placeholders() -> None:
    draft = "Dear {{first_name}}, your Cytonn Money Market Fund is waiting."
    assert has_required_placeholders(draft) is False


def test_the_client_name_stays_a_placeholder_either_way() -> None:
    draft = "Dear Jane, your {{fund_name}} is waiting."
    assert has_required_placeholders(draft) is False
    assert has_required_placeholders(draft, FACTS) is False


# --- the call brief ---


def test_the_call_brief_has_no_subject_line() -> None:
    rendered = render_call_brief(brief=FakeBrief(), facts=FACTS, contract=FakeContract())
    assert "subject" not in rendered.lower()


def test_the_call_brief_carries_the_same_brief_and_facts_as_the_email() -> None:
    rendered = render_call_brief(brief=FakeBrief(), facts=FACTS, contract=FakeContract())
    assert "Restart the standing order" in rendered
    assert "A genuine, measurable savings rhythm that stopped" in rendered
    # The brief is internal, so it may state the figures the email cites.
    assert "150000" in rendered
    assert "Cytonn Money Market Fund" in rendered


def test_the_call_brief_repeats_the_prohibitions() -> None:
    rendered = render_call_brief(brief=FakeBrief(), facts=FACTS, contract=FakeContract())
    assert "Never state an exact purchase count" in rendered
    assert CAMPAIGN_PROHIBITIONS[0] in rendered


def test_the_call_brief_names_who_it_is_for() -> None:
    rendered = render_call_brief(brief=FakeBrief(), facts=FACTS, contract=FakeContract())
    assert "named relationship manager" in rendered


# --- every catalogued angle assembles ---


def test_every_catalogued_angle_assembles_a_prompt_carrying_its_own_prohibition(
    db: None,
) -> None:
    with SessionLocal() as session:
        angles = load_active_angles(session, IN_FORCE)
    assert len(angles) == 12

    for angle, row in angles.items():
        prompt = build_system_prompt(
            angle=angle,
            prompt_variant=angle,
            brief=row,
            contract=FakeContract(),
            facts=FACTS,
        )
        assert row.never in prompt, f"{angle} lost its own prohibition"
        assert row.claim in prompt
        assert row.ask in prompt
        for prohibition in CAMPAIGN_PROHIBITIONS:
            assert prohibition in prompt


def test_an_unknown_angle_falls_back_rather_than_erroring() -> None:
    prompt = build_system_prompt(angle="no_such_angle", prompt_variant="no_such_variant")
    assert "Angle: no_such_angle" in prompt
    for prohibition in CAMPAIGN_PROHIBITIONS:
        assert prohibition in prompt


# --- every angle generates end to end ---


class ScriptedLLMClient:
    model = "scripted"

    def __init__(self, reply: str) -> None:
        self._reply = reply
        self.last_usage = None

    def generate(self, *, system: str, user: str) -> str:
        self.seen_system = system
        return self._reply


def test_every_catalogued_angle_generates_an_accepted_draft(db: None) -> None:
    """Twelve angles, twelve accepted drafts, through the real graph and guardrails."""
    with SessionLocal() as session:
        angles = load_active_angles(session, IN_FORCE)

    draft = json.dumps(
        {
            "subject": "A moment of your time",
            "body": (
                "Dear {{first_name}}, we would value hearing from you again "
                "about your Cytonn Money Market Fund whenever the timing suits."
            ),
        }
    )

    for angle, row in angles.items():
        agent = EmailAgent(
            context_loader=_context_loader(brief=row),
            llm_client=ScriptedLLMClient(draft),
        )
        state = agent.generate(client_id=1, product="money market")
        assert state["status"] == "accepted", (
            f"{angle} was rejected: {state.get('failed_guardrail')} {state.get('reason')}"
        )


def test_a_fabricated_figure_is_rejected_for_every_angle(db: None) -> None:
    """The same draft with one invented amount never reaches an accepted state."""
    with SessionLocal() as session:
        angles = load_active_angles(session, IN_FORCE)

    draft = json.dumps(
        {
            "subject": "A moment of your time",
            "body": (
                "Dear {{first_name}}, you usually invested around KES 987,654 "
                "in your Cytonn Money Market Fund. Shall we pick that back up?"
            ),
        }
    )

    for angle, row in angles.items():
        agent = EmailAgent(
            context_loader=_context_loader(brief=row),
            llm_client=ScriptedLLMClient(draft),
        )
        state = agent.generate(client_id=1, product="money market")
        assert state["status"] == "rejected", f"{angle} accepted a fabricated figure"
        assert state["failed_guardrail"] == "numeric_traceability"
