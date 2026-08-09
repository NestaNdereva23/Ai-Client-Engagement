"""EmailAgent: placeholder-only drafting, prompt variant driven by the rule outcome.

These prove the system prompt always carries the placeholder contract and the
angle, that a known prompt variant selects its own framing line while an
unknown one falls back safely rather than erroring, that facts render only
from what was actually retrieved, and that the placeholder-presence check
used by later milestones behaves as a pure structural rule.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from app.agents.email_agent import (
    REQUIRED_PLACEHOLDERS,
    build_system_prompt,
    build_system_prompt_blocks,
    has_required_placeholders,
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
