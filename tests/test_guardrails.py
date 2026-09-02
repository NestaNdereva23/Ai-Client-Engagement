from __future__ import annotations

from dataclasses import dataclass

import pytest

from app.agents.guardrails import (
    MAX_BODY_LENGTH,
    MAX_SUBJECT_LENGTH,
    MIN_BODY_LENGTH,
    GuardrailFailure,
    default_banned_words_check,
    default_currency_check,
    default_format_check,
    default_grounding_check,
    default_rate_specificity_check,
    default_sign_off_check,
)


@dataclass(frozen=True)
class FakeChunk:
    chunk_id: int
    text: str


def test_grounding_check_passes_a_claim_that_traces_to_a_chunk() -> None:
    state = {
        "body": "Dear {{first_name}}, {{fund_name}} returned 11.35% last week.",
        "chunks": [FakeChunk(chunk_id=1, text="the fund yielded 11.35% this week")],
    }
    default_grounding_check(state)  # does not raise


def test_grounding_check_fails_a_fabricated_claim() -> None:
    state = {
        "body": "Dear {{first_name}}, {{fund_name}} returned 99.99% last week.",
        "chunks": [FakeChunk(chunk_id=1, text="the fund yielded 11.35% this week")],
    }
    with pytest.raises(GuardrailFailure) as exc_info:
        default_grounding_check(state)
    assert exc_info.value.guardrail == "grounding"


def test_grounding_check_passes_a_body_with_no_rate_claims() -> None:
    default_format_check_state = {"body": "Dear {{first_name}}, we miss you at {{fund_name}}."}
    default_grounding_check(default_format_check_state)  # does not raise


def test_format_check_passes_a_reasonable_subject_and_body() -> None:
    state = {
        "subject": "Come back to {{fund_name}}",
        "body": "Dear {{first_name}}, we would love to see you invest again soon.",
    }
    default_format_check(state)  # does not raise


def test_format_check_fails_a_subject_over_the_limit() -> None:
    state = {
        "subject": "S" * (MAX_SUBJECT_LENGTH + 1),
        "body": "Dear {{first_name}}, {{fund_name}} awaits your return, please come back soon.",
    }
    with pytest.raises(GuardrailFailure) as exc_info:
        default_format_check(state)
    assert exc_info.value.guardrail == "format_length"
    assert "subject" in str(exc_info.value)


def test_format_check_fails_a_body_under_the_minimum() -> None:
    state = {"subject": "Hi {{first_name}}", "body": "{{fund_name}}"}
    assert len(state["body"]) < MIN_BODY_LENGTH
    with pytest.raises(GuardrailFailure) as exc_info:
        default_format_check(state)
    assert exc_info.value.guardrail == "format_length"
    assert "under" in str(exc_info.value)


def test_format_check_fails_a_body_over_the_limit() -> None:
    state = {
        "subject": "Come back to {{fund_name}}",
        "body": ("Dear {{first_name}}, " + "we miss you. " * 200),
    }
    assert len(state["body"]) > MAX_BODY_LENGTH
    with pytest.raises(GuardrailFailure) as exc_info:
        default_format_check(state)
    assert exc_info.value.guardrail == "format_length"
    assert "over" in str(exc_info.value)


def test_currency_check_passes_a_kes_only_draft() -> None:
    state = {
        "subject": "Come back to {{fund_name}}",
        "body": "Dear {{first_name}}, your typical contribution was KES 50,000.",
    }
    default_currency_check(state)  # does not raise


def test_currency_check_fails_a_dollar_sign_in_the_body() -> None:
    state = {
        "subject": "Come back to {{fund_name}}",
        "body": "Dear {{first_name}}, top up with as little as $50 today.",
    }
    with pytest.raises(GuardrailFailure) as exc_info:
        default_currency_check(state)
    assert exc_info.value.guardrail == "currency"


def test_currency_check_fails_usd_in_the_subject() -> None:
    state = {
        "subject": "Grow your USD savings with {{fund_name}}",
        "body": "Dear {{first_name}}, we would love to see you invest again soon.",
    }
    with pytest.raises(GuardrailFailure) as exc_info:
        default_currency_check(state)
    assert exc_info.value.guardrail == "currency"


def test_currency_check_ignores_missing_fields() -> None:
    default_currency_check({})  # does not raise


