from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from app.schemas.email_draft import DraftValidationError
from app.schemas.sms_draft import SmsDraft, parse_sms_draft


def draft(body: str) -> str:
    return json.dumps({"body": body})


def test_a_well_formed_draft_parses_cleanly() -> None:
    result = parse_sms_draft(draft("Hi {{first_name}}, {{fund_name}} is open again."))
    assert isinstance(result, SmsDraft)
    assert result.body == "Hi {{first_name}}, {{fund_name}} is open again."


def test_a_draft_wrapped_in_a_json_code_fence_still_parses() -> None:
    fenced = "```json\n" + draft("Hi {{first_name}}, {{fund_name}} is open again.") + "\n```"
    result = parse_sms_draft(fenced)
    assert "{{first_name}}" in result.body


def test_malformed_json_is_rejected() -> None:
    with pytest.raises(DraftValidationError, match="not valid JSON"):
        parse_sms_draft("this is not json")


def test_a_missing_field_is_rejected() -> None:
    with pytest.raises(DraftValidationError, match="schema validation"):
        parse_sms_draft(json.dumps({}))


def test_a_subject_field_is_rejected_as_extra() -> None:
    with pytest.raises(DraftValidationError):
        parse_sms_draft(json.dumps({"subject": "not allowed", "body": "Hi {{first_name}}."}))


def test_a_blank_body_is_rejected() -> None:
    with pytest.raises(DraftValidationError):
        parse_sms_draft(draft("   "))


def test_a_missing_required_placeholder_is_rejected() -> None:
    with pytest.raises(DraftValidationError, match=r"missing required placeholders"):
        parse_sms_draft(draft("We miss you, come back soon."))


def test_an_unexpected_placeholder_token_is_rejected() -> None:
    with pytest.raises(DraftValidationError, match=r"unexpected placeholder"):
        parse_sms_draft(draft("Hi {{first_name}}, {{fund_name}} paid {{amount}} this month."))


def test_a_placeholder_filled_fact_token_is_accepted() -> None:
    result = parse_sms_draft(
        draft("Hi {{first_name}}, {{fund_name}} still fits your {{typical_contribution}} habit.")
    )
    assert "{{typical_contribution}}" in result.body


def test_parse_sms_draft_strips_an_em_dash_from_the_body() -> None:
    result = parse_sms_draft(draft("Hi {{first_name}}, {{fund_name}} is back—come take a look."))
    assert "—" not in result.body


def test_sms_draft_model_validate_raises_pydantic_validation_error_directly() -> None:
    with pytest.raises(ValidationError):
        SmsDraft.model_validate({"subject": "no body field here"})
