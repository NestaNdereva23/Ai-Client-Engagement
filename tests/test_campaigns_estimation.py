"""Template estimation: the reference (derive_buckets) and fast (bulk SQL)
estimators, and the one property that matters most -- they must agree.
"""

from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, text

import app.campaigns.eligibility as eligibility
import app.campaigns.estimation as estimation
from app.campaigns.enrollment import enroll_cohort
from app.campaigns.estimation import estimate_templates_reference, estimate_templates_sql
from app.config import Settings
from app.db.models.campaigns import CampaignStep, Enrollment
from app.db.models.models import ClientFeatures, ClientFund, Clients, Funds, PiiVault
from app.db.models.outreach import Campaign
from app.db.models.rules import ClientMessageIndicators
from app.db.models.suppression import Suppression
from app.db.session import SessionLocal
from app.main import app
from app.services.campaigns import add_campaign_step

CAMPAIGNS = "/api/v1/campaigns"

client = TestClient(app)


@pytest.fixture(autouse=True)
def _authed(configured_reviewers, reviewer_1_headers):
    client.headers.update(reviewer_1_headers)
    yield
    client.headers.pop("Authorization", None)


FUND_ID = 9799
BASELINE, STALE, NO_FUND_ROW, EXIT_REASON, CADENCE_NONE = 979901, 979902, 979903, 979904, 979905
NO_HISTORY, OPTED_OUT, SUPPRESSED, NO_CONTACT = 979906, 979907, 979908, 979909
DIFFERENT_TIER, DIFFERENT_ANGLE = 979910, 979911
ALL_CLIENT_IDS = (
    BASELINE,
    STALE,
    NO_FUND_ROW,
    EXIT_REASON,
    CADENCE_NONE,
    NO_HISTORY,
    OPTED_OUT,
    SUPPRESSED,
    NO_CONTACT,
    DIFFERENT_TIER,
    DIFFERENT_ANGLE,
)
ELIGIBLE_CLIENT_IDS = (
    BASELINE,
    STALE,
    NO_FUND_ROW,
    EXIT_REASON,
    CADENCE_NONE,
    DIFFERENT_TIER,
    DIFFERENT_ANGLE,
)


@pytest.fixture
def roles(db: None):
    with SessionLocal() as session:
        exists = session.scalar(text("SELECT 1 FROM pg_roles WHERE rolname = 'ace_restricted'"))
    if not exists:
        pytest.skip("boundary roles not present; run alembic upgrade head")


def _seed_client(
    session,
    client_id: int,
    *,
    angle: str,
    tier: str,
    fund_type: str,
    cadence_band: str,
    stale_contact: bool = False,
    exit_reason: str = "unknown",
    purchase_depth: str = "single",
    with_fund_row: bool = True,
    rhythm_days: int | None = 30,
    opted_out: bool = False,
    contact_email: str | None = "present@example.com",
    suppressed: bool = False,
) -> None:
    session.add(
        Clients(
            client_id=client_id,
            unit_fund_id=FUND_ID,
            n_purchases_returned=1,
            n_sales_returned=1,
        )
    )
    # Flushed before anything with a client_id foreign key is added: batched
    # inserts across many clients otherwise don't guarantee clients lands
    # before its dependents in the same flush.
    session.flush()
    session.add(
        ClientFeatures(
            client_id=client_id,
            fund_type=fund_type,
            cadence_band=cadence_band,
            stale_contact=stale_contact,
            exit_reason=exit_reason,
            purchase_depth=purchase_depth,
        )
    )
    session.add(
        PiiVault(
            client_id=client_id,
            client_name=f"Estimation Test {client_id}",
            contact_email=contact_email,
            opt_out_flag=opted_out,
        )
    )
    session.add(
        ClientMessageIndicators(
            client_id=client_id,
            message_angle=angle,
            urgency="low",
            priority_tier=tier,
            prompt_variant=angle,
            rule_name="estimation_test",
            rule_version=1,
        )
    )
    if with_fund_row:
        session.add(
            ClientFund(
                client_id=client_id,
                unit_fund_id=FUND_ID,
                is_primary_contact_row=True,
                n_purchases=3,
                n_sales=1,
                rhythm_days=rhythm_days,
                avg_ticket=5000,
                max_ticket=20000,
                hold_days=45,
                days_cold=200,
                exit_date=date(2025, 3, 15),
            )
        )
    if suppressed:
        session.add(Suppression(client_id=client_id, reason="estimation_test"))


