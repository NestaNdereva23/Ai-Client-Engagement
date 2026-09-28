from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.log import record_audit
from app.config import Settings, get_settings
from app.db.models.app_setting import AppSetting
from app.editable_settings import (
    EDITABLE_SETTINGS,
    EditableSetting,
    SettingValueError,
    editable_setting,
)
from app.schemas.app_settings import AppSettingOut, AppSettingsOut, ModelInUseOut

_MODEL_JOBS = (
    ("writer", "llm_model"),
    ("judge", "judge_llm_model"),
    ("briefing", "briefing_llm_model"),
    ("agent", "agent_llm_model"),
)


def list_app_settings(session: Session) -> AppSettingsOut:
    settings = get_settings()
    rows = {row.key: row for row in session.scalars(select(AppSetting))}
    return AppSettingsOut(
        settings=[_setting_out(spec, settings, rows.get(spec.key)) for spec in EDITABLE_SETTINGS],
        models=_models_in_use(settings),
    )


def update_app_settings(
    session: Session,
    *,
    changes: dict[str, Any],
    reason: str,
    confirm_live: bool,
    actor_id: str,
) -> None:
    cleaned = {key: editable_setting(key).clean(value) for key, value in changes.items()}
    current = get_settings()
    live_key = _turns_live_on(cleaned, current)
    if live_key and not confirm_live:
        raise SettingValueError(live_key, "sending to real clients needs confirmation")
    rows = {
        row.key: row
        for row in session.scalars(select(AppSetting).where(AppSetting.key.in_(cleaned)))
    }
    for key, value in cleaned.items():
        previous = getattr(current, key)
        if previous == value:
            continue
        _store(session, rows.get(key), key, value, actor_id)
        record_audit(
            session,
            entity_type="app_setting",
            entity_id=key,
            action="updated",
            actor_id=actor_id,
            detail={"from": previous, "to": value, "reason": reason.strip()},
        )
    session.flush()


def _turns_live_on(cleaned: dict[str, Any], current: Settings) -> str | None:
    """The key of a change that would start sending to real clients, if any."""
    if cleaned.get("delivery_mode") == "live" and current.delivery_mode != "live":
        return "delivery_mode"
    if cleaned.get("live_sms_to_test_list") is False and current.live_sms_to_test_list:
        return "live_sms_to_test_list"
    return None


def _store(session: Session, row: AppSetting | None, key: str, value: Any, actor_id: str) -> None:
    if row is None:
        session.add(AppSetting(key=key, value=value, updated_by=actor_id))
        return
    row.value = value
    row.updated_by = actor_id


def _setting_out(
    spec: EditableSetting, settings: Settings, row: AppSetting | None
) -> AppSettingOut:
    return AppSettingOut(
        key=spec.key,
        value=getattr(settings, spec.key),
        kind=spec.kind,
        minimum=spec.minimum,
        maximum=spec.maximum,
        choices=list(spec.choices),
        optional=spec.optional,
        updated_by=row.updated_by if row else None,
        updated_at=row.updated_at if row else None,
    )


def _models_in_use(settings: Settings) -> list[ModelInUseOut]:
    writer = settings.llm_model
    jobs = []
    for job, field in _MODEL_JOBS:
        own = getattr(settings, field)
        jobs.append(
            ModelInUseOut(job=job, model=own or writer, same_as_writer=job != "writer" and not own)
        )
    return jobs
