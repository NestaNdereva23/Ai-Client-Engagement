from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import delete, select

from app.agents.signals import (
    _open_run,
    recompute_advisor_call_overdue_signal,
    recompute_new_client_signals,
    recompute_risk_band_rose_signal,
    recompute_segment_moved_up_signal,
    recompute_side_changed_downward_signal,
)
from app.agents.situations import (
    CROSSED_BELOW_LINE,
    DEPOSIT_SIZE_FALLING,
    FA_CALL_OVERDUE,
    FIRST_DEPOSIT_NO_REPEAT,
    MOVED_TO_ACTIVE_ACTIVE,
    RISK_ROSE_NO_CALL,
    SITUATION_CODES,
    recompute_crossed_below_line,
    recompute_deposit_size_falling,
    recompute_fa_call_overdue,
    recompute_first_deposit_no_repeat,
    recompute_moved_to_active_active,
    recompute_risk_rose_no_call,
)
from app.db.models.active_clients import (
    ActiveClientFund,
    ActiveClientInteraction,
    ActiveClientTouchpoint,
)
from app.db.models.risk import ClientRiskFeatures, RiskRun, RiskSnapshot
from app.db.models.routing import ClientSide, ClientTier
from app.db.models.signals import (
    ClientSignalSnapshot,
    ClientSignalState,
    ClientSituationSnapshot,
    ClientSituationState,
)
from app.db.session import SessionLocal
from app.risk.history import write_snapshot
from app.risk.routing import RouteResult
from app.risk.scoring import ScoreResult

AS_OF = date(2026, 10, 13)
FUND_ID = 98071
FIRST_CLIENT = 980701
CLIENT_IDS = tuple(range(FIRST_CLIENT, FIRST_CLIENT + 40))
PREVIOUS_RUN = "follow-up-test-previous-run"
CURRENT_RUN = "follow-up-test-current-run"

FALLING = FIRST_CLIENT
NOT_FALLING = FIRST_CLIENT + 1
FALLING_BUT_HELD_OUT = FIRST_CLIENT + 2
NO_REPEAT = FIRST_CLIENT + 3
REPEATS = FIRST_CLIENT + 4
OLD_SINGLE_DEPOSIT = FIRST_CLIENT + 5
NO_REPEAT_BUT_HELD_OUT = FIRST_CLIENT + 6
CALL_OVERDUE = FIRST_CLIENT + 7
CALL_DUE_TODAY = FIRST_CLIENT + 8
MEETING_OVERDUE = FIRST_CLIENT + 9
CALL_DONE = FIRST_CLIENT + 10
CALL_MADE_ANYWAY = FIRST_CLIENT + 11
CALL_OVERDUE_BUT_HELD_OUT = FIRST_CLIENT + 12
RISK_ROSE = FIRST_CLIENT + 13
RISK_STEADY = FIRST_CLIENT + 14
RISK_FELL = FIRST_CLIENT + 15
ROSE_THEN_CALLED = FIRST_CLIENT + 16
CALLED_THEN_ROSE = FIRST_CLIENT + 17
ROSE_BUT_HELD_OUT = FIRST_CLIENT + 18
FIRST_RUN_ONLY = FIRST_CLIENT + 19
MOVED_UP = FIRST_CLIENT + 20
NOT_MOVED = FIRST_CLIENT + 21
DROPPED_WITH_EVERY_TRIGGER = FIRST_CLIENT + 22

SIGNALS = {
    "sig_heavy_withdrawal": False,
    "sig_dormant": False,
    "sig_broken_pattern": False,
    "sig_shrinking": False,
    "sig_going_dormant": False,
    "sig_never_repeated": False,
}


def _fund(client_id: int, **overrides) -> ActiveClientFund:
    row = dict(
        client_id=client_id,
        unit_fund_id=FUND_ID,
        balance=100_000.0,
        n_deposits=3,
        n_withdrawals=0,
    )
    row.update(overrides)
    return ActiveClientFund(**row)


