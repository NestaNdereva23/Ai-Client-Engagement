"""Enrollment logic: cohort enrollment, idempotency, and per-person dedup.

Covers enroll_cohort creating one enrollment row per client_id, a second
call with an overlapping cohort creating no duplicates, and clients that
share a vault name (the same person on two funds) being deduped so only
one of them is marked as the primary contact row.
"""

from __future__ import annotations

import pytest
from sqlalchemy import delete, select

from app.campaigns.enrollment import enroll_cohort
from app.db.models.audit import AuditLog
from app.db.models.campaigns import Enrollment
from app.db.models.models import Clients, Funds, PiiVault
from app.db.models.outreach import Campaign
from app.db.session import SessionLocal


@pytest.fixture
def campaign(db: None):
    with SessionLocal() as session:
        row = Campaign(name="test enrollment campaign")
        session.add(row)
        session.commit()
        campaign_id = row.campaign_id

    yield campaign_id

    with SessionLocal() as session:
        session.execute(delete(Enrollment).where(Enrollment.campaign_id == campaign_id))
        session.execute(
            delete(AuditLog).where(
                AuditLog.entity_type == "enrollment", AuditLog.entity_id == str(campaign_id)
            )
        )
        session.execute(delete(Campaign).where(Campaign.campaign_id == campaign_id))
        session.commit()


@pytest.fixture
def two_funds(db: None):
    with SessionLocal() as session:
        session.add(Funds(unit_fund_id=980, unit_fund_name="Fund A"))
        session.add(Funds(unit_fund_id=981, unit_fund_name="Fund B"))
        session.commit()

    yield (980, 981)

    with SessionLocal() as session:
        session.execute(delete(Funds).where(Funds.unit_fund_id.in_((980, 981))))
        session.commit()


def _add_client(session, client_id: int, fund_id: int, name: str | None) -> None:
    session.add(
        Clients(
            client_id=client_id,
            unit_fund_id=fund_id,
            n_purchases_returned=0,
            n_sales_returned=0,
        )
    )
    session.add(PiiVault(client_id=client_id, client_name=name))


@pytest.fixture
def same_person_two_funds(two_funds):
    fund_a, fund_b = two_funds
    client_a, client_b = 98001, 98002
    with SessionLocal() as session:
        _add_client(session, client_a, fund_a, "Jane Doe")
        _add_client(session, client_b, fund_b, "Jane Doe")
        session.commit()

    yield client_a, client_b

    with SessionLocal() as session:
        session.execute(delete(Enrollment).where(Enrollment.client_id.in_((client_a, client_b))))
        session.execute(delete(PiiVault).where(PiiVault.client_id.in_((client_a, client_b))))
        session.execute(delete(Clients).where(Clients.client_id.in_((client_a, client_b))))
        session.commit()


@pytest.fixture
def two_unrelated_clients(two_funds):
    fund_a, fund_b = two_funds
    client_a, client_b = 98003, 98004
    with SessionLocal() as session:
        _add_client(session, client_a, fund_a, "Alice Smith")
        _add_client(session, client_b, fund_b, "Bob Jones")
        session.commit()

    yield client_a, client_b

    with SessionLocal() as session:
        session.execute(delete(Enrollment).where(Enrollment.client_id.in_((client_a, client_b))))
        session.execute(delete(PiiVault).where(PiiVault.client_id.in_((client_a, client_b))))
        session.execute(delete(Clients).where(Clients.client_id.in_((client_a, client_b))))
        session.commit()


def test_enroll_cohort_creates_one_row_per_client(
    campaign: int, two_unrelated_clients: tuple[int, int]
) -> None:
    client_a, client_b = two_unrelated_clients
    with SessionLocal() as session:
        created = enroll_cohort(session, campaign_id=campaign, client_ids=[client_a, client_b])
        session.commit()

    assert {row.client_id for row in created} == {client_a, client_b}
    assert all(row.is_primary_contact_row for row in created)


def test_enroll_cohort_is_idempotent_on_a_repeated_run(
    campaign: int, two_unrelated_clients: tuple[int, int]
) -> None:
    client_a, client_b = two_unrelated_clients
    with SessionLocal() as session:
        enroll_cohort(session, campaign_id=campaign, client_ids=[client_a, client_b])
        session.commit()

    with SessionLocal() as session:
        enroll_cohort(session, campaign_id=campaign, client_ids=[client_a, client_b])
        session.commit()

    with SessionLocal() as session:
        rows = (
            session.execute(select(Enrollment).where(Enrollment.campaign_id == campaign))
            .scalars()
            .all()
        )
    assert len(rows) == 2


def test_same_person_on_two_funds_gets_exactly_one_primary_row(
    campaign: int, same_person_two_funds: tuple[int, int]
) -> None:
    client_a, client_b = same_person_two_funds
    with SessionLocal() as session:
        created = enroll_cohort(session, campaign_id=campaign, client_ids=[client_a, client_b])
        session.commit()

    primary_ids = {row.client_id for row in created if row.is_primary_contact_row}
    assert primary_ids == {client_a}, "the lower client_id should win the primary row"
    assert len(created) == 2


def test_a_later_enrolled_sibling_does_not_become_primary(
    campaign: int, same_person_two_funds: tuple[int, int]
) -> None:
    client_a, client_b = same_person_two_funds
    with SessionLocal() as session:
        enroll_cohort(session, campaign_id=campaign, client_ids=[client_a])
        session.commit()

    with SessionLocal() as session:
        created = enroll_cohort(session, campaign_id=campaign, client_ids=[client_b])
        session.commit()

    assert created[0].client_id == client_b
    assert created[0].is_primary_contact_row is False


def test_enroll_cohort_writes_an_audit_row(
    campaign: int, two_unrelated_clients: tuple[int, int]
) -> None:
    client_a, client_b = two_unrelated_clients
    with SessionLocal() as session:
        enroll_cohort(session, campaign_id=campaign, client_ids=[client_a, client_b])
        session.commit()

    with SessionLocal() as session:
        rows = (
            session.execute(
                select(AuditLog).where(
                    AuditLog.entity_type == "enrollment",
                    AuditLog.entity_id == str(campaign),
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1
    assert set(rows[0].detail["enrolled_client_ids"]) == {client_a, client_b}


def test_enroll_cohort_with_no_clients_is_a_no_op(campaign: int, db: None) -> None:
    with SessionLocal() as session:
        created = enroll_cohort(session, campaign_id=campaign, client_ids=[])
    assert created == []
