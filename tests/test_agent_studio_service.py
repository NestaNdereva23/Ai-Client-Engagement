from __future__ import annotations

from datetime import date, datetime, time
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, select

from app.agents.permissions import set_permission
from app.agents.situation_action_mapping import (
    MappingSpec,
    SituationActionMappingValidationError,
)
from app.db.models.active_clients import ActiveClientFund
from app.db.models.agent_permission import AgentPermission
from app.db.models.prompt_config import ActiveConfiguration
from app.db.models.risk import RiskConfigVersion
from app.db.models.signals import ClientSituationSnapshot, ClientSituationState, SignalRun
from app.db.session import SessionLocal
from app.risk.store import save_config_version
from app.services.agent_studio import (
    compare_situation_snapshots,
    current_configuration,
    discard_situation_mapping_draft,
    run_batch_simulation,
    situation_mapping_pending_version,
    stage_situation_mapping_draft,
)

AS_OF = date(2026, 9, 15)

FUND_ID = 9860
CALL_URGENT_CLIENT = 986001
HEALTHY_CLIENT = 986002
MULTI_MATCH_CLIENT = 986003

CLIENT_IDS = (CALL_URGENT_CLIENT, HEALTHY_CLIENT, MULTI_MATCH_CLIENT)

RUN_ID = "agent-studio-batch-test-run"

_FAR_FUTURE = date(2099, 1, 1)
_CONFIG_VERSION = 950001

_WEIGHTS = {
    "sig_heavy_withdrawal": 30,
    "sig_dormant": 25,
    "sig_broken_pattern": 20,
    "sig_shrinking": 15,
    "sig_going_dormant": 7,
    "sig_never_repeated": 3,
}
_THRESHOLDS = {
    "DORMANT_DAYS": 365,
    "HEAVY_WITHDRAWAL_PCT": 0.50,
    "OVERDUE_MULTIPLE": 3.0,
    "SHRINKING_TREND": -0.10,
    "TINY_BALANCE": 100,
    "WORTH_A_CALL_BALANCE": 10_000,
    "MONTHS_UNTIL_EMPTY": 12,
    "FEE_PER_MONTH": 50,
    "SYSTEM_FEE_MAX": 100,
    "RISK_BAND_CUTOFFS": [0, 24, 49, 74],
}


def _fund(client_id: int, **overrides) -> ActiveClientFund:
    row = dict(
        client_id=client_id,
        unit_fund_id=FUND_ID,
        balance=150_000.0,
        n_deposits=5,
        n_withdrawals=0,
    )
    row.update(overrides)
    return ActiveClientFund(**row)


def _state(client_id: int, situation_code: str) -> ClientSituationState:
    return ClientSituationState(
        client_id=client_id,
        unit_fund_id=FUND_ID,
        situation_code=situation_code,
        is_active=True,
        signal_codes=[],
        since=AS_OF,
        run_id=RUN_ID,
    )


def _purge(session, client_ids: tuple[int, ...]) -> None:
    session.execute(
        delete(ClientSituationState).where(ClientSituationState.client_id.in_(client_ids))
    )
    session.execute(
        delete(ClientSituationSnapshot).where(ClientSituationSnapshot.client_id.in_(client_ids))
    )
    session.execute(delete(SignalRun).where(SignalRun.run_id == RUN_ID))
    session.execute(delete(ActiveClientFund).where(ActiveClientFund.client_id.in_(client_ids)))
    session.commit()


@pytest.fixture
def book(db: None):
    with SessionLocal() as session:
        _purge(session, CLIENT_IDS)
        session.add(SignalRun(run_id=RUN_ID, state="completed"))
        session.flush()
        session.add_all(
            [_fund(CALL_URGENT_CLIENT), _fund(HEALTHY_CLIENT), _fund(MULTI_MATCH_CLIENT)]
        )
        session.add_all(
            [
                _state(CALL_URGENT_CLIENT, "follow_up_overdue"),
                _state(HEALTHY_CLIENT, "single_fund_healthy"),
                _state(MULTI_MATCH_CLIENT, "fee_pressure_gone_quiet"),
                _state(MULTI_MATCH_CLIENT, "contribution_decline"),
            ]
        )
        session.commit()

    yield

    with SessionLocal() as session:
        _purge(session, CLIENT_IDS)


def test_batch_simulation_counts_client_funds_and_consolidates_multi_matches(book: None) -> None:
    with SessionLocal() as session:
        result = run_batch_simulation(session, AS_OF)

    assert result.matched == 3
    assert result.multi_match_consolidated == 1
    by_name = {row.situation: row.client_funds for row in result.by_situation}
    assert by_name["waiting_on_a_call"] == 1
    assert by_name["healthy_one_fund"] == 1
    # the multi-match client is counted once, under its higher-priority situation only
    assert by_name["fee_pressure_gone_quiet"] == 1
    assert by_name["getting_smaller"] == 0


def test_batch_simulation_everyone_needs_approval_without_a_permission_row(book: None) -> None:
    with SessionLocal() as session:
        result = run_batch_simulation(session, AS_OF)

    assert result.auto_queued == 0
    assert result.needs_approval == 3