def _tier(client_id: int, held_out: bool = False, **overrides) -> ClientTier:
    row = dict(
        client_id=client_id,
        purchased_recently=False,
        real_withdrawer=False,
        tier="below_capital_preservation_threshold" if held_out else "gradual_depositor",
        segment=None if held_out else "active_dormant",
        computed_at=datetime(2026, 10, 13, 6, 0, tzinfo=UTC),
        thresholds_version=1,
    )
    row.update(overrides)
    return ClientTier(**row)


def _risk_row(client_id: int, **signal_overrides) -> ClientRiskFeatures:
    return ClientRiskFeatures(
        client_id=client_id,
        unit_fund_id=FUND_ID,
        balance_tier="Small",
        **{**SIGNALS, **signal_overrides},
        risk_score=20,
        risk_band="Watch",
        risk_reasons="test",
        fund_at_risk=100.0,
        config_version=1,
        route="fa_action",
        queue_rank=None,
    )


def _snapshot(session, run_id: str, client_id: int, band: str) -> None:
    write_snapshot(
        session,
        run_id,
        client_id,
        FUND_ID,
        ScoreResult(
            risk_score=20,
            risk_band=band,
            risk_reasons="test",
            fund_at_risk=100.0,
            signals=SIGNALS,
            recency_band="1-3m",
            balance_tier="Small",
            value_tier="Medium",
        ),
        RouteResult(route="fa_action", queue_rank=None, complaint_caveat=False),
        config_version=1,
        pattern_is_reliable=True,
        overdue_multiple=1.0,
    )


def _call_touchpoint(client_id: int, **overrides) -> ActiveClientTouchpoint:
    row = dict(
        client_id=client_id,
        unit_fund_id=FUND_ID,
        kind="call",
        title="Call back",
        due_date=AS_OF - timedelta(days=2),
        status="open",
        created_by="fa-1",
        created_at=datetime(2026, 10, 1, 9, 0, tzinfo=UTC),
    )
    row.update(overrides)
    return ActiveClientTouchpoint(**row)


def _handled(client_id: int, band_at: str, **overrides) -> ActiveClientInteraction:
    row = dict(
        client_id=client_id,
        unit_fund_id=FUND_ID,
        type="call_logged",
        reviewer_id="fa-1",
        risk_band_at_interaction=band_at,
        created_at=datetime(2026, 10, 5, 9, 0, tzinfo=UTC),
    )
    row.update(overrides)
    return ActiveClientInteraction(**row)


def _purge(session) -> None:
    for model in (ActiveClientTouchpoint, ActiveClientInteraction):
        session.execute(delete(model).where(model.client_id.in_(CLIENT_IDS)))
    for model in (
        ClientSituationSnapshot,
        ClientSituationState,
        ClientSignalSnapshot,
        ClientSignalState,
        RiskSnapshot,
        ClientRiskFeatures,
        ActiveClientFund,
        ClientTier,
        ClientSide,
    ):
        session.execute(delete(model).where(model.client_id.in_(CLIENT_IDS)))
    session.execute(delete(RiskRun).where(RiskRun.run_id.in_([PREVIOUS_RUN, CURRENT_RUN])))
    session.commit()


def _first_deposit_days_ago(days: int) -> date:
    return AS_OF - timedelta(days=days)


