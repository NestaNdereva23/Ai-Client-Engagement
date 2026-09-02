from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pytest

from app.agents.email_agent import (
    ALLOWED_PLACEHOLDERS,
    BANNED_WORDS,
    PLACEHOLDER_FACT_FIELDS,
    REQUIRED_PLACEHOLDERS,
    build_system_prompt,
    build_system_prompt_blocks,
    has_required_placeholders,
    placeholder_token,
    strip_ai_dashes,
    variant_guidance,
)
from app.db.session import SessionLocal

# Inside the angle catalogue's seeded window.
IN_FORCE = date(2026, 8, 2)


@dataclass(frozen=True)
class FakeChunk:
    chunk_id: int
    text: str


def test_system_prompt_always_states_the_placeholder_contract() -> None:
    prompt = build_system_prompt(angle="back_on_schedule", prompt_variant="back_on_schedule")
    for token in REQUIRED_PLACEHOLDERS:
        assert token in prompt
    assert "real person's name" in prompt
    assert "exact investment amount" in prompt


def test_system_prompt_carries_the_angle() -> None:
    prompt = build_system_prompt(angle="pick_up_again", prompt_variant="pick_up_again")
    assert "Angle: pick_up_again" in prompt


def test_system_prompt_defaults_the_angle_when_missing() -> None:
    prompt = build_system_prompt(angle=None, prompt_variant=None)
    assert "Angle: winback" in prompt


def test_unknown_prompt_variant_falls_back_without_erroring() -> None:
    fallback = variant_guidance("some_future_variant_not_seeded_yet")
    assert fallback == variant_guidance(None)
    # A future rule version can ship a new prompt_variant string with no
    # matching code change here.
    prompt = build_system_prompt(angle="back_on_schedule", prompt_variant="brand_new_variant")
    assert fallback in prompt


def test_facts_render_only_what_was_retrieved() -> None:
    chunks = [FakeChunk(chunk_id=1, text="the fund yielded 11.35% this week")]
    prompt = build_system_prompt(
        angle="back_on_schedule", prompt_variant="back_on_schedule", chunks=chunks
    )
    assert "11.35%" in prompt


def test_no_facts_retrieved_tells_the_model_not_to_cite_a_rate() -> None:
    prompt = build_system_prompt(
        angle="back_on_schedule", prompt_variant="back_on_schedule", chunks=()
    )
    assert "do not cite a rate" in prompt


def test_variant_guidance_ignores_the_catalogue_without_a_session() -> None:
    """No session, no at: exactly the pre-catalogue behaviour, unchanged."""
    assert variant_guidance("see_what_changed") == variant_guidance(None)


def test_prompt_blocks_cached_half_carries_everything_but_one_clients_own_facts() -> None:
    """The split is for prompt caching, not a content cut: everything a
    plain build_system_prompt call states must still be stated somewhere,
    just divided between the two halves.
    """
    chunks = [FakeChunk(chunk_id=1, text="the fund yielded 11.35% this week")]
    blocks = build_system_prompt_blocks(
        angle="back_on_schedule", prompt_variant="back_on_schedule", chunks=chunks
    )
    for token in REQUIRED_PLACEHOLDERS:
        assert token in blocks.cached
    assert "Angle: back_on_schedule" in blocks.cached
    assert "11.35%" in blocks.cached


def test_prompt_blocks_dynamic_half_carries_this_clients_conditional_caveats() -> None:
    no_cadence_facts = {"stale_contact": False}
    blocks = build_system_prompt_blocks(
        angle="back_on_schedule", prompt_variant="back_on_schedule", facts=no_cadence_facts
    )
    assert "no measurable cadence" in blocks.dynamic
    assert "no measurable cadence" not in blocks.cached


def test_prompt_blocks_dynamic_half_is_empty_with_no_facts_and_no_caveats() -> None:
    blocks = build_system_prompt_blocks(angle="back_on_schedule", prompt_variant="back_on_schedule")
    assert blocks.dynamic == ""


def test_prompt_blocks_cached_half_is_identical_for_two_clients_who_only_differ_in_facts() -> None:
    """This is the property prompt caching on Message Batches depends on:
    two different clients on the same angle, tier, and retrieved chunks
    must get byte-identical cached text, even though their own facts (and
    so their dynamic half) differ.
    """
    chunks = [FakeChunk(chunk_id=1, text="the fund yielded 11.35% this week")]
    client_a = build_system_prompt_blocks(
        angle="back_on_schedule",
        prompt_variant="back_on_schedule",
        chunks=chunks,
        facts={"stale_contact": True},
    )
    client_b = build_system_prompt_blocks(
        angle="back_on_schedule",
        prompt_variant="back_on_schedule",
        chunks=chunks,
        facts={"invested_every_n_days": 30},
    )
    assert client_a.cached == client_b.cached
    assert client_a.dynamic != client_b.dynamic


def test_variant_guidance_ignores_the_catalogue_without_a_date(db: None) -> None:
    with SessionLocal() as session:
        assert variant_guidance("see_what_changed", session=session) == variant_guidance(None)


def test_an_angle_identifier_resolves_from_the_catalogue(db: None) -> None:
    with SessionLocal() as session:
        guidance = variant_guidance("back_on_schedule", session=session, at=IN_FORCE)
    assert "genuine, measurable savings rhythm" in guidance
    assert "resume the exact cadence" in guidance.lower()


