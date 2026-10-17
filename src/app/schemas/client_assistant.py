from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, model_validator


class StarterOut(BaseModel):
    code: str
    label: str
    description: str
    icon: str | None = None
    enabled: bool
    disabled_reason: str | None = None


class AssistantAboutOut(BaseModel):
    title: str
    subtitle: str
    greeting: str
    reads_label: str
    reads: list[str]
    footnote: str


class TurnIn(BaseModel):
    session_id: int | None = None
    starter_code: str | None = None
    action: str | None = None
    text: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def _exactly_one_request(self) -> TurnIn:
        given = [value for value in (self.starter_code, self.action, self.text) if value]
        if len(given) != 1:
            raise ValueError("send exactly one of starter_code, action or text")
        return self


class TurnOut(BaseModel):
    session_id: int
    turn_id: int
    run_id: int


class AssistantTurnOut(BaseModel):
    turn_id: int
    run_id: int
    question: str
    intent: str | None
    blocks: list[dict[str, Any]]
    run_state: str | None
    created_at: datetime


class AssistantSessionSummaryOut(BaseModel):
    session_id: int
    started_at: datetime
    title: str | None
    turn_count: int


class AssistantSessionOut(BaseModel):
    session_id: int
    started_at: datetime
    title: str | None
    turns: list[AssistantTurnOut]


class EmailDraftIn(BaseModel):
    turn_id: int
    option_id: str = Field(min_length=1)
