from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

SettingKind = Literal["int", "float", "bool", "text", "choice"]


class SettingValueError(ValueError):
    def __init__(self, key: str, message: str) -> None:
        super().__init__(f"{key}: {message}")
        self.key = key


@dataclass(frozen=True)
class EditableSetting:
    key: str
    kind: SettingKind
    minimum: float | None = None
    maximum: float | None = None
    choices: tuple[str, ...] = ()
    optional: bool = False
    max_length: int = 60

    def clean(self, value: Any) -> Any:
        if value is None:
            if self.optional:
                return None
            raise SettingValueError(self.key, "a value is required")
        if self.kind == "bool":
            return self._clean_bool(value)
        if self.kind == "choice":
            return self._clean_choice(value)
        if self.kind == "text":
            return self._clean_text(value)
        return self._clean_number(value)

    def _clean_bool(self, value: Any) -> bool:
        if not isinstance(value, bool):
            raise SettingValueError(self.key, "must be true or false")
        return value

    def _clean_choice(self, value: Any) -> str:
        if value not in self.choices:
            raise SettingValueError(self.key, f"must be one of {', '.join(self.choices)}")
        return value

    def _clean_text(self, value: Any) -> str:
        cleaned = value.strip() if isinstance(value, str) else ""
        if not cleaned:
            raise SettingValueError(self.key, "must not be blank")
        if len(cleaned) > self.max_length:
            raise SettingValueError(self.key, f"must be {self.max_length} characters or fewer")
        return cleaned

    def _clean_number(self, value: Any) -> int | float:
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise SettingValueError(self.key, "must be a number")
        if self.kind == "int":
            if isinstance(value, float) and not value.is_integer():
                raise SettingValueError(self.key, "must be a whole number")
            value = int(value)
        else:
            value = float(value)
        if (self.minimum is not None and value < self.minimum) or (
            self.maximum is not None and value > self.maximum
        ):
            raise SettingValueError(
                self.key, f"must be between {self.minimum:g} and {self.maximum:g}"
            )
        return value


def _whole(key: str, minimum: int, maximum: int, *, optional: bool = False) -> EditableSetting:
    return EditableSetting(key, "int", minimum, maximum, optional=optional)


def _fraction(key: str, *, optional: bool = False) -> EditableSetting:
    return EditableSetting(key, "float", 0, 1, optional=optional)


def _switch(key: str) -> EditableSetting:
    return EditableSetting(key, "bool")


def _choice(key: str, *choices: str) -> EditableSetting:
    return EditableSetting(key, "choice", choices=choices)


def _model_limits(prefix: str) -> tuple[EditableSetting, ...]:
    return (
        _fraction(f"{prefix}_temperature", optional=True),
        _whole(f"{prefix}_max_tokens", 256, 8192),
    )


EDITABLE_SETTINGS: tuple[EditableSetting, ...] = (
    _choice("delivery_mode", "test", "live"),
    _switch("live_sms_to_test_list"),
    _switch("dispatch_direct_send"),
    _whole("dispatch_direct_send_max_batch", 1, 500),
    _whole("live_campaign_max_clients", 0, 5000),
    _whole("campaign_cooldown_days", 0, 90),
    _switch("require_deliverable_contact"),
    _whole("test_campaign_max_clients", 1, 500),
    EditableSetting("test_client_first_name", "text", max_length=60),
    EditableSetting("test_subject_prefix", "text", max_length=30),
    *_model_limits("llm"),
    *_model_limits("judge_llm"),
    *_model_limits("briefing_llm"),
    *_model_limits("agent_llm"),
    _switch("rag_enabled"),
    _whole("rag_retrieval_k", 1, 10),
    _fraction("rag_min_score"),
    _whole("agent_new_client_days", 1, 365),
    _whole("agent_awaiting_call_days", 1, 30),
    _whole("agent_contact_cooldown_days", 0, 90),
    _whole("agent_daily_send_limit", 0, 10000, optional=True),
    _switch("agent_force_approve_each"),
    _switch("ai_briefing_enabled"),
    _whole("agent_query_call_budget", 1, 50),
    _whole("agent_investigation_max_turns", 1, 20),
    _whole("agent_investigation_concurrency", 1, 8),
    _whole("agent_insight_write_cap", 1, 100),
    _whole("agent_query_min_group_size", 3, 50),
    _choice("prompt_config_source", "hardcoded", "db"),
    _choice("signal_situation_source", "legacy", "situations"),
)

EDITABLE_SETTINGS_BY_KEY: dict[str, EditableSetting] = {s.key: s for s in EDITABLE_SETTINGS}


def editable_setting(key: str) -> EditableSetting:
    spec = EDITABLE_SETTINGS_BY_KEY.get(key)
    if spec is None:
        raise SettingValueError(key, "is not a setting that can be changed here")
    return spec
