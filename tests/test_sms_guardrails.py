from __future__ import annotations

import pytest

from app.agents.guardrails import (
    MAX_SMS_PARTS,
    SMS_GUARDRAIL_CHECKS,
    SMS_MULTI_PART_LENGTH,
    SMS_SINGLE_PART_LENGTH,
    GuardrailFailure,
    default_sms_length_check,
    sms_part_count,
)


def test_a_short_body_is_one_part() -> None:
    assert sms_part_count("Hi there, come back soon.") == 1


def test_a_body_at_the_single_part_limit_is_still_one_part() -> None:
    assert sms_part_count("x" * SMS_SINGLE_PART_LENGTH) == 1


def test_a_body_one_over_the_single_part_limit_becomes_two_parts() -> None:
    assert sms_part_count("x" * (SMS_SINGLE_PART_LENGTH + 1)) == 2


def test_a_body_filling_two_multi_part_segments_is_two_parts() -> None:
    assert sms_part_count("x" * (SMS_MULTI_PART_LENGTH * 2)) == 2


def test_an_empty_body_is_zero_parts() -> None:
    assert sms_part_count("") == 0


def test_length_check_passes_a_short_body() -> None:
    default_sms_length_check({"body": "Hi {{first_name}}, come back to {{fund_name}}."})


def test_length_check_rejects_a_too_short_body() -> None:
    with pytest.raises(GuardrailFailure) as exc_info:
        default_sms_length_check({"body": "Hi."})
    assert exc_info.value.guardrail == "format_length"


def test_length_check_rejects_a_body_over_the_part_limit() -> None:
    over_limit = "x" * (SMS_MULTI_PART_LENGTH * MAX_SMS_PARTS + 1)
    with pytest.raises(GuardrailFailure) as exc_info:
        default_sms_length_check({"body": over_limit})
    assert exc_info.value.guardrail == "format_length"


def test_sms_guardrail_checks_has_no_sign_off_or_email_format_check() -> None:
    names = {check.__name__ for check in SMS_GUARDRAIL_CHECKS}
    assert "default_sms_length_check" in names
    assert "default_format_check" not in names
    assert "default_sign_off_check" not in names