@pytest.fixture
def book(db: None):
    with SessionLocal() as session:
        _purge(session)
        session.add_all(
            RiskRun(run_id=run_id, state="completed", config_version=1)
            for run_id in (PREVIOUS_RUN, CURRENT_RUN)
        )
        session.flush()
        session.add_all(
            [
                _fund(FALLING),
                _tier(FALLING),
                _risk_row(FALLING, sig_shrinking=True),
                _fund(NOT_FALLING),
                _tier(NOT_FALLING),
                _risk_row(NOT_FALLING),
                _fund(FALLING_BUT_HELD_OUT),
                _tier(FALLING_BUT_HELD_OUT, held_out=True),
                _risk_row(FALLING_BUT_HELD_OUT, sig_shrinking=True),
                _fund(NO_REPEAT, n_deposits=1, first_deposit_date=_first_deposit_days_ago(3)),
                _tier(NO_REPEAT),
                _fund(REPEATS, n_deposits=3, first_deposit_date=_first_deposit_days_ago(3)),
                _tier(REPEATS),
                _fund(
                    OLD_SINGLE_DEPOSIT,
                    n_deposits=1,
                    first_deposit_date=_first_deposit_days_ago(90),
                ),
                _tier(OLD_SINGLE_DEPOSIT),
                _fund(
                    NO_REPEAT_BUT_HELD_OUT,
                    n_deposits=1,
                    first_deposit_date=_first_deposit_days_ago(3),
                ),
                _tier(NO_REPEAT_BUT_HELD_OUT, held_out=True),
                _fund(CALL_OVERDUE),
                _tier(CALL_OVERDUE),
                _call_touchpoint(CALL_OVERDUE),
                _fund(CALL_DUE_TODAY),
                _tier(CALL_DUE_TODAY),
                _call_touchpoint(CALL_DUE_TODAY, due_date=AS_OF),
                _fund(MEETING_OVERDUE),
                _tier(MEETING_OVERDUE),
                _call_touchpoint(MEETING_OVERDUE, kind="meeting"),
                _fund(CALL_DONE),
                _tier(CALL_DONE),
                _call_touchpoint(CALL_DONE, status="done"),
                _fund(CALL_MADE_ANYWAY),
                _tier(CALL_MADE_ANYWAY),
                _call_touchpoint(CALL_MADE_ANYWAY),
                _handled(CALL_MADE_ANYWAY, "Watch"),
                _fund(CALL_OVERDUE_BUT_HELD_OUT),
                _tier(CALL_OVERDUE_BUT_HELD_OUT, held_out=True),
                _call_touchpoint(CALL_OVERDUE_BUT_HELD_OUT),
                _fund(MOVED_UP),
                _tier(
                    MOVED_UP,
                    tier="frequent_depositor",
                    segment="active_active",
                    purchased_recently=True,
                    previous_tier="gradual_depositor",
                    tier_changed_at=datetime(2026, 10, 12, 6, 0, tzinfo=UTC),
                ),
                _fund(NOT_MOVED),
                _tier(
                    NOT_MOVED,
                    tier="frequent_depositor",
                    segment="active_active",
                    purchased_recently=True,
                ),
            ]
        )
        session.add_all(
            [
                _fund(
                    DROPPED_WITH_EVERY_TRIGGER,
                    balance=3_000.0,
                    n_deposits=1,
                    first_deposit_date=_first_deposit_days_ago(3),
                ),
                _tier(DROPPED_WITH_EVERY_TRIGGER, held_out=True),
                _risk_row(DROPPED_WITH_EVERY_TRIGGER, sig_shrinking=True),
                _call_touchpoint(DROPPED_WITH_EVERY_TRIGGER),
                ClientSide(
                    client_id=DROPPED_WITH_EVERY_TRIGGER,
                    side="inactive",
                    previous_side="active",
                    balance=3_000.0,
                    threshold_kes=6_000.0,
                    source_feed="active",
                ),
            ]
        )
        for client_id in (
            RISK_ROSE,
            RISK_STEADY,
            RISK_FELL,
            ROSE_THEN_CALLED,
            CALLED_THEN_ROSE,
            ROSE_BUT_HELD_OUT,
            FIRST_RUN_ONLY,
        ):
            session.add(_fund(client_id))
            session.add(_tier(client_id, held_out=client_id == ROSE_BUT_HELD_OUT))
        session.commit()

        before_and_after = {
            RISK_ROSE: ("Watch", "High"),
            RISK_STEADY: ("Watch", "Watch"),
            RISK_FELL: ("High", "Watch"),
            ROSE_THEN_CALLED: ("Watch", "High"),
            CALLED_THEN_ROSE: ("Watch", "High"),
            ROSE_BUT_HELD_OUT: ("Watch", "High"),
            DROPPED_WITH_EVERY_TRIGGER: ("Watch", "High"),
        }
        for client_id, (before, _) in before_and_after.items():
            _snapshot(session, PREVIOUS_RUN, client_id, before)
        for client_id, (_, after) in before_and_after.items():
            _snapshot(session, CURRENT_RUN, client_id, after)
        _snapshot(session, CURRENT_RUN, FIRST_RUN_ONLY, "High")
        session.add_all(
            [
                _handled(ROSE_THEN_CALLED, "High"),
                _handled(CALLED_THEN_ROSE, "Watch"),
            ]
        )
        session.commit()
    yield
    with SessionLocal() as session:
        _purge(session)


