"""Bucket derivation: grouping a due, eligible cohort by shared profile."""

from __future__ import annotations

import pytest
from sqlalchemy import delete, select

from app.agents.graph import ClientContext
from app.campaigns.bucketing import ProfileKey, derive_buckets, profile_key_for
from app.campaigns.enrollment import enroll_cohort
from app.db.models.campaigns import CampaignStep, Enrollment, TouchLog
from app.db.models.models import ClientFeatures, Clients, Funds
from app.db.models.outreach import Campaign
from app.db.session import SessionLocal
from app.services.campaigns import add_campaign_step

FUND_ID = 970
CLIENT_IDS = (97001, 97002, 97003, 97004)


def make_context_loader(facts_by_client: dict[int, dict]):
    """Same angle, tier, and chunks for every client -- only facts differ,
    the shape a real cohort sharing one angle and product takes.
    """

    def load(client_id: int, product: str) -> ClientContext:
        return ClientContext(
            raw_context={"client_id": client_id},
            angle="pick_up_again",
            prompt_variant="pick_up_again",
            priority_tier="T3",
            chunks=(),
            facts=facts_by_client.get(client_id, {}),
        )

    return load


@pytest.fixture
def clients(db: None):
    with SessionLocal() as session:
        session.add(Funds(unit_fund_id=FUND_ID, unit_fund_name="Test Fund"))
        session.commit()
        for client_id in CLIENT_IDS:
            session.add(
                Clients(
                    client_id=client_id,
                    unit_fund_id=FUND_ID,
                    n_purchases_returned=0,
                    n_sales_returned=0,
                )
            )
        session.commit()
        for client_id in CLIENT_IDS:
            session.add(ClientFeatures(client_id=client_id, fund_type="money_market"))
        session.commit()

    yield list(CLIENT_IDS)

    with SessionLocal() as session:
        session.execute(delete(ClientFeatures).where(ClientFeatures.client_id.in_(CLIENT_IDS)))
        session.execute(delete(Clients).where(Clients.client_id.in_(CLIENT_IDS)))
        session.execute(delete(Funds).where(Funds.unit_fund_id == FUND_ID))
        session.commit()


@pytest.fixture
def campaign(clients: list[int]):
    with SessionLocal() as session:
        row = Campaign(name="bucketing test campaign")
        session.add(row)
        session.commit()
        campaign_id = row.campaign_id
        add_campaign_step(session, campaign_id, offset_days=0, message_angle="pick_up_again")
        session.commit()
        enroll_cohort(session, campaign_id=campaign_id, client_ids=clients)
        session.commit()

    yield campaign_id

    with SessionLocal() as session:
        enrollment_ids = session.scalars(
            select(Enrollment.enrollment_id).where(Enrollment.campaign_id == campaign_id)
        ).all()
        if enrollment_ids:
            session.execute(delete(TouchLog).where(TouchLog.enrollment_id.in_(enrollment_ids)))
        session.execute(delete(Enrollment).where(Enrollment.campaign_id == campaign_id))
        session.execute(delete(CampaignStep).where(CampaignStep.campaign_id == campaign_id))
        session.execute(delete(Campaign).where(Campaign.campaign_id == campaign_id))
        session.commit()


def test_clients_sharing_a_profile_land_in_one_bucket_despite_different_figures(
    campaign: int, clients: list[int]
) -> None:
    """Same angle, tier, and product; contribution size differs, but that's
    placeholder-filled, not profile-defining."""
    a, b = clients[0], clients[1]
    facts_by_client = {
        a: {"invested_every_n_days": 30, "typical_contribution_kes": 5000},
        b: {"invested_every_n_days": 30, "typical_contribution_kes": 50000},
    }
    context_loader = make_context_loader(facts_by_client)

    with SessionLocal() as session:
        buckets = derive_buckets(session, campaign, limit=10, context_loader=context_loader)

    matching = [bucket for bucket in buckets if bucket.size >= 1 and _has_client(bucket, a)]
    assert len(matching) == 1
    bucket = matching[0]
    assert _has_client(bucket, b)
    assert bucket.size == 2


def test_a_conditional_prohibition_fact_splits_the_bucket(
    campaign: int, clients: list[int]
) -> None:
    """Same angle, tier, and product, but has_cadence differs, so they may
    not share a template."""
    a, b = clients[2], clients[3]
    facts_by_client = {
        a: {"invested_every_n_days": 30},
        b: {},  # no invested_every_n_days at all: has_cadence is False
    }
    context_loader = make_context_loader(facts_by_client)

    with SessionLocal() as session:
        buckets = derive_buckets(session, campaign, limit=10, context_loader=context_loader)

    bucket_a = next(bucket for bucket in buckets if _has_client(bucket, a))
    bucket_b = next(bucket for bucket in buckets if _has_client(bucket, b))
    assert bucket_a is not bucket_b
    assert bucket_a.profile_key.has_cadence is True
    assert bucket_b.profile_key.has_cadence is False


def test_profile_key_for_reads_exactly_the_conditional_prohibition_facts() -> None:
    context = ClientContext(
        raw_context={},
        angle="not_a_goodbye",
        prompt_variant="not_a_goodbye",
        priority_tier="T4",
        chunks=(),
        facts={
            "invested_every_n_days": None,
            "stale_contact": True,
            "exit_reason": "charge_settled",
            "fund_name": "Cytonn Money Market Fund",
        },
    )
    key = profile_key_for(context, product="money market")
    assert key == ProfileKey(
        message_angle="not_a_goodbye",
        priority_tier="T4",
        product="money market",
        has_cadence=False,
        stale_contact=True,
        exit_reason_charge_settled=True,
        fund_name_known=True,
    )


def test_profile_key_as_dict_matches_message_template_profile_key_shape() -> None:
    key = ProfileKey(
        message_angle="pick_up_again",
        priority_tier="T3",
        product="money market",
        has_cadence=True,
        stale_contact=False,
        exit_reason_charge_settled=False,
        fund_name_known=False,
    )
    assert key.as_dict() == {
        "message_angle": "pick_up_again",
        "priority_tier": "T3",
        "product": "money market",
        "has_cadence": True,
        "stale_contact": False,
        "exit_reason_charge_settled": False,
        "fund_name_known": False,
    }


def _has_client(bucket, client_id: int) -> bool:
    return any(member.enrollment.client_id == client_id for member in bucket.members)
