from __future__ import annotations

from pydantic import BaseModel, Field


class ActionMixOut(BaseModel):
    action_code: str
    title: str
    content_mix: str
    default_permission: str
    response_kind: str
    sends_message: bool
    paused: bool
    version: int


class ActionMixListOut(BaseModel):
    version: int | None
    actions: list[ActionMixOut]


class SetContentMixIn(BaseModel):
    content_mix: str = Field(min_length=1)


class SetContentMixOut(BaseModel):
    action_code: str
    content_mix: str
    version: int
