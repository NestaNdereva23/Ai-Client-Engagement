"""Tests for the client_risk_features table itself: the shape a compose_score
result is stored in, and that the deferred columns are genuinely optional.
"""

from __future__ import annotations

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from app.db.models.risk import ClientRiskFeatures
from app.db.session import SessionLocal


@pytest.fixture
def cleanup_rows():
    keys: list[tuple[int, int]] = []
    yield keys
    with SessionLocal() as session:
        for client_id, unit_fund_id in keys:
            session.execute(
                delete(ClientRiskFeatures).where(
                    ClientRiskFeatures.client_id == client_id,
                    ClientRiskFeatures.unit_fund_id == unit_fund_id,
                )
            )
        session.commit()


def _row(**overrides) -> ClientRiskFeatures:
    base = dict(
        client_id=90101,
        unit_fund_id=10,
        sig_heavy_withdrawal=False,
        sig_dormant=True,
        sig_broken_pattern=False,
        sig_shrinking=False,
        sig_going_dormant=False,
        sig_never_repeated=True,
        risk_score=28,
        risk_band="Watch",
        risk_reasons="No deposit in 12 months; Never made a second deposit",
        fund_at_risk=140_000.0,
        config_version=1,
    )
    base.update(overrides)
    return ClientRiskFeatures(**base)


def test_a_compose_score_result_round_trips(db, cleanup_rows) -> None:
    cleanup_rows.append((90101, 10))
    with SessionLocal() as session:
        session.add(_row())
        session.commit()

    with SessionLocal() as session:
        row = session.scalar(
            select(ClientRiskFeatures).where(
                ClientRiskFeatures.client_id == 90101, ClientRiskFeatures.unit_fund_id == 10
            )
        )
    assert row is not None
    assert row.risk_score == 28
    assert row.risk_band == "Watch"
    assert row.fund_at_risk == 140_000.0
    # Deferred columns are genuinely optional -- not backfilled with a guess.
    assert row.recency_band is None
    assert row.balance_tier is None
    assert row.value_tier is None
    assert row.route is None
    assert row.queue_rank is None
    # pattern_is_reliable defaults false when not given.
    assert row.pattern_is_reliable is False


def test_risk_score_is_required(db) -> None:
    with SessionLocal() as session, pytest.raises(IntegrityError):
        session.add(_row(client_id=90102, risk_score=None))
        session.commit()
