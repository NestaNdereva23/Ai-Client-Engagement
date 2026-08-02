from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from app.schemas.email_draft import DraftValidationError, EmailDraft, parse_email_draft


def draft(subject: str, body: str) -> str:
    return json.dumps({"subject": subject, "body": body})


def test_a_well_formed_draft_parses_cleanly() -> None:
    result = parse_email_draft(
        draft("Come back to {{fund_name}}", "Dear {{first_name}}, we miss you.")
    )
    assert isinstance(result, EmailDraft)
    assert result.subject == "Come back to {{fund_name}}"
    assert result.body == "Dear {{first_name}}, we miss you."


def test_a_draft_wrapped_in_a_json_code_fence_still_parses() -> None:
    """Some models wrap otherwise valid JSON in ```json fences despite being told not to."""
    fenced = "```json\n" + draft("Come back to {{fund_name}}", "Dear {{first_name}}.") + "\n```"
    result = parse_email_draft(fenced)
    assert result.subject == "Come back to {{fund_name}}"


def test_a_draft_wrapped_in_a_plain_code_fence_still_parses() -> None:
    fenced = "```\n" + draft("Come back to {{fund_name}}", "Dear {{first_name}}.") + "\n```"
    result = parse_email_draft(fenced)
    assert result.subject == "Come back to {{fund_name}}"


def test_genuinely_malformed_content_inside_a_fence_is_still_rejected() -> None:
    with pytest.raises(DraftValidationError, match="not valid JSON"):
        parse_email_draft("```json\nthis is not json\n```")


def test_malformed_json_is_rejected() -> None:
    with pytest.raises(DraftValidationError, match="not valid JSON"):
        parse_email_draft("this is not json")


def test_a_json_array_instead_of_an_object_is_rejected() -> None:
    with pytest.raises(DraftValidationError):
        parse_email_draft(json.dumps(["subject", "body"]))


def test_a_missing_field_is_rejected() -> None:
    with pytest.raises(DraftValidationError, match="schema validation"):
        parse_email_draft(json.dumps({"subject": "Come back to {{fund_name}}"}))


def test_an_extra_unrelated_field_is_rejected() -> None:
    with pytest.raises(DraftValidationError):
        parse_email_draft(
            json.dumps(
                {
                    "subject": "Come back to {{fund_name}}",
                    "body": "Dear {{first_name}}, we miss you.",
                    "sender": "not part of the schema",
                }
            )
        )


def test_a_blank_subject_or_body_is_rejected() -> None:
    with pytest.raises(DraftValidationError):
        parse_email_draft(draft("", "Dear {{first_name}}, {{fund_name}} awaits."))
    with pytest.raises(DraftValidationError):
        parse_email_draft(draft("Come back to {{fund_name}}", "   "))


def test_a_missing_required_placeholder_is_rejected() -> None:
    with pytest.raises(DraftValidationError, match=r"missing required placeholders"):
        parse_email_draft(draft("Welcome back", "Dear {{first_name}}, we miss you."))


def test_both_required_placeholders_may_split_across_subject_and_body() -> None:
    result = parse_email_draft(
        draft("Come back to {{fund_name}}", "Dear {{first_name}}, we miss you.")
    )
    assert result.subject == "Come back to {{fund_name}}"


def test_an_unexpected_placeholder_token_is_rejected() -> None:
    with pytest.raises(DraftValidationError, match=r"unexpected placeholder"):
        parse_email_draft(
            draft(
                "Come back to {{fund_name}}",
                "Dear {{first_name}}, you invested {{amount}}.",
            )
        )


def test_email_draft_model_validate_raises_pydantic_validation_error_directly() -> None:
    """The lower-level model is still usable on its own, outside parse_email_draft."""
    with pytest.raises(ValidationError):
        EmailDraft.model_validate({"subject": "no body here"})
