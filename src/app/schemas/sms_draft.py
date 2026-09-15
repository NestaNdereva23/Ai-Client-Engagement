from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
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
from app.schemas.email_draft import DraftValidationError

_PLACEHOLDER = re.compile(r"\{\{[^}]*\}\}")
_CODE_FENCE = re.compile(r"^```[a-zA-Z]*\n?|\n?```$")


def _strip_code_fence(raw: str) -> str:
    return _CODE_FENCE.sub("", raw.strip()).strip()


class SmsDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    body: str

    @field_validator("body")
    @classmethod
    def _strip_dashes(cls, value: str) -> str:
        return strip_ai_dashes(value)

    @model_validator(mode="after")
    def _check_placeholders(self, info: ValidationInfo) -> SmsDraft:
        if not self.body.strip():
            raise ValueError("body must not be blank")

        facts = (info.context or {}).get("facts")
        missing = [token for token in required_placeholders(facts) if token not in self.body]
        if missing:
            raise ValueError(f"missing required placeholders: {missing}")

        allowed = (info.context or {}).get("allowed_placeholders")
        used = set(_PLACEHOLDER.findall(self.body))
        unexpected = sorted(used - set(allowed if allowed is not None else ALLOWED_PLACEHOLDERS))
        if unexpected:
            raise ValueError(f"unexpected placeholder tokens: {unexpected}")

        return self


def parse_sms_draft(
    raw: str,
    facts: Mapping[str, Any] | None = None,
    *,
    allowed_placeholders: Sequence[str] | None = None,
) -> SmsDraft:
    try:
        payload: Any = json.loads(raw)
    except json.JSONDecodeError as exc:
        try:
            payload = json.loads(_strip_code_fence(raw))
        except json.JSONDecodeError:
            raise DraftValidationError(f"draft was not valid JSON: {exc}") from exc

    try:
        return SmsDraft.model_validate(
            payload, context={"facts": facts, "allowed_placeholders": allowed_placeholders}
        )
    except ValidationError as exc:
        raise DraftValidationError(f"draft failed schema validation: {exc}") from exc
