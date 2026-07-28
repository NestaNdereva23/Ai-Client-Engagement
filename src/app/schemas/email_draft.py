"""The structured shape a draft must take: subject, body, placeholders only.

The model is asked to answer with exactly this JSON shape; this module is the
independent check that it actually did. Malformed JSON, a missing field, a
dropped required placeholder, or an invented one all fail validation, so a
bad draft is rejected before a human ever sees it.
"""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from app.agents.email_agent import REQUIRED_PLACEHOLDERS

_PLACEHOLDER = re.compile(r"\{\{[^}]*\}\}")


class DraftValidationError(Exception):
    """Raised when a draft is not valid JSON or fails the EmailDraft schema."""


class EmailDraft(BaseModel):
    """A validated draft: subject, body, and nothing but the allowed placeholders."""

    model_config = ConfigDict(extra="forbid")

    subject: str
    body: str

    @model_validator(mode="after")
    def _check_placeholders(self) -> EmailDraft:
        if not self.subject.strip() or not self.body.strip():
            raise ValueError("subject and body must not be blank")

        combined = f"{self.subject}\n{self.body}"
        missing = [token for token in REQUIRED_PLACEHOLDERS if token not in combined]
        if missing:
            raise ValueError(f"missing required placeholders: {missing}")

        used = set(_PLACEHOLDER.findall(combined))
        unexpected = sorted(used - set(REQUIRED_PLACEHOLDERS))
        if unexpected:
            raise ValueError(f"unexpected placeholder tokens: {unexpected}")

        return self


def parse_email_draft(raw: str) -> EmailDraft:
    """Parse and validate the model's raw output, raising DraftValidationError on any failure."""
    try:
        payload: Any = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DraftValidationError(f"draft was not valid JSON: {exc}") from exc

    try:
        return EmailDraft.model_validate(payload)
    except ValidationError as exc:
        raise DraftValidationError(f"draft failed schema validation: {exc}") from exc
