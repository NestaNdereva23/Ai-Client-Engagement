from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class AppSettingOut(BaseModel):
    key: str
    value: Any
    kind: str
    minimum: float | None
    maximum: float | None
    choices: list[str]
    optional: bool
    updated_by: str | None
    updated_at: datetime | None


class ModelInUseOut(BaseModel):
    job: str
    model: str
    same_as_writer: bool


class AppSettingsOut(BaseModel):
    settings: list[AppSettingOut]
    models: list[ModelInUseOut]


class AppSettingsUpdate(BaseModel):
    changes: dict[str, Any] = Field(min_length=1)
    reason: str = Field(min_length=5, max_length=500)
    confirm_live: bool = False
