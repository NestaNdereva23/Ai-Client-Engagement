from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import delete

from app.agents import watchlist
from app.agents.signals import recompute_call_follow_up_overdue_signal
from app.agents.situations import recompute_follow_up_overdue
from app.agents.watchlist import WatchGroup, WatchlistThresholds, waiting_on_a_call
from app.db.models.active_clients import ActiveClientFund, ActiveClientInteraction
from app.db.models.digest import DigestLine, DigestRun
from app.db.models.risk import RiskRun
from app.db.models.signals import ClientSignalSnapshot, ClientSignalState, ClientSituationState
from app.db.session import SessionLocal

FUND_ID = 9557
UNCALLED_CLIENT = 955701
CALLED_BACK_CLIENT = 955702
NOT_ON_LIST_CLIENT = 955703

CLIENT_IDS = (UNCALLED_CLIENT, CALLED_BACK_CLIENT, NOT_ON_LIST_CLIENT)

RISK_RUN_ID = "waiting-on-a-call-parity-run"
REVIEWER = "waiting-on-a-call-parity-test"
AS_OF = date(2026, 9, 15)
AWAITING_CALL_DAYS = 2

THRESHOLDS = WatchlistThresholds(
    new_client_days=30,
    months_until_empty=6.0,
    small_balance=100.0,
    awaiting_call_days=AWAITING_CALL_DAYS,
)

SOURCES = ("legacy", "situations")


def _fund(client_id: int, **overrides) -> ActiveClientFund:
    row = dict(
        client_id=client_id,
        unit_fund_id=FUND_ID,
        balance=300_000.0,
        n_deposits=5,
        n_withdrawals=0,
    )
    row.update(overrides)
    return ActiveClientFund(**row)


def _digest_line(digest_run_id: int, client_id: int, **overrides) -> DigestLine:
    row = dict(
        digest_run_id=digest_run_id,
        group_key="waiting-on-a-call-parity-group",
        group_total=1,
        rank=1,
        client_id=client_id,
        unit_fund_id=FUND_ID,
        risk_score=80,
        risk_band="High",
        risk_reasons="waiting on a call",
        fund_at_risk=100_000.0,
        route="fa_call_priority",
        in_call_queue=True,
    )
    row.update(overrides)
    return DigestLine(**row)


def _purge(session, client_ids: tuple[int, ...]) -> None:
    session.execute(
        delete(ActiveClientInteraction).where(ActiveClientInteraction.client_id.in_(client_ids))
    )
    session.execute(delete(DigestLine).where(DigestLine.client_id.in_(client_ids)))
    session.execute(delete(DigestRun).where(DigestRun.risk_run_id == RISK_RUN_ID))
    session.execute(delete(RiskRun).where(RiskRun.run_id == RISK_RUN_ID))
    session.execute(
        delete(ClientSituationState).where(ClientSituationState.client_id.in_(client_ids))
    )
    session.execute(
        delete(ClientSignalSnapshot).where(ClientSignalSnapshot.client_id.in_(client_ids))
    )
    session.execute(delete(ClientSignalState).where(ClientSignalState.client_id.in_(client_ids)))
    session.execute(delete(ActiveClientFund).where(ActiveClientFund.client_id.in_(client_ids)))
    session.commit()


@pytest.fixture
def book(db: None):
    with SessionLocal() as session:
        _purge(session, CLIENT_IDS)
        session.add_all(
            [
                _fund(UNCALLED_CLIENT),
                _fund(CALLED_BACK_CLIENT),
                _fund(NOT_ON_LIST_CLIENT),
            ]
        )
        session.add(RiskRun(run_id=RISK_RUN_ID, state="completed", config_version=1))
        session.flush()
        old_digest = DigestRun(risk_run_id=RISK_RUN_ID, generated_at=datetime(2026, 9, 1, 6, 0))
        session.add(old_digest)
        session.flush()
        session.add_all(
            [
                _digest_line(old_digest.digest_run_id, UNCALLED_CLIENT),
                _digest_line(old_digest.digest_run_id, CALLED_BACK_CLIENT, rank=2),
            ]
        )
        session.add(
            ActiveClientInteraction(
                client_id=CALLED_BACK_CLIENT,
                unit_fund_id=FUND_ID,
                type="call_logged",
                reviewer_id=REVIEWER,
                created_at=datetime(2026, 9, 2, 9, 0),
            )
        )
        session.commit()

    yield

    with SessionLocal() as session:
        _purge(session, CLIENT_IDS)


def _keys(group: WatchGroup) -> set[tuple[int, int]]:
    return {(member.client_id, member.unit_fund_id) for member in group.members}


def _money_total(group: WatchGroup) -> float:
    return sum(member.balance for member in group.members)


def _recompute(session) -> None:
    run = recompute_call_follow_up_overdue_signal(session, AS_OF, AWAITING_CALL_DAYS)
    recompute_follow_up_overdue(session, AS_OF, run.run_id)


def _group(monkeypatch, source: str) -> WatchGroup:
    monkeypatch.setattr(
        watchlist, "get_settings", lambda: SimpleNamespace(signal_situation_source=source)
    )
    with SessionLocal() as session:
        return waiting_on_a_call(session, THRESHOLDS, AS_OF)


def test_the_two_sources_agree_on_client_funds_counts_and_money_total(
    book: None, monkeypatch
) -> None:
    with SessionLocal() as session:
        _recompute(session)

    legacy = _group(monkeypatch, "legacy")
    situation = _group(monkeypatch, "situations")

    assert _keys(legacy) == _keys(situation)
    assert len(legacy.members) == len(situation.members)
    assert _money_total(legacy) == _money_total(situation)
    assert _keys(situation) == {(UNCALLED_CLIENT, FUND_ID)}


@pytest.mark.parametrize("source", SOURCES)
def test_a_client_nobody_rang_is_included(book: None, monkeypatch, source: str) -> None:
    with SessionLocal() as session:
        _recompute(session)

    group = _group(monkeypatch, source)

    assert (UNCALLED_CLIENT, FUND_ID) in _keys(group)


@pytest.mark.parametrize("source", SOURCES)
def test_a_client_called_back_since_the_digest_is_excluded(
    book: None, monkeypatch, source: str
) -> None:
    with SessionLocal() as session:
        _recompute(session)

    group = _group(monkeypatch, source)

    assert (CALLED_BACK_CLIENT, FUND_ID) not in _keys(group)


@pytest.mark.parametrize("source", SOURCES)
def test_a_client_never_on_the_call_list_is_excluded(book: None, monkeypatch, source: str) -> None:
    with SessionLocal() as session:
        _recompute(session)

    group = _group(monkeypatch, source)

    assert (NOT_ON_LIST_CLIENT, FUND_ID) not in _keys(group)
