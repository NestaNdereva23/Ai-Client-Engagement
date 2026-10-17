from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.agents.situations import (
    DEPOSIT_SIZE_FALLING,
    FREQUENT_DEPOSITOR_ONE_FUND,
    GRADUAL_DEPOSITOR_WITHDRAWING,
)
from app.db.models.active_clients import (
    ActiveClientFund,
    ActiveClientInteraction,
    ActiveClientTouchpoint,
)
from app.db.models.fa_assignment import FaAssignment
from app.db.models.risk import ClientRiskFeatures
from app.db.models.routing import ClientTier
from app.db.models.signals import ClientSituationState, SignalRun
from app.db.session import SessionLocal
from app.main import app
from app.routing.tiers import SEGMENT_BY_TIER, TIER_GRADUAL_DEPOSITOR
from app.services.client_touchpoints import today_in_nairobi
from app.services.fund_names import short_fund_name

client = TestClient(app)

ROSTER = "/api/v1/active-clients"
ADVISOR = "next-action-fa-1"
FUND = 957
RUN_ID = "next-action-test-run"

CALL_CLIENT = 95701
EMAIL_CLIENT = 95702
QUIET_CLIENT = 95703
NO_SITUATION_CLIENT = 95704
ALL_TEST_CLIENTS = [CALL_CLIENT, EMAIL_CLIENT, QUIET_CLIENT, NO_SITUATION_CLIENT]
SITUATION_BY_CLIENT = {
    CALL_CLIENT: GRADUAL_DEPOSITOR_WITHDRAWING,
    EMAIL_CLIENT: DEPOSIT_SIZE_FALLING,
    QUIET_CLIENT: FREQUENT_DEPOSITOR_ONE_FUND,
}
SCORES = {CALL_CLIENT: 80, EMAIL_CLIENT: 60, QUIET_CLIENT: 40, NO_SITUATION_CLIENT: 20}

NO_SIGNALS = {
    "sig_heavy_withdrawal": False,
    "sig_dormant": False,
    "sig_broken_pattern": False,
    "sig_shrinking": False,
    "sig_going_dormant": False,
    "sig_never_repeated": False,
}


@pytest.fixture(autouse=True)
def _authed(configured_reviewers, reviewer_1_headers):
    client.headers.update(reviewer_1_headers)
    yield
    client.headers.pop("Authorization", None)


def _delete_test_rows(session) -> None:
    for model in (
        ActiveClientInteraction,
        ActiveClientTouchpoint,
        ClientSituationState,
        ClientRiskFeatures,
        FaAssignment,
        ActiveClientFund,
        ClientTier,
    ):
        session.execute(delete(model).where(model.client_id.in_(ALL_TEST_CLIENTS)))
    session.execute(delete(SignalRun).where(SignalRun.run_id == RUN_ID))
    session.commit()


@pytest.fixture
def seeded_book(db):
    today = today_in_nairobi()
    with SessionLocal() as session:
        _delete_test_rows(session)
        session.add(SignalRun(run_id=RUN_ID, state="completed"))
        session.flush()
        for client_id, score in SCORES.items():
            session.add(
                ActiveClientFund(
                    client_id=client_id,
                    unit_fund_id=FUND,
                    client_code=f"C{client_id}",
                    balance=10_000.0,
                    n_deposits=1,
                    n_withdrawals=0,
                )
            )
            session.add(
                FaAssignment(
                    client_id=client_id,
                    unit_fund_id=FUND,
                    fa_id=ADVISOR,
                    fa_name="Next Action Test",
                    source="feed",
                )
            )
            session.add(
                ClientTier(
                    client_id=client_id,
                    purchased_recently=False,
                    real_withdrawer=False,
                    tier=TIER_GRADUAL_DEPOSITOR,
                    segment=SEGMENT_BY_TIER[TIER_GRADUAL_DEPOSITOR],
                    computed_at=datetime.now(UTC),
                    thresholds_version=1,
                    run_id="next-action-test",
                )
            )
            session.add(
                ClientRiskFeatures(
                    client_id=client_id,
                    unit_fund_id=FUND,
                    balance_tier="Small",
                    **NO_SIGNALS,
                    risk_score=score,
                    risk_band="High",
                    risk_reasons="test",
                    fund_at_risk=1_000.0,
                    config_version=1,
                    route="fa_action",
                    queue_rank=None,
                )
            )
        for client_id, situation in SITUATION_BY_CLIENT.items():
            session.add(
                ClientSituationState(
                    client_id=client_id,
                    unit_fund_id=FUND,
                    situation_code=situation,
                    is_active=True,
                    signal_codes=[],
                    since=today,
                    run_id=RUN_ID,
                )
            )
        session.commit()
    yield
    with SessionLocal() as session:
        _delete_test_rows(session)


def _roster() -> list[dict]:
    response = client.get(
        ROSTER, params={"fa_id": ADVISOR, "worklist_only": True, "sort": "risk_score"}
    )
    assert response.status_code == 200
    return response.json()["items"]


def _row(client_id: int) -> dict:
    return next(row for row in _roster() if row["client_id"] == client_id)


def _add(*rows) -> None:
    with SessionLocal() as session:
        session.add_all(rows)
        session.commit()


def test_each_client_gets_the_action_for_their_own_situation(seeded_book) -> None:
    call = _row(CALL_CLIENT)["next_action"]
    email = _row(EMAIL_CLIENT)["next_action"]

    assert call == {
        "title": "Review their cash needs",
        "kind": "call",
        "why": call["why"],
    }
    assert (email["title"], email["kind"]) == ("Ask what changed", "email")
    assert _row(NO_SITUATION_CLIENT)["next_action"] is None


def test_a_missed_meeting_is_what_the_next_run_suggests(seeded_book) -> None:
    yesterday = today_in_nairobi() - timedelta(days=1)
    _add(
        ActiveClientTouchpoint(
            client_id=QUIET_CLIENT,
            unit_fund_id=FUND,
            kind="meeting",
            title="Google Meet",
            due_date=yesterday,
            created_by="test",
        )
    )

    action = _row(QUIET_CLIENT)["next_action"]

    assert action["title"] == "Follow up on missed meeting"
    assert action["kind"] == "email"
    assert action["why"] == "The meeting due yesterday is still open."


def test_a_logged_contact_keeps_the_client_quiet_and_says_so(seeded_book) -> None:
    _add(
        ActiveClientInteraction(
            client_id=CALL_CLIENT,
            unit_fund_id=FUND,
            type="call_logged",
            outcome="connected",
            reviewer_id=ADVISOR,
        )
    )

    row = _row(CALL_CLIENT)

    assert row["next_action"] is None
    assert row["next_action_note"] == "Called today"


def test_the_profile_carries_the_action_or_the_note(seeded_book) -> None:
    _add(
        ActiveClientInteraction(
            client_id=QUIET_CLIENT,
            unit_fund_id=FUND,
            type="email_sent",
            outcome="connected",
            reviewer_id=ADVISOR,
        )
    )

    suggested = client.get(f"{ROSTER}/{CALL_CLIENT}/{FUND}/profile").json()
    quiet = client.get(f"{ROSTER}/{QUIET_CLIENT}/{FUND}/profile").json()

    assert suggested["next_action"]["title"] == "Review their cash needs"
    assert quiet["next_action"] is None
    assert quiet["next_action_note"] == "Emailed today"


def test_a_fund_gets_a_short_name_from_its_initials() -> None:
    assert short_fund_name("Cytonn Money Market Fund") == "CMMF"
    assert short_fund_name("Cytonn High Yield Fund") == "CHYF"
    assert short_fund_name("Fund 957") == "Fund 957"
