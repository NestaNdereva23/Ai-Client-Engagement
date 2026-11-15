from __future__ import annotations

import json

import pytest

from app.briefing.email_options import (
    OptionsRejected,
    build_options_prompt,
    check_angle,
    parse_email_options,
    suggest_email_options,
)
from app.briefing.off_limits import OFF_LIMITS_WORDS, find_off_limits
from app.privacy.fact_block import AssistantFactBlock


def _item(title="A warm thank you", **changes) -> dict:
    base = {
        "title": title,
        "summary": "The email thanks the client for investing and invites a chat.",
        "why": "The client invests steadily and has not been contacted.",
        "angle": "Write warmly and thank them for investing. Invite a chat about their plans.",
        "tags": ["warm", "short"],
        "recommended": False,
    }
    return {**base, **changes}


def _reply(*items: dict) -> str:
    return json.dumps({"options": list(items)})


def _three(**first) -> str:
    return _reply(
        _item("A warm thank you", **first),
        _item("Offer a goals chat"),
        _item("A light check in"),
    )


class _ScriptedLLM:
    model = "options-stub"

    def __init__(self, *replies: str) -> None:
        self.replies = list(replies)
        self.seen: list[str] = []

    def generate(self, *, system: str, user: str) -> str:
        self.seen.extend([system, user])
        return self.replies.pop(0)


def _facts() -> AssistantFactBlock:
    return AssistantFactBlock(
        risk_band="Watch",
        deposit_rhythm_band="every_two_months",
        largest_withdrawal_size_band="large",
        sig_heavy_withdrawal=True,
    )


def test_a_good_reply_becomes_options_with_one_recommended() -> None:
    options = parse_email_options(_three(recommended=True))

    assert [option.title for option in options] == [
        "A warm thank you",
        "Offer a goals chat",
        "A light check in",
    ]
    assert [option.recommended for option in options] == [True, False, False]
    assert all(option.option_id for option in options)
    assert len({option.option_id for option in options}) == 3


def test_when_nothing_is_recommended_the_first_option_is() -> None:
    options = parse_email_options(_three())

    assert [option.recommended for option in options] == [True, False, False]


def test_when_two_are_recommended_only_the_first_flagged_one_is_kept() -> None:
    reply = _reply(
        _item("One"),
        _item("Two", recommended=True),
        _item("Three", recommended=True),
    )

    options = parse_email_options(reply)

    assert [option.recommended for option in options] == [False, True, False]


@pytest.mark.parametrize(
    "reply",
    [
        "not json at all",
        json.dumps({"options": "nope"}),
        json.dumps({"options": [_item()]}),
        _reply(*[_item(f"Option {name}") for name in "ABCDE"]),
        _reply(_item("Same"), _item("same")),
        _reply(_item("One", angle="Mention their recent withdrawal gently."), _item("Two")),
        _reply(_item("One", angle="Remind them their balance is healthy."), _item("Two")),
        _reply(_item("One", angle="Say the market has been kind lately."), _item("Two")),
        _reply(_item("One", angle="Thank them for being a top tier client."), _item("Two")),
        _reply(_item("One", angle="Ask how their plans are coming along lately."), _item("Two")),
        _reply(_item("One", angle="Thank them for their activity with us."), _item("Two")),
        _reply(_item("One", angle="Praise their latest transaction."), _item("Two")),
        _reply(_item("One", angle="Praise their recent saving."), _item("Two")),
        _reply(_item("One", angle="Keep the tone light, without pressure."), _item("Two")),
        _reply(_item("One", angle="Avoid pushing for a call."), _item("Two")),
        _reply(_item("One", angle="Do not rush them."), _item("Two")),
        _reply(_item("One", angle="Don't push."), _item("Two")),
        _reply(_item("One", summary="They deposited 200 last month."), _item("Two")),
        _reply(_item(title="x" * 71), _item("Two")),
        _reply(_item("One", why=""), _item("Two")),
        _reply(_item("One", tags="warm"), _item("Two")),
    ],
)
def test_a_reply_that_breaks_a_rule_is_rejected(reply) -> None:
    with pytest.raises(OptionsRejected):
        parse_email_options(reply)


def test_one_bad_option_is_dropped_when_enough_good_ones_remain() -> None:
    reply = _reply(
        _item("One", angle="Say the market has been kind."),
        _item("Two", recommended=True),
        _item("Three"),
    )

    options = parse_email_options(reply)

    assert [option.title for option in options] == ["Two", "Three"]
    assert [option.recommended for option in options] == [True, False]


def test_when_the_recommended_option_is_dropped_the_first_kept_one_takes_over() -> None:
    reply = _reply(
        _item("One", recommended=True, angle="Remind them of their balance."),
        _item("Two"),
        _item("Three"),
    )

    options = parse_email_options(reply)

    assert [option.recommended for option in options] == [True, False]
    assert options[0].title == "Two"


