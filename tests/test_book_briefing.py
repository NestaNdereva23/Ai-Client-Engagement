from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import delete

from app.db.models.active_clients import ActiveTransaction
from app.db.models.fa_assignment import FaAssignment
from app.db.session import SessionLocal
from app.services.book_briefing import month_end_patterns

ADVISOR = "book-brief-1"
FUND = 947
CLIENTS = [95811, 95812, 95813]
TXN_ID_BASE = 95_811_000
TODAY = date(2026, 10, 24)


@pytest.fixture
def month_end_book(db):
    with SessionLocal() as session:
        _wipe(session)
        for client_id in CLIENTS:
            session.add(FaAssignment(client_id=client_id, unit_fund_id=FUND, fa_id=ADVISOR))
        next_id = TXN_ID_BASE
        deposits = {
            CLIENTS[0]: [date(2026, month, 28) for month in (5, 6, 7, 8)] + [date(2026, 9, 2)],
            CLIENTS[1]: [date(2026, month, 29) for month in (6, 7, 8)] + [date(2026, 10, 3)],
            CLIENTS[2]: [date(2026, month, 5) for month in (5, 6, 7, 8, 9)],
        }
        for client_id, dates in deposits.items():
            for when in dates:
                session.add(
                    ActiveTransaction(
                        txn_id=next_id,
                        txn_type="purchase",
                        client_id=client_id,
                        unit_fund_id=FUND,
                        txn_date=when,
                        amount=1000.0,
                    )
                )
                next_id += 1
        session.commit()
    yield
    with SessionLocal() as session:
        _wipe(session)
        session.commit()


def _wipe(session) -> None:
    session.execute(delete(ActiveTransaction).where(ActiveTransaction.client_id.in_(CLIENTS)))
    session.execute(delete(FaAssignment).where(FaAssignment.client_id.in_(CLIENTS)))


def test_month_end_counts_usual_late_toppers_and_who_has_not_yet(month_end_book) -> None:
    with SessionLocal() as session:
        patterns = month_end_patterns(session, [ADVISOR], TODAY)

    assert patterns[ADVISOR].usual_clients == 2
    assert patterns[ADVISOR].not_yet_clients == 1


def test_month_end_stays_quiet_early_in_the_month(month_end_book) -> None:
    with SessionLocal() as session:
        assert month_end_patterns(session, [ADVISOR], date(2026, 10, 12)) == {}
