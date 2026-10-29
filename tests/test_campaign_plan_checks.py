from __future__ import annotations

import pytest

from app.agents.campaign_plan_checks import (
    MIN_FACT_LENGTH,
    FactSource,
    PlanRules,
    check_decisions,
    resolve_cited_facts,
    subjects_too_close,
)
from app.agents.campaign_prompt import (
    INSTRUCTIONS,
    NO_OBJECTIVE_BRIEF,
    OBJECTIVE_BRIEFS,
    SHARED_RULES,
    build_plan_prompt,
)
from app.schemas.campaigns import CampaignPlanConfig

RULES = PlanRules(
    allowed_angles={"a", "b", "c", "d"},
    angle_families={"a": "habit", "b": "habit", "c": "value", "d": None},
    min_gap_days=2,
    max_gap_days=10,
)
WEB = FactSource(
    text="Money market funds can usually be redeemed within one working day.",
    url="https://example.com/redeem",
    retrieved_at="2026-10-28T10:00:00+00:00",
    tool="search_web",
)
RATE = FactSource(
    text="The money market fund's current rate is 12.3%, verified as of 2026-10-27.",
    url=None,
    retrieved_at="2026-10-28T10:01:00+00:00",
    tool="get_verified_rate",
)


def _slot(**fields) -> dict:
    return {
        "slot": 0,
        "prior_angle": None,
        "earlier_angles": [],
        "earlier_subjects": [],
        **fields,
    }


def _decision(**fields) -> dict:
    return {
        "slot": 0,
        "angle": "c",
        "gap_days": 4,
        "reason": "a fresh approach",
        "next_time": "check if they opened",
        **fields,
    }


def _check(decision: dict | list[dict], slot: dict | None = None, sources=()):
    decisions = decision if isinstance(decision, list) else [decision]
    slots = {0: slot or _slot()}
    return check_decisions(decisions, slots, RULES, list(sources))


def _codes(errors) -> list[str]:
    return [error.code for error in errors]


def test_a_complete_decision_has_no_errors() -> None:
    errors, facts = _check(_decision())

    assert errors == []
    assert facts == {0: []}


def test_a_slot_that_was_not_given_is_refused() -> None:
    errors, _ = _check(_decision(slot=7))

    assert _codes(errors)[0] == "unknown_slot"


def test_a_slot_decided_twice_is_refused() -> None:
    errors, _ = _check([_decision(), _decision()])

    assert _codes(errors) == ["duplicate_slot"]


def test_a_slot_left_out_makes_the_batch_incomplete() -> None:
    slots = {0: _slot(), 1: _slot(slot=1)}

    errors, _ = check_decisions([_decision()], slots, RULES, [])

    assert _codes(errors) == ["incomplete"]
    assert "[1]" in errors[0].message


def test_an_angle_outside_the_open_menu_is_refused() -> None:
    errors, _ = _check(_decision(angle="zzz"))

    assert _codes(errors) == ["unknown_angle"]


def test_the_angle_sent_last_time_is_refused() -> None:
    errors, _ = _check(_decision(angle="c"), _slot(prior_angle="c", earlier_angles=["c"]))

    assert _codes(errors) == ["repeats_last_angle"]


def test_an_angle_used_at_any_earlier_touch_is_refused() -> None:
    slot = _slot(prior_angle="d", earlier_angles=["c", "d"])

    errors, _ = _check(_decision(angle="c"), slot)

    assert _codes(errors) == ["repeats_earlier_angle"]


def test_an_angle_from_the_same_family_as_an_earlier_one_is_refused() -> None:
    slot = _slot(prior_angle="a", earlier_angles=["a"])

    errors, _ = _check(_decision(angle="b"), slot)

    assert _codes(errors) == ["repeats_earlier_angle"]
    assert "same family" in errors[0].message


def test_an_angle_with_no_family_is_only_a_repeat_of_itself() -> None:
    slot = _slot(prior_angle="c", earlier_angles=["c"])

    errors, _ = _check(_decision(angle="d"), slot)

    assert errors == []


@pytest.mark.parametrize("gap", [1, 11, "5", None, True])
def test_a_gap_outside_the_policy_is_refused(gap) -> None:
    errors, _ = _check(_decision(gap_days=gap))

    assert _codes(errors) == ["gap_out_of_range"]


