"""The numeric-facts view: real figures, separately grantable from the bands.

Kept apart from llm_client_context so the higher-sensitivity grant can be
revoked on its own. Nothing here is rounded -- that stays ModelFactBlock's
job -- but an exact calendar date must never be SELECT-able at all, which is
checked directly rather than assumed from the SQL.
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError

from app.db.models.models import ClientFund, Clients, Funds
from app.db.models.views import llm_client_numeric_facts
from app.db.session import SessionLocal

SAFE = "ace_safe"
ALLOWLIST = {
    "client_id",
    "years_since_exit",
    "typical_contribution_kes",
    "largest_contribution_kes",
    "invested_every_n_days",
    "days_held_after_last_topup",
    "month_they_left",
}


def _view_columns(session) -> set[str]:
    rows = session.execute(
        text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'llm_client_numeric_facts'"
        )
    ).all()
    return {name for (name,) in rows}


@pytest.fixture
def view_present(db: None):
    with SessionLocal() as session:
        exists = session.scalar(
            text(
                "SELECT 1 FROM pg_class "
                "WHERE relname = 'llm_client_numeric_facts' AND relkind = 'v'"
            )
        )
        if not exists:
            pytest.skip("llm_client_numeric_facts view not present; run alembic upgrade head")


def test_it_is_a_view_not_a_table(view_present) -> None:
    with SessionLocal() as session:
        relkind = session.scalar(
            text("SELECT relkind FROM pg_class WHERE relname = 'llm_client_numeric_facts'")
        )
    assert relkind == "v"


def test_exposes_exactly_the_allowlisted_columns(view_present) -> None:
    with SessionLocal() as session:
        columns = _view_columns(session)
    assert columns == ALLOWLIST
    assert {c.name for c in llm_client_numeric_facts.columns} == ALLOWLIST


def test_safe_role_reads_the_view(view_present) -> None:
    with SessionLocal() as session:
        session.execute(text(f'SET ROLE "{SAFE}"'))
        count = session.scalar(text("SELECT count(*) FROM llm_client_numeric_facts"))
        session.execute(text("RESET ROLE"))
    assert count is not None


def test_safe_role_cannot_read_the_underlying_relationship_table(view_present) -> None:
    with SessionLocal() as session:
        session.execute(text(f'SET ROLE "{SAFE}"'))
        with pytest.raises(ProgrammingError, match="permission denied"):
            session.execute(text("SELECT count(*) FROM client_fund"))
        session.rollback()
        session.execute(text("RESET ROLE"))


@pytest.fixture
def seeded_relationship(db: None):
    """One client with a primary and a non-primary relationship.

    The non-primary row carries an obviously wrong figure, so a test reading
    the wrong row would be caught immediately rather than passing by luck.
    """
    fund_id, other_fund_id, client_id = 991, 992, 99002
    with SessionLocal() as session:
        session.add_all(
            [
                Funds(unit_fund_id=fund_id, unit_fund_name="Money Market Fund"),
                Funds(unit_fund_id=other_fund_id, unit_fund_name="High Yield Fund"),
            ]
        )
        session.commit()
        session.add(
            Clients(
                client_id=client_id,
                unit_fund_id=fund_id,
                n_purchases_returned=3,
                n_sales_returned=1,
            )
        )
        session.commit()
        session.add_all(
            [
                ClientFund(
                    client_id=client_id,
                    unit_fund_id=fund_id,
                    n_purchases=3,
                    n_sales=1,
                    exit_date=date(2024, 7, 15),
                    days_cold=730,
                    avg_ticket=123_456.0,
                    max_ticket=200_000.0,
                    rhythm_days=30.0,
                    hold_days=90,
                    is_primary_contact_row=True,
                ),
                ClientFund(
                    client_id=client_id,
                    unit_fund_id=other_fund_id,
                    n_purchases=1,
                    n_sales=0,
                    exit_date=date(2020, 1, 1),
                    days_cold=2000,
                    avg_ticket=999_999.0,
                    max_ticket=999_999.0,
                    rhythm_days=0.5,
                    hold_days=1,
                    is_primary_contact_row=False,
                ),
            ]
        )
        session.commit()

    try:
        yield client_id
    finally:
        with SessionLocal() as session:
            session.execute(ClientFund.__table__.delete().where(ClientFund.client_id == client_id))
            session.execute(Clients.__table__.delete().where(Clients.client_id == client_id))
            session.execute(
                Funds.__table__.delete().where(Funds.unit_fund_id.in_([fund_id, other_fund_id]))
            )
            session.commit()


def _row(client_id: int):
    with SessionLocal() as session:
        return session.execute(
            text("SELECT * FROM llm_client_numeric_facts WHERE client_id = :c"),
            {"c": client_id},
        ).one()


def test_only_the_primary_relationship_surfaces(seeded_relationship) -> None:
    row = _row(seeded_relationship)
    assert row.typical_contribution_kes == 123_456.0
    assert row.largest_contribution_kes == 200_000.0
    assert row.days_held_after_last_topup == 90


def test_month_they_left_is_year_and_month_only(seeded_relationship) -> None:
    """The view coarsens the date itself; day precision is never selectable
    through this view at all, not just discarded by convention afterwards.
    """
    row = _row(seeded_relationship)
    assert row.month_they_left == "2024-07"
    assert "15" not in row.month_they_left


def test_no_column_on_the_view_is_date_typed(view_present) -> None:
    """Structural: no column here could return day precision even if selected."""
    with SessionLocal() as session:
        types = (
            session.execute(
                text(
                    "SELECT data_type FROM information_schema.columns "
                    "WHERE table_name = 'llm_client_numeric_facts'"
                )
            )
            .scalars()
            .all()
        )
    assert not any("date" in t.lower() for t in types)


def test_years_since_exit_is_computed_from_days_cold(seeded_relationship) -> None:
    row = _row(seeded_relationship)
    # Postgres returns integer/float division as numeric, not a Python float.
    assert float(row.years_since_exit) == pytest.approx(730 / 365.25)


def test_a_same_day_rhythm_is_not_a_cadence(db: None) -> None:
    """rhythm_days under one day is same-day top-ups, not a real cadence."""
    fund_id, client_id = 993, 99003
    with SessionLocal() as session:
        session.add(Funds(unit_fund_id=fund_id, unit_fund_name="Money Market Fund"))
        session.commit()
        session.add(
            Clients(
                client_id=client_id,
                unit_fund_id=fund_id,
                n_purchases_returned=2,
                n_sales_returned=0,
            )
        )
        session.commit()
        session.add(
            ClientFund(
                client_id=client_id,
                unit_fund_id=fund_id,
                n_purchases=2,
                n_sales=0,
                rhythm_days=0.0,
                is_primary_contact_row=True,
            )
        )
        session.commit()

    try:
        row = _row(client_id)
        assert row.invested_every_n_days is None
    finally:
        with SessionLocal() as session:
            session.execute(ClientFund.__table__.delete().where(ClientFund.client_id == client_id))
            session.execute(Clients.__table__.delete().where(Clients.client_id == client_id))
            session.execute(Funds.__table__.delete().where(Funds.unit_fund_id == fund_id))
            session.commit()


def test_a_real_rhythm_survives(seeded_relationship) -> None:
    row = _row(seeded_relationship)
    assert row.invested_every_n_days == 30
