"""EmailAgent: placeholder-only drafting, prompt variant driven by the rule outcome.

These prove the system prompt always carries the placeholder contract and the
angle, that a known prompt variant selects its own framing line while an
unknown one falls back safely rather than erroring, that facts render only
from what was actually retrieved, and that the placeholder-presence check
used by later milestones behaves as a pure structural rule.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.agents.email_agent import (
    REQUIRED_PLACEHOLDERS,
    build_system_prompt,
    has_required_placeholders,
    variant_guidance,
)


@dataclass(frozen=True)
class FakeChunk:
    chunk_id: int
    text: str


def test_system_prompt_always_states_the_placeholder_contract() -> None:
    prompt = build_system_prompt(angle="winback_habit", prompt_variant="habit_standard")
    for token in REQUIRED_PLACEHOLDERS:
        assert token in prompt
    assert "real person's name" in prompt
    assert "exact investment amount" in prompt


def test_system_prompt_carries_the_angle() -> None:
    prompt = build_system_prompt(angle="winback_flexible", prompt_variant="flexible_standard")
    assert "Angle: winback_flexible" in prompt


def test_system_prompt_defaults_the_angle_when_missing() -> None:
    prompt = build_system_prompt(angle=None, prompt_variant=None)
    assert "Angle: winback" in prompt


def test_known_prompt_variants_each_get_distinct_guidance() -> None:
    variants = [
        "habit_premium",
        "habit_standard",
        "habit_premium_soft",
        "habit_standard_soft",
        "flexible_premium",
        "flexible_standard",
        "flexible_minimal",
        "flexible_soft",
    ]
    guidance = {variant_guidance(v) for v in variants}
    # Every seeded rule variant (rules/store.py, v1 and v2 seeds) resolves to
    # its own line, not a shared fallback.
    assert len(guidance) == len(variants)


def test_soft_variants_warn_against_asserting_an_exact_count_or_total() -> None:
    for variant in ("habit_premium_soft", "habit_standard_soft", "flexible_soft"):
        guidance = variant_guidance(variant)
        assert "exact count or total" in guidance


def test_unknown_prompt_variant_falls_back_without_erroring() -> None:
    fallback = variant_guidance("some_future_variant_not_seeded_yet")
    assert fallback == variant_guidance(None)
    # A future rule version can ship a new prompt_variant string with no
    # matching code change here.
    prompt = build_system_prompt(angle="winback_habit", prompt_variant="brand_new_variant")
    assert fallback in prompt


def test_facts_render_only_what_was_retrieved() -> None:
    chunks = [FakeChunk(chunk_id=1, text="the fund yielded 11.35% this week")]
    prompt = build_system_prompt(
        angle="winback_habit", prompt_variant="habit_standard", chunks=chunks
    )
    assert "11.35%" in prompt


def test_no_facts_retrieved_tells_the_model_not_to_cite_a_rate() -> None:
    prompt = build_system_prompt(angle="winback_habit", prompt_variant="habit_standard", chunks=())
    assert "do not cite a rate" in prompt


def test_has_required_placeholders_true_only_when_both_tokens_present() -> None:
    assert has_required_placeholders("Dear {{first_name}}, your {{fund_name}} awaits.")
    assert not has_required_placeholders("Dear {{first_name}}, welcome back.")
    assert not has_required_placeholders("Dear Jane, your fund awaits.")
