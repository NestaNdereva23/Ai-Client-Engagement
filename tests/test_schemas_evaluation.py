"""EvaluationScores: the schema a judge's raw output must validate against.

These prove a well-formed score parses cleanly, malformed JSON and a missing
field are both rejected, a score outside 1-5 is rejected, an unrelated extra
field is rejected, and the exception always carries a useful reason rather
than a bare parser traceback.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from app.schemas.evaluation import EvaluationParseError, EvaluationScores, parse_evaluation_scores


def scores(**overrides) -> str:
    defaults = {"tone": 4, "compliance": 5, "grounding": 5, "personalization": 3, "notes": "fine"}
    defaults.update(overrides)
    return json.dumps(defaults)


def test_a_well_formed_score_parses_cleanly() -> None:
    result = parse_evaluation_scores(scores(tone=4, compliance=5, grounding=5, personalization=3))
    assert isinstance(result, EvaluationScores)
    assert (result.tone, result.compliance, result.grounding, result.personalization) == (
        4,
        5,
        5,
        3,
    )
    assert result.notes == "fine"


def test_malformed_json_is_rejected() -> None:
    with pytest.raises(EvaluationParseError, match="not valid JSON"):
        parse_evaluation_scores("this is not json")


def test_a_json_array_instead_of_an_object_is_rejected() -> None:
    with pytest.raises(EvaluationParseError):
        parse_evaluation_scores(json.dumps([1, 2, 3, 4]))


def test_a_missing_field_is_rejected() -> None:
    with pytest.raises(EvaluationParseError, match="schema validation"):
        parse_evaluation_scores(json.dumps({"tone": 4, "compliance": 5, "grounding": 5}))


def test_an_extra_unrelated_field_is_rejected() -> None:
    with pytest.raises(EvaluationParseError):
        parse_evaluation_scores(scores(sender="not part of the schema"))


@pytest.mark.parametrize("field", ["tone", "compliance", "grounding", "personalization"])
def test_a_score_outside_one_to_five_is_rejected(field: str) -> None:
    with pytest.raises(EvaluationParseError):
        parse_evaluation_scores(scores(**{field: 0}))
    with pytest.raises(EvaluationParseError):
        parse_evaluation_scores(scores(**{field: 6}))


def test_evaluation_scores_model_validate_raises_pydantic_validation_error_directly() -> None:
    """The lower-level model is still usable on its own, outside parse_evaluation_scores."""
    with pytest.raises(ValidationError):
        EvaluationScores.model_validate({"tone": 4})
