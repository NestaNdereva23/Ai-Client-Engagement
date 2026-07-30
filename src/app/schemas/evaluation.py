"""The structured shape a judge's score must take: four 1-5 dimensions plus notes."""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class EvaluationParseError(Exception):
    """Raised when a judge's output is not valid JSON or fails the EvaluationScores schema."""


class EvaluationScores(BaseModel):
    """A validated judge score: four 1-5 dimensions and a short explanation."""

    model_config = ConfigDict(extra="forbid")

    tone: int = Field(ge=1, le=5)
    compliance: int = Field(ge=1, le=5)
    grounding: int = Field(ge=1, le=5)
    personalization: int = Field(ge=1, le=5)
    notes: str


def parse_evaluation_scores(raw: str) -> EvaluationScores:
    """Parse and validate the judge's raw output, raising EvaluationParseError on failure."""
    try:
        payload: Any = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise EvaluationParseError(f"judge output was not valid JSON: {exc}") from exc

    try:
        return EvaluationScores.model_validate(payload)
    except ValidationError as exc:
        raise EvaluationParseError(f"judge output failed schema validation: {exc}") from exc
