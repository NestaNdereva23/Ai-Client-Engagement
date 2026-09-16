from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.agents.scenario_studio import ScenarioInput, evaluate_scenario
from app.agents.situation_action_mapping import (
    MappingSpec,
    SituationActionMappingValidationError,
)
from app.agents.watchlist import WatchlistConfigMissing, WatchlistThresholds, load_thresholds
from app.api.reviewer_auth import get_current_reviewer_id
from app.db.session import get_session
from app.rules.versioning import VersioningError
from app.schemas.agent_studio import (
    AngleBriefOut,
    BatchSimulationOut,
    ConfigurationSummaryOut,
    ConfigVersionDiffOut,
    ConfigVersionSummaryOut,
    PermissionSettingOut,
    ScenarioRequest,
    ScenarioResponseOut,
    SituationCompareRowOut,
    SituationCountOut,
    SituationMappingDraftOut,
    SituationMappingDraftRequest,
    SituationMappingOut,
    SituationMappingPublishRequest,
    SituationMappingRowOut,
    SnapshotComparisonOut,
)
from app.services.agent_studio import (
    angle_brief,
    compare_configuration_versions,
    compare_situation_snapshots,
    configuration_versions,
    current_configuration,
    current_situation_mapping,
    discard_situation_mapping_draft,
    publish_situation_mapping_draft,
    run_batch_simulation,
    situation_mapping_pending_version,
    situation_mapping_versions,
    stage_situation_mapping_draft,
)

router = APIRouter(
    prefix="/agent-studio",
    tags=["agent_studio"],
    dependencies=[Depends(get_current_reviewer_id)],
)


def _thresholds(session: Session, body: ScenarioRequest, as_of: date) -> WatchlistThresholds:
    overrides = (
        body.new_client_days,
        body.months_until_empty_threshold,
        body.small_balance_kes,
        body.awaiting_call_days,
    )
    if all(value is not None for value in overrides):
        return WatchlistThresholds(
            new_client_days=body.new_client_days,
            months_until_empty=body.months_until_empty_threshold,
            small_balance=body.small_balance_kes,
            awaiting_call_days=body.awaiting_call_days,
        )
    try:
        live = load_thresholds(session, as_of)
    except WatchlistConfigMissing:
        raise HTTPException(
            status_code=409,
            detail=(
                "no risk config version is in force, and the scenario "
                "did not supply every threshold"
            ),
        ) from None
    return WatchlistThresholds(
        new_client_days=body.new_client_days or live.new_client_days,
        months_until_empty=body.months_until_empty_threshold or live.months_until_empty,
        small_balance=body.small_balance_kes or live.small_balance,
        awaiting_call_days=body.awaiting_call_days or live.awaiting_call_days,
    )


@router.post("/scenario", response_model=ScenarioResponseOut)
def post_scenario(
    body: ScenarioRequest, session: Session = Depends(get_session)
) -> ScenarioResponseOut:
    as_of = body.as_of or date.today()
    thresholds = _thresholds(session, body, as_of)
    scenario = ScenarioInput(
        balance=body.balance,
        n_deposits=body.n_deposits,
        first_deposit_days_ago=body.first_deposit_days_ago,
        months_until_empty=body.months_until_empty,
        sig_dormant=body.sig_dormant,
        sig_shrinking=body.sig_shrinking,
        risk_band=body.risk_band,
        funds_held=body.funds_held,
        days_since_call_flagged=body.days_since_call_flagged,
        call_logged_since_flagged=body.call_logged_since_flagged,
        route_moved_more_urgent=body.route_moved_more_urgent,
        on_call_list=body.on_call_list,
    )
    result = evaluate_scenario(
        session, scenario, thresholds, as_of=as_of, money_ceiling_kes=body.money_ceiling_kes
    )
    return ScenarioResponseOut(
        matched=result.matched,
        winner=result.winner,
        folded=result.folded,
        action_code=result.action_code,
        action_title=result.action_title,
        permission=result.permission,
        outcome=result.outcome,
        reason=result.reason,
    )


@router.get("/batch-simulation", response_model=BatchSimulationOut)
def get_batch_simulation(
    as_of: date | None = Query(default=None), session: Session = Depends(get_session)
) -> BatchSimulationOut:
    result = run_batch_simulation(session, as_of or date.today())
    return BatchSimulationOut(
        as_of=result.as_of,
        scanned=result.scanned,
        matched=result.matched,
        multi_match_consolidated=result.multi_match_consolidated,
        money_total_kes=result.money_total_kes,
        by_situation=tuple(
            SituationCountOut(
                situation=row.situation,
                client_funds=row.client_funds,
                money_total_kes=row.money_total_kes,
            )
            for row in result.by_situation
        ),
        auto_queued=result.auto_queued,
        needs_approval=result.needs_approval,
        trimmed_by_daily_cap=result.trimmed_by_daily_cap,
    )


@router.get("/replay", response_model=SnapshotComparisonOut)
def get_replay(
    date_a: date = Query(...), date_b: date = Query(...), session: Session = Depends(get_session)
) -> SnapshotComparisonOut:
    result = compare_situation_snapshots(session, date_a, date_b)
    return SnapshotComparisonOut(
        date_a=result.date_a,
        date_b=result.date_b,
        rows=tuple(
            SituationCompareRowOut(
                situation=row.situation, count_a=row.count_a, count_b=row.count_b
            )
            for row in result.rows
        ),
    )