def _purge_cohort_rows(session) -> None:
    """Delete anything left over under these ids, so a prior run that
    aborted mid-fixture (leaving Funds committed but Clients rolled back,
    say) can never block this one from setting up cleanly.
    """
    session.execute(delete(Suppression).where(Suppression.client_id.in_(ALL_CLIENT_IDS)))
    session.execute(delete(ClientFund).where(ClientFund.client_id.in_(ALL_CLIENT_IDS)))
    session.execute(
        delete(ClientMessageIndicators).where(ClientMessageIndicators.client_id.in_(ALL_CLIENT_IDS))
    )
    session.execute(delete(PiiVault).where(PiiVault.client_id.in_(ALL_CLIENT_IDS)))
    session.execute(delete(ClientFeatures).where(ClientFeatures.client_id.in_(ALL_CLIENT_IDS)))
    session.execute(delete(Clients).where(Clients.client_id.in_(ALL_CLIENT_IDS)))
    session.execute(delete(Funds).where(Funds.unit_fund_id == FUND_ID))
    session.commit()


@pytest.fixture
def cohort(roles, monkeypatch):
    """Eleven clients exercising every ProfileKey field and every exclusion
    path check_eligibility applies: purchase_depth='none', opt-out,
    suppression, and no deliverable contact.

    require_deliverable_contact is pinned True regardless of local .env --
    REQUIRE_DELIVERABLE_CONTACT=false is a dev-only escape hatch (see
    test_campaigns_eligibility.py), and NO_CONTACT exists specifically to
    exercise the gate this fixture claims to exercise.
    """
    monkeypatch.setattr(
        eligibility, "get_settings", lambda: Settings(require_deliverable_contact=True)
    )
    monkeypatch.setattr(
        estimation, "get_settings", lambda: Settings(require_deliverable_contact=True)
    )
    with SessionLocal() as session:
        _purge_cohort_rows(session)
        session.add(Funds(unit_fund_id=FUND_ID, unit_fund_name="Estimation Test Fund"))
        session.commit()

        _seed_client(
            session,
            BASELINE,
            angle="pick_up_again",
            tier="T3",
            fund_type="money_market",
            cadence_band="Regular",
        )
        _seed_client(
            session,
            STALE,
            angle="pick_up_again",
            tier="T3",
            fund_type="money_market",
            cadence_band="Regular",
            stale_contact=True,
        )
        _seed_client(
            session,
            NO_FUND_ROW,
            angle="pick_up_again",
            tier="T3",
            fund_type="money_market",
            cadence_band="Regular",
            with_fund_row=False,
        )
        _seed_client(
            session,
            EXIT_REASON,
            angle="pick_up_again",
            tier="T3",
            fund_type="money_market",
            cadence_band="Regular",
            exit_reason="charge_settled",
        )
        _seed_client(
            session,
            CADENCE_NONE,
            angle="pick_up_again",
            tier="T3",
            fund_type="high_yield",
            cadence_band="None",
            rhythm_days=30,
        )
        _seed_client(
            session,
            NO_HISTORY,
            angle="pick_up_again",
            tier="T3",
            fund_type="money_market",
            cadence_band="Regular",
            purchase_depth="none",
        )
        _seed_client(
            session,
            OPTED_OUT,
            angle="pick_up_again",
            tier="T3",
            fund_type="money_market",
            cadence_band="Regular",
            opted_out=True,
        )
        _seed_client(
            session,
            SUPPRESSED,
            angle="pick_up_again",
            tier="T3",
            fund_type="money_market",
            cadence_band="Regular",
            suppressed=True,
        )
        _seed_client(
            session,
            NO_CONTACT,
            angle="pick_up_again",
            tier="T3",
            fund_type="money_market",
            cadence_band="Regular",
            contact_email=None,
        )
        _seed_client(
            session,
            DIFFERENT_TIER,
            angle="pick_up_again",
            tier="T1",
            fund_type="money_market",
            cadence_band="Regular",
        )
        _seed_client(
            session,
            DIFFERENT_ANGLE,
            angle="not_a_goodbye",
            tier="T3",
            fund_type="money_market",
            cadence_band="Regular",
        )
        session.commit()

    with SessionLocal() as session:
        row = Campaign(name="estimation test campaign")
        session.add(row)
        session.commit()
        campaign_id = row.campaign_id
        add_campaign_step(session, campaign_id, offset_days=0, message_angle="pick_up_again")
        session.commit()
        enroll_cohort(session, campaign_id=campaign_id, client_ids=list(ALL_CLIENT_IDS))
        session.commit()

    yield campaign_id

    with SessionLocal() as session:
        session.execute(delete(Enrollment).where(Enrollment.campaign_id == campaign_id))
        session.execute(delete(CampaignStep).where(CampaignStep.campaign_id == campaign_id))
        session.execute(delete(Campaign).where(Campaign.campaign_id == campaign_id))
        session.commit()
        _purge_cohort_rows(session)


