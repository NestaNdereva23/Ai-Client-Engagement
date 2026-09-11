"""The structured shape one group's chosen action takes: one action, one
angle, and a reason. The choose step's model records this by calling a tool,
so this module only holds the shape itself; catalogue-aware checks (is this
group offered tonight, is this action code real, does the angle match) live
next to that tool, where the live catalogue is in scope.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, field_validator


class AgentChoice(BaseModel):
    """One group's chosen action, already checked against the live catalogue
    by the tool that built it.
    """

    model_config = ConfigDict(extra="forbid")

    group_name: str
    action_code: str
    angle: str | None = None
    reason: str

    @field_validator("group_name", "action_code", "reason")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value
