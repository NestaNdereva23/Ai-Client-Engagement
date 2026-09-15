from __future__ import annotations

from app.agents.sms_agent import build_sms_system_prompt


def test_system_prompt_never_asks_for_a_subject() -> None:
    prompt = build_sms_system_prompt(angle="winback_habit", prompt_variant=None)
    assert '"subject"' not in prompt
    assert "no sign off" in prompt or "sign off" in prompt


def test_system_prompt_states_the_placeholder_contract() -> None:
    prompt = build_sms_system_prompt(angle="winback_habit", prompt_variant=None)
    assert "{{first_name}}" in prompt
    assert "{{fund_name}}" in prompt


def test_system_prompt_carries_the_angle_when_no_brief_is_given() -> None:
    prompt = build_sms_system_prompt(angle="fee_warning", prompt_variant=None)
    assert "fee_warning" in prompt


def test_system_prompt_lists_the_configured_banned_words() -> None:
    prompt = build_sms_system_prompt(
        angle="winback_habit", prompt_variant=None, safety_words=("guarantee",)
    )
    assert "guarantee" in prompt


def test_system_prompt_uses_a_configured_voice_text_instead_of_the_default() -> None:
    prompt = build_sms_system_prompt(
        angle="winback_habit", prompt_variant=None, voice_text="Custom sms voice."
    )
    assert prompt.startswith("Custom sms voice.")


def test_system_prompt_mentions_sms_parts_not_words() -> None:
    prompt = build_sms_system_prompt(angle="winback_habit", prompt_variant=None)
    assert "SMS part" in prompt
