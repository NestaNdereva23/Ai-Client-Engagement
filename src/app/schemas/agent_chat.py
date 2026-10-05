from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ChatAskIn(BaseModel):
    question: str = Field(min_length=1)
    session_id: int | None = None


class ChatAskOut(BaseModel):
    session_id: int
    turn_id: int
    run_id: int


class ChatTurnOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    turn_id: int
    session_id: int
    run_id: int
    question: str
    answer: str | None
    created_at: datetime
    run_state: str | None = None


class ChatSessionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    session_id: int
    started_at: datetime
    title: str | None
    turns: list[ChatTurnOut] = []
