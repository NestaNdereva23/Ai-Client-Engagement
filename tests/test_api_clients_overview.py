from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.db.models.campaigns import Enrollment
from app.db.models.models import ClientFeatures, Clients, Funds
from app.db.models.outreach import Campaign
from app.db.models.rules import ClientMessageIndicators
from app.db.session import SessionLocal
from app.main import app

client = TestClient(app)

OVERVIEW = "/api/v1/clients/overview"
CLIENTS = "/api/v1/clients"
SEGMENTS = "/api/v1/segments"
BOOK_SUMMARY = "/api/v1/clients/summary"
ENROLLMENT_SUMMARY = "/api/v1/clients/enrollment-summary"
SUPPRESSION_SUMMARY = "/api/v1/clients/suppression-summary"
ANGLES = "/api/v1/rules/angles"
CAMPAIGN_ANALYTICS = "/api/v1/campaigns/analytics"


@pytest.fixture(autouse=True)
def _authed(configured_reviewers, reviewer_1_headers):
    client.headers.update(reviewer_1_headers)
    yield
    client.headers.pop("Authorization", None)


def test_missing_token_is_401(configured_reviewers) -> None:
    assert TestClient(app).get(OVERVIEW).status_code == 401


@pytest.fixture
def enrolled_book(db: None):
    """Three clients in one fund across different bands, two of them enrolled.

    Spread over two value bands and two recency bands so the cross-tab has
    more than one cell, and one client is left without a resolved angle so
    the message-angle rollup has to carry a null bucket.
    """
    fund_id = 981
    ids = (98101, 98102, 98103)
    with SessionLocal() as session:
        session.add(Funds(unit_fund_id=fund_id, unit_fund_name="Cytonn Money Market Fund"))
        session.commit()
        session.add_all(
            [
                Clients(
                    client_id=cid,
                    unit_fund_id=fund_id,
                    n_purchases_returned=0,
                    n_sales_returned=0,
                )
                for cid in ids
            ]
        )
        session.commit()
        session.add_all(
            [
                ClientFeatures(
                    client_id=ids[0],
                    purchase_depth="single",
                    recency_band="1 to 3y",
                    value_band="High",
                    cadence_band="Unknown",
                    stale_contact=True,
                    history_censored=True,
                ),
                ClientFeatures(
                    client_id=ids[1],
                    purchase_depth="few",
                    recency_band="3 to 6y",
                    value_band="Medium",
                    cadence_band="Periodic",
                    purchases_censored=True,
                ),
                ClientFeatures(
                    client_id=ids[2],
                    purchase_depth="capped",
                    recency_band="Unknown",
                    value_band="High",
                    cadence_band="Tight",
                ),
            ]
        )
        session.add_all(
            [
                ClientMessageIndicators(
                    client_id=cid,
                    message_angle="onboarding_retry",
                    urgency="high",
                    priority_tier="T1",
                    prompt_variant="onboarding_retry",
                    rule_name="onboarding_retry",
                    rule_version=3,
                )
                for cid in ids[:2]
            ]
        )
        session.commit()

        campaign = Campaign(name="clients overview test campaign")
        session.add(campaign)
        session.commit()
        campaign_id = campaign.campaign_id
        session.add_all(
            [
                Enrollment(
                    campaign_id=campaign_id,
                    client_id=ids[0],
                    status="enrolled",
                    is_primary_contact_row=True,
                ),
                Enrollment(
                    campaign_id=campaign_id,
                    client_id=ids[1],
                    status="excluded",
                    is_primary_contact_row=False,
                ),
            ]
        )
        session.commit()

    yield ids

    with SessionLocal() as session:
        session.execute(delete(Enrollment).where(Enrollment.campaign_id == campaign_id))
        session.execute(delete(Campaign).where(Campaign.campaign_id == campaign_id))
        session.execute(
            delete(ClientMessageIndicators).where(ClientMessageIndicators.client_id.in_(ids))
        )
        session.execute(delete(ClientFeatures).where(ClientFeatures.client_id.in_(ids)))
        session.execute(delete(Clients).where(Clients.client_id.in_(ids)))
        session.execute(delete(Funds).where(Funds.unit_fund_id == fund_id))
        session.commit()


def _buckets(rows: list[dict]) -> set[tuple[str | None, int]]:
    return {(row["key"], row["count"]) for row in rows}


def _cells(rows: list[dict]) -> set[tuple[str | None, str | None, int]]:
    return {(row["value_band"], row["recency_band"], row["count"]) for row in rows}


def test_segments_match_the_segments_endpoint(enrolled_book) -> None:
    overview = client.get(OVERVIEW).json()["segments"]
    segments = client.get(SEGMENTS).json()

    for field in ("by_purchase_depth", "by_value_band", "by_cadence_band", "by_message_angle"):
        assert _buckets(overview[field]) == _buckets(segments[field]), field

    assert _cells(overview["by_value_and_recency"]) == _cells(segments["by_value_and_recency"])

    for field in (
        "stale_contact_count",
        "history_censored_count",
        "purchases_censored_count",
        "unknown_recency_count",
    ):
        assert overview[field] == segments[field], field


def test_summaries_match_their_own_endpoints(enrolled_book) -> None:
    overview = client.get(OVERVIEW).json()

    assert overview["book"] == client.get(BOOK_SUMMARY).json()
    assert overview["enrollment"] == client.get(ENROLLMENT_SUMMARY).json()
    assert overview["suppression"] == client.get(SUPPRESSION_SUMMARY).json()
    assert overview["angles"] == client.get(ANGLES).json()


def test_reengagement_matches_campaign_analytics(enrolled_book) -> None:
    overview = client.get(OVERVIEW).json()["reengagement"]
    analytics = client.get(CAMPAIGN_ANALYTICS).json()

    assert overview["primary_count"] == analytics["primary_count"]
    assert overview["reengaged_count"] == analytics["reengaged_count"]
    assert overview["reengagement_rate"] == pytest.approx(analytics["reengagement_rate"])


def test_roster_is_the_unfiltered_first_page(enrolled_book) -> None:
    overview = client.get(OVERVIEW, params={"roster_limit": 2}).json()["roster"]
    listed = client.get(CLIENTS, params={"limit": 2}).json()

    assert overview["items"] == listed["items"]
    assert overview["next_cursor"] == listed["next_cursor"]


def test_roster_cursor_pages_through_the_list_endpoint(enrolled_book) -> None:
    overview = client.get(OVERVIEW, params={"roster_limit": 2}).json()["roster"]
    assert overview["next_cursor"] is not None

    following = client.get(CLIENTS, params={"limit": 2, "cursor": overview["next_cursor"]}).json()
    seen = [row["client_id"] for row in overview["items"]]
    assert all(row["client_id"] not in seen for row in following["items"])


def test_roster_limit_is_bounded(enrolled_book) -> None:
    assert client.get(OVERVIEW, params={"roster_limit": 0}).status_code == 422
    assert client.get(OVERVIEW, params={"roster_limit": 100000}).status_code == 422