def test_batch_simulation_auto_queues_the_action_with_permission_to_act_alone(book: None) -> None:
    with SessionLocal() as session:
        before = session.scalar(
            select(AgentPermission.permission).where(
                AgentPermission.action_code == "suggest_second_fund",
                AgentPermission.priority_tier.is_(None),
                AgentPermission.risk_band.is_(None),
            )
        )
        set_permission(
            session,
            "suggest_second_fund",
            "act_alone",
            changed_by="agent-studio-service-test",
            changed_reason="let the batch simulation test the auto-queue path",
        )
        session.commit()

    try:
        with SessionLocal() as session:
            result = run_batch_simulation(session, AS_OF)
    finally:
        with SessionLocal() as session:
            if before is None:
                session.execute(
                    delete(AgentPermission).where(
                        AgentPermission.action_code == "suggest_second_fund",
                        AgentPermission.priority_tier.is_(None),
                        AgentPermission.risk_band.is_(None),
                    )
                )
            else:
                set_permission(
                    session,
                    "suggest_second_fund",
                    before,
                    changed_by="agent-studio-service-test",
                    changed_reason="restore the setting this test found in place",
                )
            session.commit()

    assert result.auto_queued == 1
    assert result.needs_approval == 2


@pytest.fixture
def snapshot_dates(db: None):
    early = datetime.combine(date(2026, 8, 18), time(6, 0))
    late = datetime.combine(date(2026, 9, 1), time(6, 0))
    with SessionLocal() as session:
        _purge(session, CLIENT_IDS)
        session.add(SignalRun(run_id=RUN_ID, state="completed"))
        session.flush()
        session.add_all(
            [
                ClientSituationSnapshot(
                    run_id=RUN_ID,
                    client_id=CALL_URGENT_CLIENT,
                    unit_fund_id=FUND_ID,
                    situation_code="follow_up_overdue",
                    is_active=True,
                    signal_codes=[],
                    created_at=early,
                ),
                ClientSituationSnapshot(
                    run_id=RUN_ID,
                    client_id=HEALTHY_CLIENT,
                    unit_fund_id=FUND_ID,
                    situation_code="follow_up_overdue",
                    is_active=True,
                    signal_codes=[],
                    created_at=late,
                ),
            ]
        )
        session.commit()

    yield date(2026, 8, 18), date(2026, 9, 1)

    with SessionLocal() as session:
        _purge(session, CLIENT_IDS)


def test_replay_compares_situation_counts_between_two_dates(snapshot_dates) -> None:
    date_a, date_b = snapshot_dates

    with SessionLocal() as session:
        comparison = compare_situation_snapshots(session, date_a, date_b)

    row = next(r for r in comparison.rows if r.situation == "waiting_on_a_call")
    assert row.count_a == 1
    assert row.count_b == 2


@pytest.fixture
def risk_config():
    with SessionLocal() as session:
        save_config_version(
            session,
            _CONFIG_VERSION,
            _WEIGHTS,
            _THRESHOLDS,
            fa_call_capacity=150,
            at_risk_min=25,
            valid_from=_FAR_FUTURE,
        )
        session.commit()

    yield

    with SessionLocal() as session:
        session.execute(
            delete(RiskConfigVersion).where(RiskConfigVersion.version == _CONFIG_VERSION)
        )
        session.execute(
            delete(ActiveConfiguration).where(
                ActiveConfiguration.component_type == "risk_config_version",
                ActiveConfiguration.active_version == _CONFIG_VERSION,
            )
        )
        session.commit()


def test_current_configuration_reads_the_live_risk_config(risk_config) -> None:
    with SessionLocal() as session:
        summary = current_configuration(session, _FAR_FUTURE)

    assert summary.risk_config_version == _CONFIG_VERSION
    assert summary.months_until_empty == _THRESHOLDS["MONTHS_UNTIL_EMPTY"]
    assert summary.small_balance_kes == _THRESHOLDS["TINY_BALANCE"]
    assert summary.situation_priority[0] == "more_urgent_but_not_called"


def test_current_configuration_reports_the_kill_switch(risk_config, monkeypatch) -> None:
    import app.services.agent_studio as agent_studio_module

    monkeypatch.setattr(
        agent_studio_module,
        "get_settings",
        lambda: SimpleNamespace(
            agent_daily_send_limit=None, agent_first_run_limit=25, agent_force_approve_each=True
        ),
    )

    with SessionLocal() as session:
        summary = current_configuration(session, _FAR_FUTURE)

    assert summary.kill_switch_active is True


_VALID_ROWS = [
    MappingSpec(
        situation="healthy_one_fund",
        action_code="suggest_second_fund",
        objective="encourage",
        angle="wrong_shelf",
        evidence_required="a healthy risk band and exactly one fund",
        channel="email",
    )
]


def test_staging_an_invalid_draft_is_refused(db: None) -> None:
    bad_rows = [
        MappingSpec(
            situation="healthy_one_fund",
            action_code="suggest_second_fund",
            objective="not_a_real_objective",
            angle="wrong_shelf",
            evidence_required="a healthy risk band",
            channel="email",
        )
    ]
    with pytest.raises(SituationActionMappingValidationError):
        with SessionLocal() as session:
            stage_situation_mapping_draft(session, bad_rows, by="agent-studio-test")


def test_staging_and_discarding_a_valid_draft(db: None) -> None:
    with SessionLocal() as session:
        version = stage_situation_mapping_draft(session, _VALID_ROWS, by="agent-studio-test")
        session.commit()

    try:
        with SessionLocal() as session:
            pending = situation_mapping_pending_version(session)
        assert pending == version
    finally:
        with SessionLocal() as session:
            discard_situation_mapping_draft(session, version)
            session.commit()

    with SessionLocal() as session:
        pending_after = situation_mapping_pending_version(session)
    assert pending_after != version
