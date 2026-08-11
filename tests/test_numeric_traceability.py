"""The numeric traceability guardrail and the tier word cap.

Traceability is what separates personalising from fabricating: a figure the
model invented, or derived by combining two it was given, traces to neither
the fact block nor a retrieved chunk and so never reaches a reviewer.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from app.agents.guardrails import (
    MAX_BODY_LENGTH,
    GuardrailFailure,
    check_no_unresolved_placeholders,
    default_format_check,
    default_numeric_traceability_check,
    instance_numeric_traceability_check,
    traceable_numbers,
)


@dataclass(frozen=True)
class FakeChunk:
    chunk_id: int
    text: str


@dataclass(frozen=True)
class FakeContract:
    max_words: int = 120
    sign_off: str = "client services"


FACTS = {
    "fund_name": "Cytonn Money Market Fund",
    "typical_contribution_kes": 150_000,
    "largest_contribution_kes": 4_500_000,
    "invested_every_n_days": 30,
    "years_since_exit": 2.5,
    "month_they_left": "2024-07",
    "stale_contact": True,
}


def _check(body: str, *, facts=FACTS, chunks=()) -> None:
    default_numeric_traceability_check({"body": body, "facts": facts, "chunks": chunks})


# --- what a draft may say ---


@pytest.mark.parametrize(
    "body",
    [
        "You invested around KES 150,000 each time.",
        "You invested around KES 150000 each time.",
        "You invested every 30 days.",
        "It has been 2.5 years since you left.",
        "You left us in 2024-07.",
        "You put in as much as KES 4,500,000 at once.",
    ],
)
def test_a_figure_from_the_fact_block_passes(body: str) -> None:
    _check(body)


def test_a_figure_from_a_retrieved_chunk_passes() -> None:
    chunks = [FakeChunk(chunk_id=1, text="the fund yielded 11.35% last week")]
    _check("The fund returned 11.35% last week.", chunks=chunks)


def test_a_draft_with_no_numbers_at_all_passes() -> None:
    _check("Dear {{first_name}}, we would value hearing from you again.")


def test_a_number_inside_a_placeholder_is_not_a_claim() -> None:
    _check("Dear {{first_name}}, your {{fund_name}} awaits.", facts=None)


# --- what it may not ---


def test_a_fabricated_figure_is_caught() -> None:
    with pytest.raises(GuardrailFailure, match="trace to no fact") as exc:
        _check("You invested around KES 450,000 each time.")
    assert exc.value.guardrail == "numeric_traceability"


def test_a_figure_derived_from_two_real_ones_is_caught() -> None:
    """150,000 and 4,500,000 are both given; their sum is neither, nor true."""
    with pytest.raises(GuardrailFailure, match="trace to no fact"):
        _check("Together that came to KES 4,650,000.")


def test_a_total_the_model_computed_is_caught() -> None:
    with pytest.raises(GuardrailFailure, match="trace to no fact"):
        _check("Across the year that came to KES 1,800,000.")


def test_a_rate_no_chunk_supports_is_caught() -> None:
    with pytest.raises(GuardrailFailure, match="trace to no fact"):
        _check("The fund returned 12.75% last week.")


def test_an_incidental_digit_is_caught_so_it_must_be_spelled_out() -> None:
    """The prompt asks for words, so a stray digit is a real signal, not noise."""
    with pytest.raises(GuardrailFailure, match="trace to no fact"):
        _check("Could we take 15 minutes to show you?")


def test_the_same_sentence_spelled_out_passes() -> None:
    _check("Could we take fifteen minutes to show you?")


def test_every_untraceable_number_is_named_in_the_failure() -> None:
    with pytest.raises(GuardrailFailure) as exc:
        _check("We saw 111,111 and 222,222 from you.")
    assert "111,111" in str(exc.value)
    assert "222,222" in str(exc.value)


# --- the allowed set itself ---


def test_a_boolean_fact_contributes_no_numbers() -> None:
    """True must not become "1" and quietly permit a stray 1 in the body."""
    assert traceable_numbers({"stale_contact": True, "in_wave": False}) == set()


def test_a_string_fact_contributes_the_numbers_inside_it() -> None:
    assert "2024" in traceable_numbers({"month_they_left": "2024-07"})


def test_no_facts_and_no_chunks_permits_nothing() -> None:
    assert traceable_numbers(None, ()) == set()


# --- the tier word cap ---


def _format(body: str, *, contract=None, subject: str = "Come back") -> None:
    state = {"subject": subject, "body": body}
    if contract is not None:
        state["contract"] = contract
    default_format_check(state)


def test_a_body_within_its_tiers_word_cap_passes() -> None:
    _format(" ".join(["word"] * 120), contract=FakeContract(max_words=120))


def test_a_body_over_its_tiers_word_cap_is_caught() -> None:
    with pytest.raises(GuardrailFailure, match="over the 120 this tier allows") as exc:
        _format(" ".join(["word"] * 121), contract=FakeContract(max_words=120))
    assert exc.value.guardrail == "format_length"


@pytest.mark.parametrize("max_words", [120, 140, 110, 60])
def test_each_tiers_cap_is_enforced_at_its_own_number(max_words: int) -> None:
    _format(" ".join(["word"] * max_words), contract=FakeContract(max_words=max_words))
    with pytest.raises(GuardrailFailure, match=f"over the {max_words} this tier allows"):
        _format(" ".join(["word"] * (max_words + 1)), contract=FakeContract(max_words=max_words))


def test_without_a_contract_the_character_bound_still_applies() -> None:
    with pytest.raises(GuardrailFailure, match=str(MAX_BODY_LENGTH)):
        _format("x" * (MAX_BODY_LENGTH + 1))


def test_a_tiered_message_is_not_also_held_to_the_character_bound() -> None:
    """A long-worded body under its word cap must not trip the old limit."""
    body = " ".join(["antidisestablishmentarianism"] * 120)
    assert len(body) > MAX_BODY_LENGTH
    _format(body, contract=FakeContract(max_words=120))


def test_the_minimum_body_length_still_applies_to_a_tiered_message() -> None:
    with pytest.raises(GuardrailFailure, match="minimum"):
        _format("too short", contract=FakeContract())


# --- the post-instantiation re-check ---


def test_check_no_unresolved_placeholders_passes_a_fully_resolved_message() -> None:
    check_no_unresolved_placeholders("Come back to Cytonn MMF", "Dear Jane, we miss you.")


def test_check_no_unresolved_placeholders_catches_a_leftover_token() -> None:
    with pytest.raises(GuardrailFailure) as exc:
        check_no_unresolved_placeholders(
            "Hi Jane", "Your typical contribution of {{typical_contribution}}."
        )
    assert exc.value.guardrail == "unresolved_placeholder"
    assert "{{typical_contribution}}" in str(exc.value)


def test_instance_check_passes_a_figure_already_in_the_template() -> None:
    """A number the template itself already carried (a real chunk-grounded
    claim, unchanged by personalization) needs no client fact to back it."""
    instance_numeric_traceability_check(
        template_body="Dear {{first_name}}, your fund returned 11.35% last week.",
        resolved_body="Dear Jane, your fund returned 11.35% last week.",
        client_facts=None,
    )


def test_instance_check_passes_a_substituted_client_figure() -> None:
    instance_numeric_traceability_check(
        template_body="Dear {{first_name}}, your contribution was {{typical_contribution}}.",
        resolved_body="Dear Jane, your contribution was 5,000.",
        client_facts={"typical_contribution_kes": 5000},
    )


def test_instance_check_catches_a_number_that_traces_to_nothing() -> None:
    with pytest.raises(GuardrailFailure) as exc:
        instance_numeric_traceability_check(
            template_body="Dear {{first_name}}, come back to us.",
            resolved_body="Dear Jane, you invested 450,000 with us.",
            client_facts={"typical_contribution_kes": 5000},
        )
    assert exc.value.guardrail == "instance_numeric_traceability"
    assert "450,000" in str(exc.value)


def test_instance_check_catches_the_wrong_clients_figure() -> None:
    """The substituted number must trace to *this* client's own fact, not
    just any number that happens to look plausible."""
    with pytest.raises(GuardrailFailure, match="trace to no template fact"):
        instance_numeric_traceability_check(
            template_body="Dear {{first_name}}, your contribution was {{typical_contribution}}.",
            resolved_body="Dear Jane, your contribution was 9,999.",
            client_facts={"typical_contribution_kes": 5000},
        )
