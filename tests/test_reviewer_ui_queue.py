"""The reviewer console's queue and message pages (app.api.routers.reviewer_ui).

Drives it through TestClient the same way test_api_review.py drives the
JSON API. Covers what's specific to the console: tier ordering, deciding
through a form producing the same review_action the JSON API would, the
angle brief showing up, and a cohort message hiding the outcomes it isn't
allowed to take.
"""

from __future__ import annotations

from datetime import date
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.auth.passwords import hash_password
from app.campaigns.cohorts import CohortSlot
from app.config import Settings
from app.db.models.auth import ReviewerUser
from app.db.models.llmops import GenerationRun
from app.db.models.models import Clients, Funds, PiiVault
from app.db.models.outreach import Campaign, OutreachMessage, ReviewAction, ReviewCohort
from app.db.models.rules import MessageAngleCatalog, TierContract
from app.db.session import SessionLocal
from app.llmops.versions import persist_generation_run
from app.main import app
from app.services.review import create_outreach_message

client = TestClient(app)

LOGIN = "/reviewer/login"
QUEUE = "/reviewer/queue"

# A historical window nothing else runs generation against, so inserting a
# catalogue/tier-contract version here can never become "the active one"
# for any other test's own, present-day lookup.
_ISOLATED_DATE = date(2020, 1, 15)


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


def accepted_state(
    client_id: int, *, priority_tier: str | None, angle: str = "winback_habit"
) -> dict:
    return {
        "run_id": str(uuid4()),
        "trace_id": uuid4().hex,
        "client_id": client_id,
        "product": "money market",
        "angle": angle,
        "priority_tier": priority_tier,
        "data_date": _ISOLATED_DATE,
        "prompt_variant": "habit_premium",
        "status": "accepted",
        "attempts": 1,
        "failed_guardrail": None,
        "reason": None,
        "raw_structured_output": {
            "subject": "Come back to {{fund_name}}",
            "body": "Dear {{first_name}}, we miss you.",
        },
    }


@pytest.fixture
def reviewer_session(db: None):
    """A logged-in TestClient for a reviewer-role account."""
    with SessionLocal() as session:
        user = ReviewerUser(
            username="test-queue-reviewer",
            password_hash=hash_password("queue-pass"),
            display_name="Queue Reviewer",
            role="reviewer",
        )
        session.add(user)
        session.commit()
        user_id = user.user_id

    with TestClient(app) as session_client:
        session_client.post(
            LOGIN, data={"username": "test-queue-reviewer", "password": "queue-pass"}
        )
        yield session_client

    with SessionLocal() as session:
        session.execute(delete(ReviewerUser).where(ReviewerUser.user_id == user_id))
        session.commit()


def _make_client_and_fund(session, *, fund_id: int, client_id: int) -> None:
    session.add(Funds(unit_fund_id=fund_id, unit_fund_name="Cytonn Money Market Fund"))
    session.commit()
    session.add(
        Clients(
            client_id=client_id, unit_fund_id=fund_id, n_purchases_returned=0, n_sales_returned=0
        )
    )
    session.add(PiiVault(client_id=client_id, client_name="Jane Doe"))
    session.commit()


@pytest.fixture
def tiered_messages(db: None):
    """A T4 message created first, then a T1 message -- reverse of created_at
    order, so a test can tell tier ordering apart from creation order.
    """
    fund_id = 99190
    client_id = 99191
    with SessionLocal() as session:
        _make_client_and_fund(session, fund_id=fund_id, client_id=client_id)
        campaign = Campaign(name="reviewer ui tier test campaign")
        session.add(campaign)
        session.commit()
        campaign_id = campaign.campaign_id

        run_t4 = persist_generation_run(
            session, accepted_state(client_id, priority_tier="T4"), make_settings()
        )
        message_t4 = create_outreach_message(session, run_t4, campaign_id=campaign_id)
        session.commit()

        run_t1 = persist_generation_run(
            session, accepted_state(client_id, priority_tier="T1"), make_settings()
        )
        message_t1 = create_outreach_message(session, run_t1, campaign_id=campaign_id)
        session.commit()

        message_ids = [message_t4.message_id, message_t1.message_id]
        run_ids = [run_t4.run_id, run_t1.run_id]
        t1_message_id = message_t1.message_id

    yield t1_message_id, message_ids

    with SessionLocal() as session:
        session.execute(delete(ReviewAction).where(ReviewAction.message_id.in_(message_ids)))
        session.execute(delete(OutreachMessage).where(OutreachMessage.message_id.in_(message_ids)))
        session.execute(delete(GenerationRun).where(GenerationRun.run_id.in_(run_ids)))
        session.execute(delete(Campaign).where(Campaign.campaign_id == campaign_id))
        session.execute(delete(PiiVault).where(PiiVault.client_id == client_id))
        session.execute(delete(Clients).where(Clients.client_id == client_id))
        session.execute(delete(Funds).where(Funds.unit_fund_id == fund_id))
        session.commit()


