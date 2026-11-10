from __future__ import annotations

from pydantic import BaseModel, Field


class ActionMixOut(BaseModel):
    action_code: str
    title: str
    content_mix: str
    permission: str
    response_kind: str
    sends_message: bool
    paused: bool
    version: int
    situations: list[str]


class ActionMixListOut(BaseModel):
    version: int | None
    actions: list[ActionMixOut]


class SetContentMixIn(BaseModel):
    content_mix: str = Field(min_length=1)


class SetContentMixOut(BaseModel):
    action_code: str
    content_mix: str
    version: int


class SetPausedIn(BaseModel):
    paused: bool


class SetPausedOut(BaseModel):
    action_code: str
    paused: bool
    version: int


class SetPermissionIn(BaseModel):
    permission: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class SetPermissionOut(BaseModel):
    action_code: str
    permission: str


class GroupValues(BaseModel):
    # Empty follows the wider setting. max_clients_per_day of 0 means no limit.
    permission: str | None = None
    balance_filter: bool | None = None
    min_balance: float | None = Field(default=None, ge=0)
    max_balance: float | None = Field(default=None, ge=0)
    max_clients_per_day: int | None = Field(default=None, ge=0, le=5000)
    content_mix: str | None = None
    guide_doc_ids: list[int] | None = None
    use_rates: bool | None = None
    no_repeat: bool | None = None
    paused: bool | None = None


class GroupCountsOut(BaseModel):
    in_group: int
    in_range: int
    would_join: int


class GroupSettingOut(BaseModel):
    group_name: str
    action_code: str | None
    action_title: str | None
    sends_message: bool
    all_advisers: GroupValues
    own: GroupValues | None
    effective: GroupValues
    counts: GroupCountsOut


class GroupSettingListOut(BaseModel):
    fa_id: str | None
    groups: list[GroupSettingOut]


class GroupChangeIn(BaseModel):
    group_name: str = Field(min_length=1)
    values: GroupValues


class SaveGroupSettingsIn(BaseModel):
    reason: str | None = None
    changes: list[GroupChangeIn] = Field(min_length=1)


class SaveGroupSettingsOut(BaseModel):
    saved: int
