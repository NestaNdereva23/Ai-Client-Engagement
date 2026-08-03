"""The model-facing projection is an allow-listed view over client_features.

These run against a database with the view migration applied; without it they
skip. They pin the exact exposed columns, prove the excluded ones stay hidden,
and check the safe role reads the view but not the feature table.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError

from app.db.models.models import ClientFeatures, Clients, Funds
from app.db.models.views import llm_client_context
from app.db.session import SessionLocal

SAFE = "ace_safe"
ALLOWLIST = {
    "client_id",
    "archetype",
    "recency_bucket",
    "value_tier_label",
    "rhythm_band",
    "recency_band",
    "value_band",
    "cadence_band",
    "hold_band",
    "purchase_depth",
    "trend_band",
    "exit_reason",
    "fund_type",
    "in_wave",
    "has_depth",
    "staged_exit",
    "stale_contact",
}
# Feature columns that must never surface through the projection.
FORBIDDEN = {
    "own_rhythm_days",
    "observed_volume",
    "purchases_censored",
    "history_censored",
    "updated_at",
    "value_tier",
    "client_name",
    "client_code",
    "n_funds",
    "holds_other_funds",
    "priority_tier",
}
EAT = timezone(timedelta(hours=3))


def _view_columns(session) -> set[str]:
    rows = session.execute(
        text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'llm_client_context'"
        )
    ).all()
    return {name for (name,) in rows}


@pytest.fixture
def view_present(db: None):
    """Skip unless the projection view exists, i.e. the migration has been applied."""
    with SessionLocal() as session:
        exists = session.scalar(
            text("SELECT 1 FROM pg_class WHERE relname = 'llm_client_context' AND relkind = 'v'")
        )
        if not exists:
            pytest.skip("llm_client_context view not present; run alembic upgrade head")


def test_it_is_a_view_not_a_table(view_present) -> None:
    with SessionLocal() as session:
        relkind = session.scalar(
            text("SELECT relkind FROM pg_class WHERE relname = 'llm_client_context'")
        )
    assert relkind == "v"


def test_exposes_exactly_the_allowlisted_columns(view_present) -> None:
    with SessionLocal() as session:
        columns = _view_columns(session)
    assert columns == ALLOWLIST
    # The code-side mapping stays in step with the view.
    assert {c.name for c in llm_client_context.columns} == ALLOWLIST


def test_excludes_raw_and_pii_feature_columns(view_present) -> None:
    with SessionLocal() as session:
        columns = _view_columns(session)
    assert columns.isdisjoint(FORBIDDEN)


def test_safe_role_reads_the_view(view_present) -> None:
    with SessionLocal() as session:
        session.execute(text(f'SET ROLE "{SAFE}"'))
        count = session.scalar(text("SELECT count(*) FROM llm_client_context"))
        session.execute(text("RESET ROLE"))
    assert count is not None


def test_safe_role_cannot_read_the_underlying_feature_table(view_present) -> None:
    with SessionLocal() as session:
        session.execute(text(f'SET ROLE "{SAFE}"'))
        with pytest.raises(ProgrammingError, match="permission denied"):
            session.execute(text("SELECT count(*) FROM client_features"))
        session.rollback()
        session.execute(text("RESET ROLE"))


def test_view_is_keyed_by_client_id_and_relabels_the_tier(view_present) -> None:
    fund_id, client_id = 990, 99001
    with SessionLocal() as session:
        # Each parent is committed before its child so the foreign keys hold.
        session.add(Funds(unit_fund_id=fund_id, unit_fund_name="Money Market Fund"))
        session.commit()
        session.add(
            Clients(
                client_id=client_id,
                unit_fund_id=fund_id,
                n_purchases_returned=1,
                n_sales_returned=0,
            )
        )
        session.commit()
        session.add(
            ClientFeatures(
                client_id=client_id,
                archetype="One-and-done",
                recency_bucket="Exited 3y plus",
                value_tier="High",
                rhythm_band="Unknown",
                observed_volume=1,
                recency_band="Over 6y",
                value_band="High",
                cadence_band="None",
                hold_band="Stayed years",
                purchase_depth="single",
                trend_band="unknown",
                exit_reason="client_sale",
                fund_type="money_market",
                in_wave=False,
                has_depth=False,
                staged_exit=False,
                stale_contact=True,
                updated_at=datetime(2026, 7, 20, 8, 0, tzinfo=EAT),
            )
        )
        session.commit()

    try:
        with SessionLocal() as session:
            row = session.execute(
                text(
                    "SELECT client_id, archetype, value_tier_label, rhythm_band, "
                    "recency_band, value_band, fund_type, stale_contact "
                    "FROM llm_client_context WHERE client_id = :c"
                ),
                {"c": client_id},
            ).one()
        assert row.client_id == client_id
        assert row.archetype == "One-and-done"
        # value_tier is surfaced under its label name, unchanged in value.
        assert row.value_tier_label == "High"
        assert row.rhythm_band == "Unknown"
        assert row.recency_band == "Over 6y"
        assert row.value_band == "High"
        assert row.fund_type == "money_market"
        assert row.stale_contact is True
    finally:
        with SessionLocal() as session:
            session.execute(
                ClientFeatures.__table__.delete().where(ClientFeatures.client_id == client_id)
            )
            session.execute(Clients.__table__.delete().where(Clients.client_id == client_id))
            session.execute(Funds.__table__.delete().where(Funds.unit_fund_id == fund_id))
            session.commit()