@pytest.fixture
def pending_message(db: None):
    """One plain pending_review message, for the decide-through-the-UI test."""
    fund_id = 99192
    client_id = 99193
    with SessionLocal() as session:
        _make_client_and_fund(session, fund_id=fund_id, client_id=client_id)
        campaign = Campaign(name="reviewer ui decide test campaign")
        session.add(campaign)
        session.commit()
        campaign_id = campaign.campaign_id

        run = persist_generation_run(
            session, accepted_state(client_id, priority_tier="T2"), make_settings()
        )
        message = create_outreach_message(session, run, campaign_id=campaign_id)
        session.commit()
        message_id, run_id = message.message_id, run.run_id

    yield message_id

    with SessionLocal() as session:
        session.execute(delete(ReviewAction).where(ReviewAction.message_id == message_id))
        session.execute(delete(OutreachMessage).where(OutreachMessage.message_id == message_id))
        session.execute(delete(GenerationRun).where(GenerationRun.run_id == run_id))
        session.execute(delete(Campaign).where(Campaign.campaign_id == campaign_id))
        session.execute(delete(PiiVault).where(PiiVault.client_id == client_id))
        session.execute(delete(Clients).where(Clients.client_id == client_id))
        session.execute(delete(Funds).where(Funds.unit_fund_id == fund_id))
        session.commit()


@pytest.fixture
def message_with_angle_brief(db: None):
    """A message generated under an angle/tier catalogued only in the
    isolated historical window, so the detail page has a real brief and
    contract to show.
    """
    fund_id = 99194
    client_id = 99195
    angle = "reviewer_ui_test_angle"
    tier = "T3"
    with SessionLocal() as session:
        _make_client_and_fund(session, fund_id=fund_id, client_id=client_id)
        session.add(
            MessageAngleCatalog(
                version=999001,
                angle=angle,
                headline="Reviewer UI test headline",
                who="Clients used only by this test",
                claim="Nothing outside this test's fixtures",
                ask="Nothing",
                never="Anything real",
                valid_from=_ISOLATED_DATE,
                valid_to=date(2020, 2, 1),
            )
        )
        session.add(
            TierContract(
                version=999001,
                tier=tier,
                display_name="Test tier",
                primary_channel="email",
                max_words=120,
                sign_off="reviewer",
                human_approval=True,
                review_sample_rate=1.0,
                valid_from=_ISOLATED_DATE,
                valid_to=date(2020, 2, 1),
            )
        )
        session.commit()

        campaign = Campaign(name="reviewer ui angle brief test campaign")
        session.add(campaign)
        session.commit()
        campaign_id = campaign.campaign_id

        run = persist_generation_run(
            session,
            accepted_state(client_id, priority_tier=tier, angle=angle),
            make_settings(),
        )
        message = create_outreach_message(session, run, campaign_id=campaign_id)
        session.commit()
        message_id, run_id = message.message_id, run.run_id

    yield message_id

    with SessionLocal() as session:
        session.execute(delete(ReviewAction).where(ReviewAction.message_id == message_id))
        session.execute(delete(OutreachMessage).where(OutreachMessage.message_id == message_id))
        session.execute(delete(GenerationRun).where(GenerationRun.run_id == run_id))
        session.execute(delete(Campaign).where(Campaign.campaign_id == campaign_id))
        session.execute(delete(PiiVault).where(PiiVault.client_id == client_id))
        session.execute(delete(Clients).where(Clients.client_id == client_id))
        session.execute(delete(Funds).where(Funds.unit_fund_id == fund_id))
        session.execute(delete(MessageAngleCatalog).where(MessageAngleCatalog.version == 999001))
        session.execute(delete(TierContract).where(TierContract.version == 999001))
        session.commit()


