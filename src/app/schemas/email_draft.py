"""The structured shape a draft must take: subject, body, placeholders only.

The model is asked to answer with exactly this JSON shape; this module is the
independent check that it actually did. Malformed JSON, a missing field, a
dropped required placeholder, or an invented one all fail validation, so a
bad draft is rejected before a human ever sees it.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from pydantic import (
    BaseModel,
    ConfigDict,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)

from app.agents.email_agent import ALLOWED_PLACEHOLDERS, required_placeholders, strip_ai_dashes

_PLACEHOLDER = re.compile(r"\{\{[^}]*\}\}")
_CODE_FENCE = re.compile(r"^```[a-zA-Z]*\n?|\n?```$")


class DraftValidationError(Exception):
    """Raised when a draft is not valid JSON or fails the EmailDraft schema."""


class EmailDraft(BaseModel):
    """A validated draft: subject, body, and nothing but the allowed placeholders."""

    model_config = ConfigDict(extra="forbid")

    subject: str
    body: str

    @field_validator("subject", "body")
    @classmethod
    def _strip_dashes(cls, value: str) -> str:
        return strip_ai_dashes(value)

    @model_validator(mode="after")
    def _check_placeholders(self, info: ValidationInfo) -> EmailDraft:
        if not self.subject.strip() or not self.body.strip():
            raise ValueError("subject and body must not be blank")

        # Which tokens are still required depends on what the draft was told:
        # a draft given the fund name as a fact writes it out instead.
        facts = (info.context or {}).get("facts")
        combined = f"{self.subject}\n{self.body}"
        missing = [token for token in required_placeholders(facts) if token not in combined]
        if missing:
            raise ValueError(f"missing required placeholders: {missing}")

        used = set(_PLACEHOLDER.findall(combined))
        unexpected = sorted(used - set(ALLOWED_PLACEHOLDERS))
        if unexpected:
            raise ValueError(f"unexpected placeholder tokens: {unexpected}")

        return self


def _strip_code_fence(raw: str) -> str:
    # Drop a wrapping ```json ... ``` (or plain ``` ... ```) fence, if present."""
    return _CODE_FENCE.sub("", raw.strip()).strip()


def parse_email_draft(raw: str, facts: Mapping[str, Any] | None = None) -> EmailDraft:
    """Parse and validate the model's raw output, raising DraftValidationError on any failure.

    Tries the raw string first, a model that already returns clean JSON never
    touches the fallback below. Only on failure does it retry once against a
    markdown-code-fence-stripped version, since some models wrap otherwise
    valid JSON in ```json ... ``` despite being told not to.
    """
    try:
        payload: Any = json.loads(raw)
    except json.JSONDecodeError as exc:
        try:
            payload = json.loads(_strip_code_fence(raw))
        except json.JSONDecodeError:
            raise DraftValidationError(f"draft was not valid JSON: {exc}") from exc

    try:
        return EmailDraft.model_validate(payload, context={"facts": facts})
    except ValidationError as exc:
        raise DraftValidationError(f"draft failed schema validation: {exc}") from exc