def test_reference_and_sql_estimators_agree_on_the_full_cohort(cohort: int) -> None:
    with SessionLocal() as session:
        reference = estimate_templates_reference(session, cohort, limit=100)
    with SessionLocal() as session:
        sql = estimate_templates_sql(session, cohort, limit=100)

    assert reference.eligible_clients == len(ELIGIBLE_CLIENT_IDS)
    assert sql.eligible_clients == reference.eligible_clients
    assert sql.estimated_templates == reference.estimated_templates
    assert sql.buckets == reference.buckets


def test_sql_estimator_splits_no_fund_row_and_cadence_none_correctly(cohort: int) -> None:
    """The two nuances the implementation plan calls out by name: a missing
    client_fund row zeroes every fact-derived field, and cadence_band
    'None' overrides a real rhythm_days.
    """
    with SessionLocal() as session:
        sql = estimate_templates_sql(session, cohort, limit=100)

    assert len(sql.buckets) == 7  # one per distinct profile among the eligible seven
    no_fund_row_key = next(
        b.profile_key
        for b in sql.buckets
        if b.profile_key.fund_name_known is False
        and b.profile_key.has_cadence is False
        and b.profile_key.product == "money market"
    )
    assert no_fund_row_key.stale_contact is False
    assert no_fund_row_key.exit_reason_charge_settled is False

    cadence_none_key = next(
        b.profile_key for b in sql.buckets if b.profile_key.product == "high yield"
    )
    assert cadence_none_key.has_cadence is False
    assert cadence_none_key.fund_name_known is True


def test_same_configuration_gives_the_same_number_twice(cohort: int) -> None:
    with SessionLocal() as session:
        first = estimate_templates_sql(session, cohort, limit=100)
    with SessionLocal() as session:
        second = estimate_templates_sql(session, cohort, limit=100)
    assert first.estimated_templates == second.estimated_templates
    assert first.buckets == second.buckets


def test_an_empty_cohort_estimates_zero_rather_than_erroring(db: None) -> None:
    with SessionLocal() as session:
        row = Campaign(name="empty estimation test campaign")
        session.add(row)
        session.commit()
        campaign_id = row.campaign_id

    try:
        with SessionLocal() as session:
            sql = estimate_templates_sql(session, campaign_id, limit=100)
        assert sql.estimated_templates == 0
        assert sql.eligible_clients == 0
        assert sql.buckets == ()
    finally:
        with SessionLocal() as session:
            session.execute(delete(Campaign).where(Campaign.campaign_id == campaign_id))
            session.commit()


def test_estimate_endpoint_never_constructs_an_llm_client(monkeypatch, db: None) -> None:
    """The estimation path must never build an LLMClient. Patching the
    campaigns router's own reference to raise proves nothing on the estimate
    path calls it, the same way /templates/draft's llm_client=get_llm_client(...)
    would if it were reached.
    """

    def _must_not_be_called(*args, **kwargs):
        raise AssertionError("the estimate endpoint must never construct an LLMClient")

    monkeypatch.setattr("app.api.routers.campaigns.get_llm_client", _must_not_be_called)

    with SessionLocal() as session:
        row = Campaign(name="no-llm estimation test campaign")
        session.add(row)
        session.commit()
        campaign_id = row.campaign_id

    try:
        response = client.get(f"{CAMPAIGNS}/{campaign_id}/templates/estimate")
        assert response.status_code == 200
        body = response.json()
        assert body["estimated_templates"] == 0
        assert body["eligible_clients"] == 0
    finally:
        with SessionLocal() as session:
            session.execute(delete(Campaign).where(Campaign.campaign_id == campaign_id))
            session.commit()


def test_estimate_endpoint_404s_for_an_unknown_campaign(db: None) -> None:
    response = client.get(f"{CAMPAIGNS}/999999999/templates/estimate")
    assert response.status_code == 404