def _run_everything(session) -> None:
    run = _open_run(session)
    recompute_new_client_signals(session, AS_OF, run)
    recompute_segment_moved_up_signal(session, AS_OF, run)
    recompute_advisor_call_overdue_signal(session, AS_OF, run)
    recompute_risk_band_rose_signal(session, AS_OF, CURRENT_RUN, run)
    recompute_side_changed_downward_signal(session, AS_OF, run)
    session.commit()
    recompute_deposit_size_falling(session, AS_OF, run.run_id)
    recompute_first_deposit_no_repeat(session, AS_OF, run.run_id)
    recompute_fa_call_overdue(session, AS_OF, run.run_id)
    recompute_risk_rose_no_call(session, AS_OF, run.run_id)
    recompute_moved_to_active_active(session, AS_OF, run.run_id)
    recompute_crossed_below_line(session, AS_OF, run.run_id)


def _active_codes(session, client_id: int) -> set[str]:
    return set(
        session.scalars(
            select(ClientSituationState.situation_code).where(
                ClientSituationState.client_id == client_id,
                ClientSituationState.is_active.is_(True),
            )
        )
    )


@pytest.mark.parametrize(
    ("client_id", "expected"),
    [
        (FALLING, {DEPOSIT_SIZE_FALLING}),
        (NOT_FALLING, set()),
        (FALLING_BUT_HELD_OUT, set()),
        (NO_REPEAT, {FIRST_DEPOSIT_NO_REPEAT}),
        (REPEATS, set()),
        (OLD_SINGLE_DEPOSIT, set()),
        (NO_REPEAT_BUT_HELD_OUT, set()),
        (CALL_OVERDUE, {FA_CALL_OVERDUE}),
        (CALL_DUE_TODAY, set()),
        (MEETING_OVERDUE, set()),
        (CALL_DONE, set()),
        (CALL_MADE_ANYWAY, set()),
        (CALL_OVERDUE_BUT_HELD_OUT, set()),
        (RISK_ROSE, {RISK_ROSE_NO_CALL}),
        (RISK_STEADY, set()),
        (RISK_FELL, set()),
        (ROSE_THEN_CALLED, set()),
        (CALLED_THEN_ROSE, {RISK_ROSE_NO_CALL}),
        (ROSE_BUT_HELD_OUT, set()),
        (FIRST_RUN_ONLY, set()),
        (MOVED_UP, {MOVED_TO_ACTIVE_ACTIVE}),
        (NOT_MOVED, set()),
        (DROPPED_WITH_EVERY_TRIGGER, {CROSSED_BELOW_LINE}),
    ],
)
def test_each_situation_fires_for_its_client_and_stays_off_for_the_near_miss(
    book, client_id, expected
) -> None:
    with SessionLocal() as session:
        _run_everything(session)
        assert _active_codes(session, client_id) == expected


def test_the_five_situations_are_registered() -> None:
    assert {
        DEPOSIT_SIZE_FALLING,
        FIRST_DEPOSIT_NO_REPEAT,
        FA_CALL_OVERDUE,
        RISK_ROSE_NO_CALL,
        MOVED_TO_ACTIVE_ACTIVE,
    } <= set(SITUATION_CODES)