def test_banned_words_check_passes_a_draft_with_no_banned_word() -> None:
    state = {
        "subject": "Come back to {{fund_name}}",
        "body": "Dear {{first_name}}, keep this fund handy for your next deposit.",
    }
    default_banned_words_check(state)  # does not raise


def test_banned_words_check_fails_on_the_word_park_in_the_body() -> None:
    state = {
        "subject": "Come back to {{fund_name}}",
        "body": "Dear {{first_name}}, a good place to park cash when you need it.",
    }
    with pytest.raises(GuardrailFailure) as exc_info:
        default_banned_words_check(state)
    assert exc_info.value.guardrail == "banned_words"
    assert "park" in str(exc_info.value)


def test_banned_words_check_catches_an_inflected_form_in_the_subject() -> None:
    state = {
        "subject": "Your next funds parking opportunity",
        "body": "Dear {{first_name}}, we would love to see you invest again soon.",
    }
    with pytest.raises(GuardrailFailure) as exc_info:
        default_banned_words_check(state)
    assert exc_info.value.guardrail == "banned_words"


def test_banned_words_check_does_not_flag_a_word_that_merely_contains_the_root() -> None:
    state = {
        "subject": "Come back to {{fund_name}}",
        "body": "Dear {{first_name}}, this fund's returns spark real interest lately.",
    }
    default_banned_words_check(state)  # "spark" is not "park"


def test_banned_words_check_ignores_missing_fields() -> None:
    default_banned_words_check({})  # does not raise


@dataclass(frozen=True)
class FakeContract:
    max_words: int
    sign_off: str


def test_sign_off_check_passes_when_the_body_carries_the_required_sign_off() -> None:
    state = {
        "body": "Dear {{first_name}}, we would love to see you invest again. Best regards, RM",
        "contract": FakeContract(max_words=125, sign_off="Best regards, RM"),
    }
    default_sign_off_check(state)  # does not raise


def test_sign_off_check_fails_when_the_sign_off_is_missing() -> None:
    state = {
        "body": "Dear {{first_name}}, we would love to see you invest again soon.",
        "contract": FakeContract(max_words=125, sign_off="Best regards, RM"),
    }
    with pytest.raises(GuardrailFailure) as exc_info:
        default_sign_off_check(state)
    assert exc_info.value.guardrail == "sign_off"


def test_sign_off_check_passes_when_there_is_no_contract() -> None:
    default_sign_off_check({"body": "Dear {{first_name}}, we miss you."})  # does not raise


def test_rate_specificity_check_passes_when_the_body_states_the_figure() -> None:
    state = {
        "body": "Dear {{first_name}}, {{fund_name}} is currently offering 11.08% annually.",
        "chunks": [FakeChunk(chunk_id=1, text="the fund is offering 11.08% effective annual rate")],
    }
    default_rate_specificity_check(state)  # does not raise


def test_rate_specificity_check_passes_when_no_rate_was_ever_retrieved() -> None:
    state = {
        "body": "Dear {{first_name}}, {{fund_name}} continues to offer meaningful returns.",
        "chunks": [],
    }
    default_rate_specificity_check(state)  # nothing to be specific about


def test_rate_specificity_check_passes_a_body_that_never_raises_returns() -> None:
    state = {
        "body": "Dear {{first_name}}, it has been a while since we last heard from you.",
        "chunks": [FakeChunk(chunk_id=1, text="the fund is offering 11.08% effective annual rate")],
    }
    default_rate_specificity_check(state)  # does not raise


def test_rate_specificity_check_fails_a_vague_claim_when_a_rate_was_retrieved() -> None:
    state = {
        "body": "Dear {{first_name}}, {{fund_name}} continues to offer meaningful returns.",
        "chunks": [FakeChunk(chunk_id=1, text="the fund is offering 11.08% effective annual rate")],
    }
    with pytest.raises(GuardrailFailure) as exc_info:
        default_rate_specificity_check(state)
    assert exc_info.value.guardrail == "rate_specificity"


def test_guardrail_failure_defaults_to_an_unknown_guardrail_name() -> None:
    failure = GuardrailFailure("something went wrong")
    assert failure.guardrail == "unknown"


def test_guardrail_failure_carries_the_name_it_was_raised_with() -> None:
    failure = GuardrailFailure("bad claim", guardrail="grounding")
    assert failure.guardrail == "grounding"
    assert str(failure) == "bad claim"
