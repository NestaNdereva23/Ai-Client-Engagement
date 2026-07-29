"""Output guardrails: grounding and format/length, each tagged with its own name.

These prove the grounding check traces every rate claim in the body,
the format check enforces a short win back email's subject and body limits
in both directions, a pass is silent, and a failure always carries a
guardrail name a caller can use to record which one caught it.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from app.agents.guardrails import (
    MAX_BODY_LENGTH,
    MAX_SUBJECT_LENGTH,
    MIN_BODY_LENGTH,
    GuardrailFailure,
    default_format_check,
    default_grounding_check,
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


def test_guardrail_failure_defaults_to_an_unknown_guardrail_name() -> None:
    failure = GuardrailFailure("something went wrong")
    assert failure.guardrail == "unknown"


def test_guardrail_failure_carries_the_name_it_was_raised_with() -> None:
    failure = GuardrailFailure("bad claim", guardrail="grounding")
    assert failure.guardrail == "grounding"
    assert str(failure) == "bad claim"