@router.get("/configuration", response_model=ConfigurationSummaryOut)
def get_configuration(
    as_of: date | None = Query(default=None), session: Session = Depends(get_session)
) -> ConfigurationSummaryOut:
    try:
        summary = current_configuration(session, as_of or date.today())
    except WatchlistConfigMissing:
        raise HTTPException(status_code=409, detail="no risk config version is in force") from None
    return ConfigurationSummaryOut(
        as_of=summary.as_of,
        risk_config_version=summary.risk_config_version,
        small_balance_kes=summary.small_balance_kes,
        months_until_empty=summary.months_until_empty,
        awaiting_call_days=summary.awaiting_call_days,
        new_client_days=summary.new_client_days,
        situation_priority=summary.situation_priority,
        daily_send_limit=summary.daily_send_limit,
        first_run_limit=summary.first_run_limit,
        kill_switch_active=summary.kill_switch_active,
        permissions=tuple(
            PermissionSettingOut(
                action_code=row.action_code,
                priority_tier=row.priority_tier,
                risk_band=row.risk_band,
                permission=row.permission,
                max_clients_per_day=row.max_clients_per_day,
                max_money_kes=row.max_money_kes,
            )
            for row in summary.permissions
        ),
    )


@router.get("/configuration/versions", response_model=list[ConfigVersionSummaryOut])
def get_configuration_versions(
    session: Session = Depends(get_session),
) -> list[ConfigVersionSummaryOut]:
    return [ConfigVersionSummaryOut(**row) for row in configuration_versions(session)]


@router.get("/configuration/compare", response_model=ConfigVersionDiffOut)
def get_configuration_compare(
    version_a: int = Query(...),
    version_b: int = Query(...),
    session: Session = Depends(get_session),
) -> ConfigVersionDiffOut:
    diff = compare_configuration_versions(session, version_a, version_b)
    return ConfigVersionDiffOut(fields=diff)


@router.get("/situation-mapping", response_model=SituationMappingOut)
def get_situation_mapping(
    as_of: date | None = Query(default=None), session: Session = Depends(get_session)
) -> SituationMappingOut:
    at = as_of or date.today()
    rows = current_situation_mapping(session, at)
    version = rows[0].version if rows else None
    return SituationMappingOut(
        version=version,
        rows=tuple(
            SituationMappingRowOut(
                situation=row.situation,
                action_code=row.action_code,
                objective=row.objective,
                angle=row.angle,
                evidence_required=row.evidence_required,
                channel=row.channel,
            )
            for row in rows
        ),
        pending_version=situation_mapping_pending_version(session),
    )


@router.get("/situation-mapping/versions", response_model=list[ConfigVersionSummaryOut])
def get_situation_mapping_versions(
    session: Session = Depends(get_session),
) -> list[ConfigVersionSummaryOut]:
    return [ConfigVersionSummaryOut(**row) for row in situation_mapping_versions(session)]


@router.post("/situation-mapping/draft", response_model=SituationMappingDraftOut)
def post_situation_mapping_draft(
    body: SituationMappingDraftRequest,
    reviewer_id: str = Depends(get_current_reviewer_id),
    session: Session = Depends(get_session),
) -> SituationMappingDraftOut:
    mappings = [
        MappingSpec(
            situation=row.situation,
            action_code=row.action_code,
            objective=row.objective,
            angle=row.angle,
            evidence_required=row.evidence_required,
            channel=row.channel,
        )
        for row in body.rows
    ]
    try:
        version = stage_situation_mapping_draft(session, mappings, by=reviewer_id)
        session.commit()
    except SituationActionMappingValidationError as exc:
        session.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return SituationMappingDraftOut(version=version)


@router.post("/situation-mapping/{version}/publish", response_model=SituationMappingDraftOut)
def post_situation_mapping_publish(
    version: int,
    body: SituationMappingPublishRequest,
    reviewer_id: str = Depends(get_current_reviewer_id),
    session: Session = Depends(get_session),
) -> SituationMappingDraftOut:
    try:
        publish_situation_mapping_draft(session, version, by=reviewer_id, at=body.at)
        session.commit()
    except VersioningError as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from None
    return SituationMappingDraftOut(version=version)


@router.post("/situation-mapping/{version}/discard", response_model=SituationMappingDraftOut)
def post_situation_mapping_discard(
    version: int, session: Session = Depends(get_session)
) -> SituationMappingDraftOut:
    try:
        discard_situation_mapping_draft(session, version)
        session.commit()
    except VersioningError as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from None
    return SituationMappingDraftOut(version=version)


@router.get("/angles/{angle}", response_model=AngleBriefOut)
def get_angle_brief(
    angle: str, as_of: date | None = Query(default=None), session: Session = Depends(get_session)
) -> AngleBriefOut:
    at = as_of or date.today()
    row = angle_brief(session, angle, at)
    if row is None:
        raise HTTPException(status_code=404, detail="angle not found in the catalogue in force")
    return AngleBriefOut(
        angle=row.angle,
        headline=row.headline,
        who=row.who,
        claim=row.claim,
        ask=row.ask,
        never=row.never,
        use=row.use,
        held=row.held,
    )
