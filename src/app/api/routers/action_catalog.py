from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.agents.action_catalog import (
    ActionCatalogValidationError,
    active_action_catalog_version,
    load_action,
    load_active_actions,
    set_action_content_mix,
    set_action_paused,
)
from app.agents.fa_scope import fa_scope
from app.agents.permissions import (
    GROUP_FIELDS,
    GroupSettings,
    PermissionValidationError,
    effective_permission,
    group_row,
    load_group_settings,
    save_group_setting,
    set_permission,
)
from app.agents.propose import default_action_code_for_group
from app.agents.situation_action_mapping import load_active_mappings
from app.agents.watchlist import GROUP_NAMES, situation_group
from app.api.agent_scope import AgentScope, get_agent_scope
from app.api.reviewer_auth import get_current_reviewer_id
from app.db.models.agent import AgentActionCatalog
from app.db.models.agent_permission import AgentPermission
from app.db.session import get_session
from app.schemas.action_catalog import (
    ActionMixListOut,
    ActionMixOut,
    GroupCountsOut,
    GroupSettingListOut,
    GroupSettingOut,
    GroupValues,
    SaveGroupSettingsIn,
    SaveGroupSettingsOut,
    SetContentMixIn,
    SetContentMixOut,
    SetPausedIn,
    SetPausedOut,
    SetPermissionIn,
    SetPermissionOut,
)

router = APIRouter(
    prefix="/agent/actions",
    tags=["agent_actions"],
    dependencies=[Depends(get_current_reviewer_id)],
)


@router.get("", response_model=ActionMixListOut)
def list_action_mixes(session: Session = Depends(get_session)) -> ActionMixListOut:
    today = date.today()
    version = active_action_catalog_version(session, today)
    rows = load_active_actions(session, today)

    situations_by_action: dict[str, list[str]] = {}
    for mapping in load_active_mappings(session, today):
        situations_by_action.setdefault(mapping.action_code, []).append(mapping.situation)

    def sort_key(row: AgentActionCatalog) -> tuple[bool, str]:
        return (not situations_by_action.get(row.action_code), row.action_code)

    actions = [
        ActionMixOut(
            action_code=row.action_code,
            title=row.title,
            content_mix=row.content_mix,
            permission=effective_permission(session, row.action_code, at=today),
            response_kind=row.response_kind,
            sends_message=bool(row.message_angle),
            paused=row.paused,
            version=row.version,
            situations=situations_by_action.get(row.action_code, []),
        )
        for row in sorted(rows.values(), key=sort_key)
    ]
    return ActionMixListOut(version=version, actions=actions)


@router.get("/groups", response_model=GroupSettingListOut)
def list_group_settings(
    scope: AgentScope = Depends(get_agent_scope),
    session: Session = Depends(get_session),
) -> GroupSettingListOut:
    today = date.today()
    groups = []
    with fa_scope(scope.fa_id):
        for name in GROUP_NAMES:
            action_code = default_action_code_for_group(session, name, today)
            action = load_action(session, action_code, today) if action_code else None
            all_row = group_row(session, name, None, today)
            own_row = group_row(session, name, scope.fa_id, today) if scope.fa_id else None
            settings = load_group_settings(session, name, scope.fa_id, today)
            effective = GroupValues(
                **{field: getattr(settings, field) for field in GROUP_FIELDS},
            )
            if action_code is not None:
                effective.permission = effective_permission(
                    session, action_code, at=today, group_name=name
                )
            groups.append(
                GroupSettingOut(
                    group_name=name,
                    action_code=action_code,
                    action_title=None if action is None else action.title,
                    sends_message=bool(action and action.message_angle),
                    all_advisers=_values(all_row),
                    own=None if scope.fa_id is None else _values(own_row),
                    effective=effective,
                    counts=_counts(session, name, settings),
                )
            )
    return GroupSettingListOut(fa_id=scope.fa_id, groups=groups)


@router.put("/groups/settings", response_model=SaveGroupSettingsOut)
def save_group_settings(
    body: SaveGroupSettingsIn,
    reviewer_id: str = Depends(get_current_reviewer_id),
    scope: AgentScope = Depends(get_agent_scope),
    session: Session = Depends(get_session),
) -> SaveGroupSettingsOut:
    # An adviser only ever writes their own rows; all advisers rows need the everyone view.
    for change in body.changes:
        if change.group_name not in GROUP_NAMES:
            raise HTTPException(status_code=422, detail=f"unknown group '{change.group_name}'")
        try:
            save_group_setting(
                session,
                change.group_name,
                change.values.model_dump(),
                fa_id=scope.fa_id,
                changed_by=reviewer_id,
                changed_reason=body.reason,
            )
        except PermissionValidationError as exc:
            session.rollback()
            raise HTTPException(status_code=422, detail=str(exc)) from None
    session.commit()
    return SaveGroupSettingsOut(saved=len(body.changes))


def _values(row: AgentPermission | None) -> GroupValues:
    if row is None:
        return GroupValues()
    return GroupValues(**{field: getattr(row, field) for field in GROUP_FIELDS})


def _counts(session: Session, group_name: str, settings: GroupSettings) -> GroupCountsOut:
    # Counted before the usual checks such as do not contact and recent contact.
    members = situation_group(session, group_name).members
    in_range = [m for m in members if settings.balance_allows(m.balance)]
    clients = len({m.client_id for m in in_range})
    limit = settings.max_clients_per_day
    return GroupCountsOut(
        in_group=len({m.client_id for m in members}),
        in_range=clients,
        would_join=0 if settings.paused else (min(clients, limit) if limit else clients),
    )


@router.post("/{action_code}/content-mix", response_model=SetContentMixOut)
def set_content_mix(
    action_code: str,
    body: SetContentMixIn,
    reviewer_id: str = Depends(get_current_reviewer_id),
    session: Session = Depends(get_session),
) -> SetContentMixOut:
    try:
        version = set_action_content_mix(
            session, action_code, body.content_mix, changed_by=reviewer_id
        )
    except ActionCatalogValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    session.commit()
    return SetContentMixOut(action_code=action_code, content_mix=body.content_mix, version=version)


@router.post("/{action_code}/paused", response_model=SetPausedOut)
def set_paused(
    action_code: str,
    body: SetPausedIn,
    reviewer_id: str = Depends(get_current_reviewer_id),
    session: Session = Depends(get_session),
) -> SetPausedOut:
    try:
        version = set_action_paused(session, action_code, body.paused, changed_by=reviewer_id)
    except ActionCatalogValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    session.commit()
    return SetPausedOut(action_code=action_code, paused=body.paused, version=version)


@router.post("/{action_code}/permission", response_model=SetPermissionOut)
def set_permission_level(
    action_code: str,
    body: SetPermissionIn,
    reviewer_id: str = Depends(get_current_reviewer_id),
    session: Session = Depends(get_session),
) -> SetPermissionOut:
    try:
        set_permission(
            session,
            action_code,
            body.permission,
            changed_by=reviewer_id,
            changed_reason=body.reason,
        )
    except PermissionValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    session.commit()
    return SetPermissionOut(action_code=action_code, permission=body.permission)