@pytest.fixture
def cohort_sample_message(db: None):
    """A message riding on a review sampling cohort, so the detail page
    must not offer reject/escalate/hold.
    """
    fund_id = 99196
    client_id = 99197
    with SessionLocal() as session:
        _make_client_and_fund(session, fund_id=fund_id, client_id=client_id)
        campaign = Campaign(name="reviewer ui cohort test campaign")
        session.add(campaign)
        session.commit()
        campaign_id = campaign.campaign_id

        cohort = ReviewCohort(
            cohort_id=uuid4().hex,
            campaign_id=campaign_id,
            priority_tier="T3",
            sample_rate=1.0,
            sample_cap=25,
        )
        session.add(cohort)
        session.commit()

        run = persist_generation_run(
            session, accepted_state(client_id, priority_tier="T3"), make_settings()
        )
        message = create_outreach_message(
            session,
            run,
            campaign_id=campaign_id,
            cohort_slot=CohortSlot(cohort_id=cohort.cohort_id, is_sample=True),
        )
        session.commit()
        message_id, run_id, cohort_id = message.message_id, run.run_id, cohort.cohort_id

    yield message_id

    with SessionLocal() as session:
        session.execute(delete(ReviewAction).where(ReviewAction.message_id == message_id))
        session.execute(delete(OutreachMessage).where(OutreachMessage.message_id == message_id))
        session.execute(delete(GenerationRun).where(GenerationRun.run_id == run_id))
        session.execute(delete(ReviewCohort).where(ReviewCohort.cohort_id == cohort_id))
        session.execute(delete(Campaign).where(Campaign.campaign_id == campaign_id))
        session.execute(delete(PiiVault).where(PiiVault.client_id == client_id))
        session.execute(delete(Clients).where(Clients.client_id == client_id))
        session.execute(delete(Funds).where(Funds.unit_fund_id == fund_id))
        session.commit()


def test_queue_lists_t1_ahead_of_an_earlier_created_t4(reviewer_session, tiered_messages) -> None:
    t1_message_id, _all_ids = tiered_messages
    response = reviewer_session.get(QUEUE)
    assert response.status_code == 200
    # The T1 message was created second but must render before the T4 one.
    body = response.text
    assert body.index(t1_message_id) < body.index(_all_ids[0])


def test_decide_through_the_ui_records_the_same_shape_as_the_json_api(
    reviewer_session, pending_message
) -> None:
    message_id = pending_message
    response = reviewer_session.post(
        f"/reviewer/messages/{message_id}/decide",
        data={"outcome": "approve", "reason": "looks good"},
        follow_redirects=False,
    )
    assert response.status_code == 303

    with SessionLocal() as session:
        message = session.get(OutreachMessage, message_id)
        assert message.status == "approved"
        actions = session.query(ReviewAction).filter(ReviewAction.message_id == message_id).all()
        assert len(actions) == 1
        assert actions[0].reviewer_id == "test-queue-reviewer"
        assert actions[0].outcome == "approve"
        assert actions[0].reason == "looks good"
        assert actions[0].priority_tier == "T2"


def test_message_detail_shows_the_angle_brief(reviewer_session, message_with_angle_brief) -> None:
    message_id = message_with_angle_brief
    response = reviewer_session.get(f"/reviewer/messages/{message_id}")
    assert response.status_code == 200
    assert "Reviewer UI test headline" in response.text
    assert "Clients used only by this test" in response.text
    # No judge evaluation was recorded for this fixture's run.
    assert "No judge evaluation recorded yet" in response.text
    assert "Attempts: 1" in response.text


def test_cohort_sample_message_hides_reject_and_hold(
    reviewer_session, cohort_sample_message
) -> None:
    message_id = cohort_sample_message
    response = reviewer_session.get(f"/reviewer/messages/{message_id}")
    assert response.status_code == 200
    body = response.text
    assert 'value="approve"' in body
    assert 'value="edit_approve"' in body
    assert 'value="reject"' not in body
    assert 'value="escalate"' not in body
    assert 'value="hold"' not in body
