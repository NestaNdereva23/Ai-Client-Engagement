from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.agents.situations import (
    APPROACHING_PRESERVATION_LINE,
    DEPOSIT_SIZE_FALLING,
    FREQUENT_DEPOSITOR_ONE_FUND,
    FREQUENT_DEPOSITOR_WITHDRAWING,
    GRADUAL_DEPOSITOR_WITHDRAWING,
    RISK_ROSE_NO_CALL,
)
from app.briefing.client_email import build_email_prompt
from app.briefing.next_action import (
    CALL,
    EMAIL,
    LoggedContact,
    NextActionFacts,
    OpenTouchpoint,
    decide,
)

TODAY = date(2026, 10, 15)
QUIET_DAYS = 7


def _facts(**fields) -> NextActionFacts:
    base = {
        "situations": (),
        "planned": None,
        "missed": None,
        "last_contact": None,
        "can_email": True,
    }
    return NextActionFacts(**{**base, **fields})


def _day(days_ago: int) -> date:
    return TODAY - timedelta(days=days_ago)


def test_a_missed_meeting_gets_a_follow_up_email() -> None:
    missed = OpenTouchpoint(41, "meeting", _day(1))

    decision = decide(_facts(missed=missed), TODAY, QUIET_DAYS)

    assert decision.note is None
    action = decision.action
    assert (action.code, action.title, action.kind) == (
        "missed_meeting:41",
        "Follow up on missed meeting",
        EMAIL,
    )
    assert action.why == "The meeting due yesterday is still open."
    assert action.email_purpose == "reschedule_meeting"


def test_a_missed_meeting_becomes_a_call_when_the_client_cannot_be_emailed() -> None:
    missed = OpenTouchpoint(41, "meeting", _day(1))

    action = decide(_facts(missed=missed, can_email=False), TODAY, QUIET_DAYS).action

    assert (action.kind, action.email_purpose) == (CALL, None)


def test_a_missed_call_gets_a_call() -> None:
    action = decide(_facts(missed=OpenTouchpoint(7, "call", _day(3))), TODAY, QUIET_DAYS).action

    assert (action.code, action.kind) == ("missed_call:7", CALL)
    assert action.why == "The call due 3 days ago is still open."


def test_a_contact_logged_since_the_missed_meeting_clears_it() -> None:
    missed = OpenTouchpoint(41, "meeting", _day(3))
    contact = LoggedContact("call_logged", "connected", _day(2))

    decision = decide(_facts(missed=missed, last_contact=contact), TODAY, QUIET_DAYS)

    assert decision.action is None
    assert decision.note == "Called 2 days ago"


def test_something_already_planned_means_no_action() -> None:
    missed = OpenTouchpoint(41, "meeting", _day(2))
    planned = OpenTouchpoint(42, "call", TODAY + timedelta(days=5))

    decision = decide(_facts(missed=missed, planned=planned), TODAY, QUIET_DAYS)

    assert decision.action is None
    assert decision.note == "Call planned for 20 October"


def test_an_unanswered_attempt_is_tried_again_the_next_day_not_the_same_day() -> None:
    yesterday = LoggedContact("call_logged", "no_answer", _day(1))
    today = LoggedContact("call_logged", "no_answer", TODAY)

    again = decide(_facts(last_contact=yesterday), TODAY, QUIET_DAYS)
    same_day = decide(_facts(last_contact=today), TODAY, QUIET_DAYS)

    assert again.action.title == "Try them again"
    assert again.action.code == f"no_answer:{_day(1).isoformat()}"
    assert same_day.action is None
    assert same_day.note == "Called today"


def test_a_recent_contact_keeps_the_client_quiet_until_the_quiet_days_pass() -> None:
    situations = (GRADUAL_DEPOSITOR_WITHDRAWING,)
    recent = LoggedContact("meeting_logged", "connected", _day(6))
    old = LoggedContact("meeting_logged", "connected", _day(7))

    quiet = decide(_facts(situations=situations, last_contact=recent), TODAY, QUIET_DAYS)
    due = decide(_facts(situations=situations, last_contact=old), TODAY, QUIET_DAYS)

    assert (quiet.action, quiet.note) == (None, "Met 6 days ago")
    assert due.action.title == "Review their cash needs"


def test_the_situation_with_the_higher_group_decides_the_action() -> None:
    situations = (FREQUENT_DEPOSITOR_ONE_FUND, DEPOSIT_SIZE_FALLING, RISK_ROSE_NO_CALL)

    action = decide(_facts(situations=situations), TODAY, QUIET_DAYS).action

    assert action.code == RISK_ROSE_NO_CALL
    assert action.kind == CALL


def test_a_client_in_no_situation_has_no_action() -> None:
    assert decide(_facts(situations=("single_fund_healthy",)), TODAY, QUIET_DAYS).action is None


def test_an_email_action_turns_into_a_call_when_the_client_cannot_be_emailed() -> None:
    facts = _facts(situations=(DEPOSIT_SIZE_FALLING,), can_email=False)

    action = decide(facts, TODAY, QUIET_DAYS).action

    assert (action.kind, action.email_purpose) == (CALL, None)


def test_a_client_near_the_preservation_line_is_called_but_an_email_encourages_top_ups() -> None:
    facts = _facts(situations=(APPROACHING_PRESERVATION_LINE,))

    action = decide(facts, TODAY, QUIET_DAYS).action
    prompt = build_email_prompt({"fund_name": "Money Market Fund"}, action.email_purpose)

    assert (action.kind, action.email_purpose) == (CALL, "encourage_top_up")
    assert "encourage them to keep adding" in prompt
    assert "Never mention a balance, a line" in prompt


@pytest.mark.parametrize(
    ("situation", "purpose"),
    [
        (FREQUENT_DEPOSITOR_WITHDRAWING, "grow_by_staying_invested"),
        (GRADUAL_DEPOSITOR_WITHDRAWING, "review_goals"),
    ],
)
def test_an_email_to_a_withdrawing_client_never_talks_about_money_moving(
    situation: str, purpose: str
) -> None:
    action = decide(_facts(situations=(situation,)), TODAY, QUIET_DAYS).action
    prompt = build_email_prompt({"fund_name": "Money Market Fund"}, action.email_purpose)

    assert action.email_purpose == purpose
    assert "Do not mention any deposit, withdrawal" in prompt
    assert "Do not say you noticed a deposit" in prompt


def test_a_given_reason_replaces_the_default_wording() -> None:
    facts = _facts(situations=(GRADUAL_DEPOSITOR_WITHDRAWING,))

    action = decide(facts, TODAY, QUIET_DAYS, "Took out KSh 660K.").action

    assert action.why == "Took out KSh 660K."
