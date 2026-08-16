"""Tests for GET /campaigns/analytics and GET /campaigns/analytics/trend:
the book-wide, cross-campaign outreach analytics counterpart to
GET /risk/analytics and GET /risk/analytics/trend.

The fixture seeds every row before the test body runs, so unlike
test_api_risk_analytics.py's delta-against-a-baseline approach, these
tests read bucket keys that are unique to the fixture (e.g.
analytics_test_value_band) and assert on them directly, which stays
correct no matter what other data already lives in the shared test
database.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.config import Settings
from app.db.models.campaigns import ContactEvent, Enrollment, TouchLog
from app.db.models.llmops import GenerationRun
from app.db.models.models import ClientFeatures, Clients, Funds, PiiVault
from app.db.models.outreach import Campaign, OutreachMessage, ReviewAction
from app.db.models.rules import ClientMessageIndicators
from app.db.session import SessionLocal
from app.llmops.versions import persist_generation_run
from app.main import app

client = TestClient(app)

CAMPAIGNS = "/api/v1/campaigns"
FUND_ID = 948
CLIENT_ID = 94800


def make_settings(**overrides) -> Settings:
    defaults = {
        "llm_provider": "anthropic",
        "anthropic_api_key": "test-key",
        "llm_model": "claude-opus-5",
        "llm_temperature": None,
        "llm_max_tokens": 1024,
    }
    defaults.update(overrides)
    return Settings(**defaults)


def accepted_state(client_id: int) -> dict:
    return {
        "run_id": str(uuid4()),
        "trace_id": uuid4().hex,
        "client_id": client_id,
        "product": "money market",
        "angle": "pick_up_again",
        "prompt_variant": "pick_up_again",
        "status": "accepted",
        "attempts": 1,
        "failed_guardrail": None,
        "reason": None,
        "raw_structured_output": {
            "subject": "Come back to {{fund_name}}",
            "body": "Dear {{first_name}}, we miss you.",
        },
    }


def _analytics() -> dict:
    response = client.get(f"{CAMPAIGNS}/analytics")
    assert response.status_code == 200
    return response.json()


def _count(body: dict, section: str, key: str | None) -> int:
    return next((row["count"] for row in body[section] if row["key"] == key), 0)


@pytest.fixture
def campaign_with_full_funnel(db: None):
    """One campaign: a primary enrollment that reengaged, a suppressed
    duplicate row, an approved message, a review action, a reply event, and
    a sent touch -- one row in every table the analytics reads.
    """
    fund_id = FUND_ID
    primary_id, suppressed_id = CLIENT_ID + 1, CLIENT_ID + 2
    with SessionLocal() as session:
        # Self-healing: purge anything left over from a prior aborted run of
        # this same fixture, so an earlier crash can never permanently wedge
        # the next run (see test-fixture-id-collisions).
        session.execute(delete(ContactEvent).where(ContactEvent.client_id == primary_id))
        session.execute(
            delete(TouchLog).where(
                TouchLog.enrollment_id.in_(
                    select(Enrollment.enrollment_id).where(
                        Enrollment.client_id.in_((primary_id, suppressed_id))
                    )
                )
            )
        )
        session.execute(
            delete(ReviewAction).where(
                ReviewAction.message_id.in_(
                    select(OutreachMessage.message_id).where(
                        OutreachMessage.client_id.in_((primary_id, suppressed_id))
                    )
                )
            )
        )
        session.execute(
            delete(OutreachMessage).where(
                OutreachMessage.client_id.in_((primary_id, suppressed_id))
            )
        )
        session.execute(
            delete(Enrollment).where(Enrollment.client_id.in_((primary_id, suppressed_id)))
        )
        session.execute(
            delete(ClientMessageIndicators).where(
                ClientMessageIndicators.client_id.in_((primary_id, suppressed_id))
            )
        )
        session.execute(
            delete(ClientFeatures).where(ClientFeatures.client_id.in_((primary_id, suppressed_id)))
        )
        session.execute(delete(PiiVault).where(PiiVault.client_id.in_((primary_id, suppressed_id))))
        session.execute(delete(Clients).where(Clients.client_id.in_((primary_id, suppressed_id))))
        session.execute(delete(Campaign).where(Campaign.name == "test analytics campaign"))
        session.execute(delete(Funds).where(Funds.unit_fund_id == fund_id))
        session.commit()

        campaign = Campaign(name="test analytics campaign")
        session.add(campaign)
        session.add(Funds(unit_fund_id=fund_id, unit_fund_name="Test Fund"))
        session.commit()
        campaign_id = campaign.campaign_id

        for cid in (primary_id, suppressed_id):
            session.add(
                Clients(
                    client_id=cid,
                    unit_fund_id=fund_id,
                    n_purchases_returned=0,
                    n_sales_returned=0,
                )
            )
        session.commit()

        session.add_all(
            [
                PiiVault(client_id=primary_id, client_name="Same Person"),
                PiiVault(client_id=suppressed_id, client_name="Same Person"),
            ]
        )
        session.add(
            ClientFeatures(
                client_id=primary_id,
                value_band="analytics_test_value_band",
                recency_band="analytics_test_recency_band",
            )
        )
        session.add(
            ClientMessageIndicators(
                client_id=primary_id,
                message_angle="analytics_test_angle",
                urgency="normal",
                priority_tier="analytics_test_tier",
                prompt_variant="pick_up_again",
                rule_name="test rule",
                rule_version=1,
            )
        )
        session.commit()

        session.add_all(
            [
                Enrollment(
                    campaign_id=campaign_id,
                    client_id=primary_id,
                    is_primary_contact_row=True,
                    status="stopped_reengaged",
                ),
                Enrollment(
                    campaign_id=campaign_id,
                    client_id=suppressed_id,
                    is_primary_contact_row=False,
                    status="excluded",
                ),
            ]
        )
        session.commit()
        primary_enrollment_id = session.scalar(
            select(Enrollment.enrollment_id).where(
                Enrollment.campaign_id == campaign_id,
                Enrollment.client_id == primary_id,
            )
        )

        message_run = persist_generation_run(session, accepted_state(primary_id), make_settings())
        message_id = uuid4().hex
        session.add(
            OutreachMessage(
                message_id=message_id,
                campaign_id=campaign_id,
                generation_run_id=message_run.run_id,
                client_id=primary_id,
                ai_draft_content={"subject": "Subject", "body": "Body"},
                status="approved",
            )
        )
        session.commit()

        session.add(
            ReviewAction(
                message_id=message_id,
                reviewer_id="test-reviewer",
                outcome="approve",
            )
        )
        sent_at = datetime.now(UTC)
        session.add(
            TouchLog(
                enrollment_id=primary_enrollment_id,
                step_no=1,
                message_id=message_id,
                sent_at=sent_at,
                delivery_status="sent",
            )
        )
        session.add(ContactEvent(client_id=primary_id, type="reply", occurred_at=sent_at))
        session.commit()

    yield campaign_id

    with SessionLocal() as session:
        session.execute(delete(ContactEvent).where(ContactEvent.client_id == primary_id))
        session.execute(delete(TouchLog).where(TouchLog.message_id == message_id))
        session.execute(delete(ReviewAction).where(ReviewAction.message_id == message_id))
        session.execute(delete(OutreachMessage).where(OutreachMessage.message_id == message_id))
        session.execute(delete(GenerationRun).where(GenerationRun.run_id == message_run.run_id))
        session.execute(delete(Enrollment).where(Enrollment.campaign_id == campaign_id))
        session.execute(
            delete(ClientMessageIndicators).where(ClientMessageIndicators.client_id == primary_id)
        )
        session.execute(delete(ClientFeatures).where(ClientFeatures.client_id == primary_id))
        session.execute(delete(PiiVault).where(PiiVault.client_id.in_((primary_id, suppressed_id))))
        session.execute(delete(Clients).where(Clients.client_id.in_((primary_id, suppressed_id))))
        session.execute(delete(Campaign).where(Campaign.campaign_id == campaign_id))
        session.execute(delete(Funds).where(Funds.unit_fund_id == fund_id))
        session.commit()


def test_shape_includes_every_breakdown(db: None) -> None:
    body = _analytics()
    assert isinstance(body["total_enrolled"], int)
    assert isinstance(body["primary_count"], int)
    assert isinstance(body["suppressed_count"], int)
    assert isinstance(body["active_campaign_count"], int)
    for section in (
        "by_enrollment_status",
        "by_value_band",
        "by_recency_band",
        "by_priority_tier",
        "by_message_angle",
        "by_message_status",
        "by_review_outcome",
        "by_contact_event",
    ):
        assert isinstance(body[section], list)
    assert isinstance(body["reengaged_count"], int)
    assert isinstance(body["reengagement_rate"], float)


def test_enrollment_and_cohort_cuts_reflect_the_new_funnel(
    campaign_with_full_funnel,
) -> None:
    # campaign_with_full_funnel seeds before this test body runs, so there is
    # no pre-seed snapshot to diff against here; every bucket key below is
    # unique to this fixture, so an exact count is unambiguous instead.
    after = _analytics()

    assert after["primary_count"] >= 1
    assert after["suppressed_count"] >= 1
    assert _count(after, "by_enrollment_status", "stopped_reengaged") >= 1
    # The suppressed row must not appear in the cohort cuts, which are
    # scoped to primary rows only -- exactly one primary row carries each
    # of these test-only bucket values.
    assert _count(after, "by_value_band", "analytics_test_value_band") == 1
    assert _count(after, "by_recency_band", "analytics_test_recency_band") == 1
    assert _count(after, "by_priority_tier", "analytics_test_tier") == 1
    assert _count(after, "by_message_angle", "analytics_test_angle") == 1


def test_message_review_and_contact_event_cuts_reflect_the_new_funnel(
    campaign_with_full_funnel,
) -> None:
    after = _analytics()

    assert _count(after, "by_message_status", "approved") >= 1
    assert _count(after, "by_review_outcome", "approve") >= 1
    assert _count(after, "by_contact_event", "reply") >= 1


def test_reengagement_rate_reflects_the_new_reengaged_primary_row(
    campaign_with_full_funnel,
) -> None:
    after = _analytics()
    assert after["reengaged_count"] >= 1
    assert after["reengagement_rate"] == pytest.approx(
        after["reengaged_count"] / after["primary_count"]
    )


def test_trend_shape_covers_the_requested_number_of_days(db: None) -> None:
    response = client.get(f"{CAMPAIGNS}/analytics/trend", params={"days": 7})
    assert response.status_code == 200
    body = response.json()
    assert len(body["points"]) == 7
    days = [date.fromisoformat(p["day"]) for p in body["points"]]
    assert days == sorted(days)
    assert days[-1] == date.today()
    assert days[0] == date.today() - timedelta(days=6)
    for point in body["points"]:
        assert isinstance(point["touches_sent"], int)
        assert isinstance(point["replies"], int)
        assert isinstance(point["bounces"], int)


def test_trend_reflects_a_touch_sent_today(campaign_with_full_funnel) -> None:
    response = client.get(f"{CAMPAIGNS}/analytics/trend", params={"days": 1})
    assert response.status_code == 200
    points = response.json()["points"]
    assert len(points) == 1
    assert points[0]["touches_sent"] >= 1
    assert points[0]["replies"] >= 1