def test_the_withdrawal_may_be_named_in_why_but_never_in_the_angle() -> None:
    reply = _reply(
        _item("One", why="They made a large withdrawal this week, so keep it warm."),
        _item("Two"),
    )

    options = parse_email_options(reply)

    assert "withdrawal" in options[0].why


def test_an_angle_that_hides_a_topic_is_refused_when_the_option_is_chosen() -> None:
    leaky = (
        "Invite the client to consider how regular contributions can work alongside what they "
        "already own. Mention that staying invested allows interest to accumulate naturally "
        "without drawing attention to any recent transactions."
    )

    with pytest.raises(OptionsRejected):
        check_angle(leaky)


def test_the_fund_name_may_appear_in_an_angle() -> None:
    reply = _reply(
        _item("One", angle="Thank them warmly for investing in the Money Market Fund."),
        _item("Two"),
    )

    assert [option.title for option in parse_email_options(reply)] == ["One", "Two"]


@pytest.mark.parametrize("word", OFF_LIMITS_WORDS.split(", "))
def test_every_word_the_prompt_lists_is_one_the_check_catches(word) -> None:
    assert find_off_limits(f"Talk about the {word}.") is not None


def test_the_prompt_tells_the_model_what_the_check_refuses() -> None:
    prompt = build_options_prompt(instructions="STUDIO OPTIONS")

    assert "transaction" in prompt
    assert "'without'" in prompt
    assert "recency_band is how long" in prompt


def test_the_prompt_carries_the_team_guidance_only_when_given() -> None:
    bare = build_options_prompt()
    guided = build_options_prompt("Turn activity into growth", "Show interest earned")

    assert "GUIDANCE FROM THE TEAM" not in bare
    assert "Turn activity into growth" in guided
    assert "Show interest earned" in guided


def test_the_model_sees_only_bands_and_the_audit_records_the_crossing() -> None:
    llm = _ScriptedLLM(_three())
    crossings = []

    suggest_email_options(_facts(), llm, entity_id="85970", audit=crossings.append)

    sent = " ".join(llm.seen)
    assert "largest_withdrawal_size_band: large" in sent
    assert "85970" not in sent
    assert len(crossings) == 1
    assert crossings[0].inbound == "pass"


def test_one_bad_reply_is_retried_and_told_why() -> None:
    bad = _reply(_item("One", angle="Say the market has been kind."), _item("Two"))
    llm = _ScriptedLLM(bad, _three())

    options = suggest_email_options(_facts(), llm, entity_id="1", audit=lambda record: None)

    assert llm.replies == []
    assert [option.title for option in options][0] == "Two"
    first_system, second_system = llm.seen[0], llm.seen[2]
    assert "REFUSED" not in first_system
    assert "REFUSED" in second_system
    assert "option 'One': an angle used the word 'market'" in second_system


def test_the_retry_is_told_which_option_failed_and_every_problem() -> None:
    bad = _reply(
        _item("One", angle="Say the market has been kind."),
        _item("Two", angle="Keep it light, without pressure."),
        _item("Three", why="No contact for 90 days."),
    )
    llm = _ScriptedLLM(bad, _three())

    suggest_email_options(_facts(), llm, entity_id="1", audit=lambda record: None)

    retry_system = llm.seen[2]
    assert "option 'One': an angle used the word 'market'" in retry_system
    assert "option 'Two': an angle said what to avoid ('without')" in retry_system
    assert "option 'Three': why carried digits; spell any number as a word" in retry_system


def test_a_good_option_from_a_refused_reply_is_kept_for_the_next_attempt() -> None:
    first = _reply(
        _item("Keeper", recommended=True),
        _item("Bad one", angle="Praise their recent saving."),
        _item("Bad two", angle="Keep it light, without pressure."),
    )
    second = _reply(_item("Fresh one"), _item("Keeper"), _item("Bad three", why="After 90 days."))
    llm = _ScriptedLLM(first, second)

    options = suggest_email_options(_facts(), llm, entity_id="1", audit=lambda record: None)

    assert [option.title for option in options] == ["Keeper", "Fresh one"]
    assert [option.recommended for option in options] == [True, False]
    assert llm.replies == []


def test_the_prompt_states_every_rule_the_code_enforces_even_when_studio_text_omits_them() -> None:
    prompt = build_options_prompt(instructions="STUDIO OPTIONS")

    assert "Write no digits in any field, including why" in prompt
    assert "must not say what to avoid or leave out" in prompt
    assert "Limits: title 70 characters" in prompt


def test_three_bad_replies_give_up() -> None:
    llm = _ScriptedLLM("not json", "still not json", "nope")

    with pytest.raises(OptionsRejected):
        suggest_email_options(_facts(), llm, entity_id="1", audit=lambda record: None)

    assert llm.replies == []