@pytest.mark.parametrize(
    "field,code", [("reason", "missing_reason"), ("next_time", "missing_next_time")]
)
@pytest.mark.parametrize("blank", [None, "", "   "])
def test_a_decision_needs_a_reason_and_a_next_time_note(field: str, code: str, blank) -> None:
    errors, _ = _check(_decision(**{field: blank}))

    assert _codes(errors) == [code]


def test_a_subject_too_close_to_an_earlier_one_is_refused() -> None:
    slot = _slot(earlier_subjects=["Your savings plan is waiting"])

    errors, _ = _check(_decision(suggested_subject="Your savings plan is waiting for you"), slot)

    assert _codes(errors) == ["subject_too_close"]


def test_a_different_subject_is_accepted() -> None:
    slot = _slot(earlier_subjects=["Your savings plan is waiting"])

    errors, _ = _check(_decision(suggested_subject="A small step that adds up"), slot)

    assert errors == []


def test_subjects_ignore_case_and_punctuation() -> None:
    assert subjects_too_close("Come back, we miss you!", "come back we miss you")
    assert not subjects_too_close("Come back", "")


def test_a_fact_that_a_tool_returned_is_kept_with_its_source() -> None:
    note = "money market funds can usually be redeemed   within one working day."

    errors, facts = _check(_decision(research_notes=[note]), sources=[WEB])

    assert errors == []
    assert facts[0] == [
        {
            "text": "money market funds can usually be redeemed within one working day.",
            "url": "https://example.com/redeem",
            "retrieved_at": "2026-10-28T10:00:00+00:00",
            "tool": "search_web",
        }
    ]


def test_a_part_of_a_returned_sentence_can_be_cited() -> None:
    cited, unsupported = resolve_cited_facts(["current rate is 12.3%, verified"], [RATE])

    assert unsupported == []
    assert cited[0]["tool"] == "get_verified_rate"
    assert cited[0]["url"] is None


def test_a_fact_no_tool_returned_is_refused() -> None:
    errors, _ = _check(
        _decision(research_notes=["Cytonn funds are guaranteed to grow every year."]),
        sources=[WEB, RATE],
    )

    assert _codes(errors) == ["unsupported_fact"]


def test_a_fact_with_no_tool_calls_at_all_is_refused() -> None:
    errors, _ = _check(_decision(research_notes=["Rates are at an all time high."]))

    assert _codes(errors) == ["unsupported_fact"]


def test_a_fragment_too_short_to_check_is_refused() -> None:
    short = "12.3%"
    assert len(short) < MIN_FACT_LENGTH

    errors, _ = _check(_decision(research_notes=[short]), sources=[RATE])

    assert _codes(errors) == ["unsupported_fact"]


def test_every_problem_comes_back_in_one_answer() -> None:
    slot = _slot(prior_angle="c", earlier_angles=["c"])
    bad = _decision(
        angle="c", gap_days=99, reason="", next_time="", research_notes=["made up fact here"]
    )

    errors, _ = _check(bad, slot)

    assert _codes(errors) == [
        "repeats_last_angle",
        "gap_out_of_range",
        "missing_reason",
        "missing_next_time",
        "unsupported_fact",
    ]


@pytest.mark.parametrize("objective", sorted(OBJECTIVE_BRIEFS))
def test_the_prompt_follows_the_campaigns_objective(objective: str) -> None:
    config = CampaignPlanConfig(
        objective=objective, min_gap_days=3, max_gap_days=9, observation_days=21
    )

    prompt = build_plan_prompt(INSTRUCTIONS, config)

    assert prompt.startswith(INSTRUCTIONS.strip())
    assert OBJECTIVE_BRIEFS[objective] in prompt
    assert "between 3 and 9" in prompt
    assert "within 21 days" in prompt
    assert all(rule in prompt for rule in SHARED_RULES)
    assert not any(brief in prompt for name, brief in OBJECTIVE_BRIEFS.items() if name != objective)


def test_a_campaign_with_no_objective_gets_the_neutral_brief() -> None:
    prompt = build_plan_prompt("Base text.", CampaignPlanConfig())

    assert NO_OBJECTIVE_BRIEF in prompt
    assert all(rule in prompt for rule in SHARED_RULES)
