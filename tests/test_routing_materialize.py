from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import delete

from app.db.models.active_clients import ActiveClientFund, ActiveTransaction
from app.db.models.models import ClientFeatures, ClientFund, Clients, Funds
from app.db.models.routing import ClientSide
from app.db.session import SessionLocal
from app.routing.materialize import materialize_inactive_from_active

_FUND = 924901
_IDS = list(range(924100, 924140))
_REF = datetime(2026, 10, 6, 12, 0, 0)


def _purge(session) -> None:
    session.execute(delete(ClientSide).where(ClientSide.client_id.in_(_IDS)))
    session.execute(delete(ClientFeatures).where(ClientFeatures.client_id.in_(_IDS)))
    session.execute(delete(ClientFund).where(ClientFund.client_id.in_(_IDS)))
    session.execute(delete(Clients).where(Clients.client_id.in_(_IDS)))
    session.execute(delete(ActiveTransaction).where(ActiveTransaction.client_id.in_(_IDS)))
    session.execute(delete(ActiveClientFund).where(ActiveClientFund.client_id.in_(_IDS)))
    session.execute(delete(Funds).where(Funds.unit_fund_id == _FUND))
    session.commit()


def _fund(session, client_id: int, balance: float, n_deposits: int, n_withdrawals: int) -> None:
    session.add(
        ActiveClientFund(
            client_id=client_id,
            unit_fund_id=_FUND,
            balance=balance,
            n_deposits=n_deposits,
            n_withdrawals=n_withdrawals,
            last_deposit_date=date(2026, 6, 1),
        )
    )


def _txn(session, txn_id: int, client_id: int, txn_type: str, amount: float, when: date) -> None:
    session.add(
        ActiveTransaction(
            txn_id=txn_id,
            txn_type=txn_type,
            client_id=client_id,
            unit_fund_id=_FUND,
            fund_short_name="High Yield Fund",
            txn_date=when,
            amount=amount,
        )
    )


def test_a_client_above_the_line_is_not_materialized(db) -> None:
    with SessionLocal() as session:
        _purge(session)
        _fund(session, 924100, balance=9000.0, n_deposits=1, n_withdrawals=0)
        _txn(session, 9241001, 924100, "purchase", 9000.0, date(2026, 6, 1))
        session.commit()

        materialize_inactive_from_active(session, reference=_REF)

        assert session.get(Clients, 924100) is None
        assert session.get(ClientFeatures, 924100) is None
        _purge(session)


def test_a_small_depositor_lands_as_low_depositors(db) -> None:
    with SessionLocal() as session:
        _purge(session)
        _fund(session, 924101, balance=800.0, n_deposits=1, n_withdrawals=0)
        _txn(session, 9241011, 924101, "purchase", 800.0, date(2026, 6, 1))
        session.commit()

        materialize_inactive_from_active(session, reference=_REF)

        feature = session.get(ClientFeatures, 924101)
        assert feature is not None
        assert feature.priority_tier == "low_depositors"
        assert session.get(Clients, 924101) is not None
        assert session.get(ClientFund, (924101, _FUND)) is not None
        _purge(session)


def test_a_client_with_no_deposits_lands_as_hot_leads(db) -> None:
    with SessionLocal() as session:
        _purge(session)
        _fund(session, 924102, balance=0.0, n_deposits=0, n_withdrawals=0)
        session.commit()

        materialize_inactive_from_active(session, reference=_REF)

        feature = session.get(ClientFeatures, 924102)
        assert feature is not None
        assert feature.priority_tier == "hot_leads"
        _purge(session)


def test_a_former_big_depositor_now_drawn_down_is_a_withdrawer(db) -> None:
    with SessionLocal() as session:
        _purge(session)
        _fund(session, 924103, balance=2300.0, n_deposits=3, n_withdrawals=3)
        _txn(session, 9241031, 924103, "purchase", 40000.0, date(2024, 1, 1))
        _txn(session, 9241032, 924103, "purchase", 40000.0, date(2024, 3, 1))
        _txn(session, 9241033, 924103, "purchase", 40000.0, date(2024, 6, 1))
        _txn(session, 9241034, 924103, "sale", 30000.0, date(2024, 7, 1))
        _txn(session, 9241035, 924103, "sale", 40000.0, date(2025, 2, 1))
        _txn(session, 9241036, 924103, "sale", 20000.0, date(2025, 3, 1))
        _txn(session, 9241037, 924103, "sale", 50.0, date(2025, 4, 1))
        session.commit()

        materialize_inactive_from_active(session, reference=_REF)

        feature = session.get(ClientFeatures, 924103)
        assert feature is not None
        assert feature.priority_tier == "gradual_withdrawers"
        assert feature.high_value is True
        _purge(session)


def test_re_running_updates_the_same_rows(db) -> None:
    with SessionLocal() as session:
        _purge(session)
        _fund(session, 924104, balance=500.0, n_deposits=1, n_withdrawals=0)
        _txn(session, 9241041, 924104, "purchase", 500.0, date(2026, 6, 1))
        session.commit()

        materialize_inactive_from_active(session, reference=_REF)
        materialize_inactive_from_active(session, reference=_REF)

        rows = session.query(Clients).filter(Clients.client_id == 924104).all()
        assert len(rows) == 1
        _purge(session)