def test_every_catalogued_angle_gets_its_own_distinct_guidance(db: None) -> None:
    angles = [
        "not_a_goodbye",
        "wrong_shelf",
        "see_what_changed",
        "the_long_hold",
        "your_next_deposit",
        "second_try",
        "you_wound_down",
        "you_were_scaling",
        "you_were_fading",
        "back_on_schedule",
        "onboarding_retry",
        "pick_up_again",
    ]
    with SessionLocal() as session:
        guidance = {a: variant_guidance(a, session=session, at=IN_FORCE) for a in angles}
    assert len(set(guidance.values())) == len(angles)


def test_a_variant_the_catalogue_does_not_carry_still_falls_back(db: None) -> None:
    with SessionLocal() as session:
        catalogued = variant_guidance("habit_standard", session=session, at=IN_FORCE)
    assert catalogued == variant_guidance("habit_standard")


def test_has_required_placeholders_true_only_when_both_tokens_present() -> None:
    assert has_required_placeholders("Dear {{first_name}}, your {{fund_name}} awaits.")
    assert not has_required_placeholders("Dear {{first_name}}, welcome back.")
    assert not has_required_placeholders("Dear Jane, your fund awaits.")


def test_allowed_placeholders_carries_one_token_per_placeholder_fact_field() -> None:
    """Every placeholder-filled fact gets exactly one token, on top of the
    two every draft has always been able to use."""
    assert set(REQUIRED_PLACEHOLDERS) <= set(ALLOWED_PLACEHOLDERS)
    for field in PLACEHOLDER_FACT_FIELDS:
        assert f"{{{{{field}}}}}" in ALLOWED_PLACEHOLDERS
    assert len(ALLOWED_PLACEHOLDERS) == 2 + len(PLACEHOLDER_FACT_FIELDS)


def test_placeholder_token_matches_the_allowed_vocabulary() -> None:
    for field in PLACEHOLDER_FACT_FIELDS:
        assert placeholder_token(field) in ALLOWED_PLACEHOLDERS


def test_placeholder_token_rejects_a_field_outside_the_vocabulary() -> None:
    with pytest.raises(ValueError):
        placeholder_token("balance")


def test_system_prompt_states_every_allowed_placeholder() -> None:
    """A template draft may use any of these; the model has to be told all
    of them are valid, not just the original two."""
    prompt = build_system_prompt(angle="pick_up_again", prompt_variant="pick_up_again")
    for token in ALLOWED_PLACEHOLDERS:
        assert token in prompt


def test_required_placeholders_does_not_widen_with_the_new_vocabulary() -> None:
    """Not every draft uses all five new tokens; only first_name (and
    fund_name, when withheld) is ever mandatory, whether or not a draft
    also happens to use one of the new ones."""
    assert has_required_placeholders("Dear {{first_name}}, {{fund_name}} awaits.")
    assert has_required_placeholders(
        "Dear {{first_name}}, {{fund_name}} awaits. Contribution: {{typical_contribution}}."
    )


def test_strip_ai_dashes_turns_an_unspaced_em_dash_into_a_comma() -> None:
    text = "We're not here to convince you to return—your decision was yours to make."
    assert strip_ai_dashes(text) == (
        "We're not here to convince you to return, your decision was yours to make."
    )


def test_strip_ai_dashes_turns_a_spaced_em_dash_into_a_comma() -> None:
    assert strip_ai_dashes("a simple, flexible option — whenever you have cash") == (
        "a simple, flexible option, whenever you have cash"
    )


def test_strip_ai_dashes_handles_an_en_dash() -> None:
    assert strip_ai_dashes("open 50–125 words") == "open 50, 125 words"


def test_strip_ai_dashes_handles_a_double_hyphen() -> None:
    assert strip_ai_dashes("worth a look--think it over") == "worth a look, think it over"


def test_strip_ai_dashes_handles_a_spaced_single_hyphen() -> None:
    assert strip_ai_dashes("a good fit - and low pressure too") == (
        "a good fit, and low pressure too"
    )


def test_strip_ai_dashes_turns_a_compound_word_hyphen_into_a_space() -> None:
    assert strip_ai_dashes("a low-pressure, win-back message") == (
        "a low pressure, win back message"
    )


def test_strip_ai_dashes_leaves_text_with_no_dash_unchanged() -> None:
    text = "Hi {{first_name}}, {{fund_name}} is currently offering 11.08%."
    assert strip_ai_dashes(text) == text


def test_strip_ai_dashes_passes_through_blank_input() -> None:
    assert strip_ai_dashes("") == ""


def test_park_is_a_banned_word() -> None:
    assert "park" in BANNED_WORDS


def test_system_prompt_lists_the_banned_words() -> None:
    prompt = build_system_prompt(angle="your_next_deposit", prompt_variant="your_next_deposit")
    assert "BANNED WORDS" in prompt
    for word in BANNED_WORDS:
        assert word in prompt


def test_system_prompt_forbids_interrogating_why_the_client_left() -> None:
    prompt = build_system_prompt(angle="you_wound_down", prompt_variant="you_wound_down")
    assert "interrogates why the client left" in prompt


def test_system_prompt_forbids_announcing_the_win_back_attempt() -> None:
    prompt = build_system_prompt(angle="you_wound_down", prompt_variant="you_wound_down")
    assert "WINNING THE CLIENT BACK" in prompt
    assert "not trying to convince the client" in prompt


def test_system_prompt_requires_a_greeting_and_a_sign_off() -> None:
    prompt = build_system_prompt(angle="pick_up_again", prompt_variant="pick_up_again")
    assert "must always open with a short greeting" in prompt
    assert "must always end with the sign off" in prompt


def test_system_prompt_requires_the_exact_rate_over_a_vague_phrase() -> None:
    prompt = build_system_prompt(angle="your_next_deposit", prompt_variant="your_next_deposit")
    assert "meaningful returns" in prompt
    assert "state the exact figure" in prompt
