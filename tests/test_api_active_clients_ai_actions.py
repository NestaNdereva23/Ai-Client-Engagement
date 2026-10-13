from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.agents import card_copy
from app.agents.watchlist import GETTING_SMALLER, SIGNED_UP_RECENTLY
from app.db.models.active_clients import ActiveClientFund, ActiveClientInteraction
from app.db.models.agent import AgentActionCatalog
from app.db.models.agent_proposal import AgentProposal, AgentProposalClient
from app.db.models.fa_assignment import FaAssignment
from app.db.models.risk import ClientRiskFeatures
from app.db.models.routing import ClientTier
from app.db.session import SessionLocal
from app.main import app
from app.routing.tiers import SEGMENT_BY_TIER, TIER_GRADUAL_DEPOSITOR
from app.services.fund_names import short_fund_name

client = TestClient(app)

ROSTER = "/api/v1/active-clients"
ADVISOR = "ai-action-fa-1"
FUND = 957
CATALOG_VERSION = 9570
CALL_ACTION = "test_ai_action_call"
EMAIL_ACTION = "test_ai_action_email"
WATCH_ACTION = "test_ai_action_watch"

HIGHER_SCORE_CLIENT = 95701
LOWER_SCORE_CLIENT = 95702
UNSUGGESTED_CLIENT = 95703
ALL_TEST_CLIENTS = [HIGHER_SCORE_CLIENT, LOWER_SCORE_CLIENT, UNSUGGESTED_CLIENT]
SCORES = {
    HIGHER_SCORE_CLIENT: (80, "Critical"),
    LOWER_SCORE_CLIENT: (50, "High"),
    UNSUGGESTED_CLIENT: (30, "Watch"),
}

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
    proposal_ids = session.scalars(
        select(AgentProposal.proposal_id).where(AgentProposal.catalog_version == CATALOG_VERSION)
    ).all()
    session.execute(
        delete(ActiveClientInteraction).where(
            ActiveClientInteraction.client_id.in_(ALL_TEST_CLIENTS)
        )
    )
    if proposal_ids:
        session.execute(
            delete(AgentProposalClient).where(AgentProposalClient.proposal_id.in_(proposal_ids))
        )
        session.execute(delete(AgentProposal).where(AgentProposal.proposal_id.in_(proposal_ids)))
    session.execute(delete(AgentActionCatalog).where(AgentActionCatalog.version == CATALOG_VERSION))
    for model in (ClientRiskFeatures, FaAssignment, ActiveClientFund, ClientTier):
        session.execute(delete(model).where(model.client_id.in_(ALL_TEST_CLIENTS)))
    session.commit()


def _catalog_row(action_code: str, title: str, response_kind: str) -> AgentActionCatalog:
    return AgentActionCatalog(
        version=CATALOG_VERSION,
        action_code=action_code,
        title=title,
        response_kind=response_kind,
        who="test clients",
        evidence_required="test evidence",
        content_mix="balanced",
        default_permission="suggest_only",
    )


@pytest.fixture
def seeded_book(db):
    with SessionLocal() as session:
        _delete_test_rows(session)
        for client_id, (score, band) in SCORES.items():
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
                    fa_name="Ai Action Test",
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
                    run_id="ai-action-test",
                )
            )
            session.add(
                ClientRiskFeatures(
                    client_id=client_id,
                    unit_fund_id=FUND,
                    balance_tier="Small",
                    **NO_SIGNALS,
                    risk_score=score,
                    risk_band=band,
                    risk_reasons="test",
                    fund_at_risk=1_000.0,
                    config_version=1,
                    route="fa_action",
                    queue_rank=None,
                )
            )
        session.add_all(
            [
                _catalog_row(CALL_ACTION, "Ask someone to call them", "phone_call"),
                _catalog_row(EMAIL_ACTION, "Check in before it gets worse", "automated_email"),
                _catalog_row(WATCH_ACTION, "Watch this group", "monitor_only"),
            ]
        )
        session.commit()
    yield
    with SessionLocal() as session:
        _delete_test_rows(session)


def _proposal(
    session,
    *,
    action_code: str = CALL_ACTION,
    response_kind: str | None = "phone_call",
    group_name: str = GETTING_SMALLER,
    status: str = "proposed",
    clients: list[int] | None = None,
    left_out: list[int] | None = None,
    created_at: datetime | None = None,
) -> int:
    included = clients if clients is not None else [HIGHER_SCORE_CLIENT]
    left_out = left_out or []
    proposal = AgentProposal(
        action_code=action_code,
        catalog_version=CATALOG_VERSION,
        group_name=group_name,
        client_count=len(included) + len(left_out),
        evidence="test evidence",
        reason="test reason",
        response_kind=response_kind,
        permission_applied="suggest_only",
        status=status,
    )
    if created_at is not None:
        proposal.created_at = created_at
    session.add(proposal)
    session.commit()
    session.add_all(
        [
            AgentProposalClient(
                proposal_id=proposal.proposal_id,
                client_id=client_id,
                unit_fund_id=FUND,
                included=True,
            )
            for client_id in included
        ]
        + [
            AgentProposalClient(
                proposal_id=proposal.proposal_id,
                client_id=client_id,
                unit_fund_id=FUND,
                included=False,
                skip_reason="test skip",
            )
            for client_id in left_out
        ]
    )
    session.commit()
    return proposal.proposal_id


def _roster() -> list[dict]:
    response = client.get(
        ROSTER, params={"fa_id": ADVISOR, "worklist_only": True, "sort": "risk_score"}
    )
    assert response.status_code == 200
    return response.json()["items"]


def _row(client_id: int) -> dict:
    return next(row for row in _roster() if row["client_id"] == client_id)


def _post_choice(client_id: int, type_: str, proposal_id: int | None):
    body = {"type": type_}
    if proposal_id is not None:
        body["proposal_id"] = proposal_id
    return client.post(
        f"{ROSTER}/{client_id}/{FUND}/interactions", params={"username": ADVISOR}, json=body
    )


def test_a_client_in_an_open_proposal_shows_the_ai_action(seeded_book) -> None:
    with SessionLocal() as session:
        proposal_id = _proposal(session, clients=[HIGHER_SCORE_CLIENT, LOWER_SCORE_CLIENT])

    action = _row(HIGHER_SCORE_CLIENT)["ai_action"]

    copy = card_copy.copy_for(GETTING_SMALLER)
    assert action == {
        "proposal_id": proposal_id,
        "action_code": CALL_ACTION,
        "title": "Ask someone to call them",
        "response_kind": "phone_call",
        "response_label": "Phone call",
        "why_now": copy.why_now,
        "group_label": copy.screen_label,
        "group_clients": 2,
        "next_step": "log_call",
        "choice": None,
        "created_at": action["created_at"],
    }
    assert _row(UNSUGGESTED_CLIENT)["ai_action"] is None


@pytest.mark.parametrize("case", ["closed", "left_out", "watch_only"])
def test_a_suggestion_the_advisor_should_not_see_is_not_shown(seeded_book, case) -> None:
    with SessionLocal() as session:
        if case == "closed":
            _proposal(session, status="approved")
        elif case == "left_out":
            _proposal(session, clients=[LOWER_SCORE_CLIENT], left_out=[HIGHER_SCORE_CLIENT])
        else:
            _proposal(session, action_code=WATCH_ACTION, response_kind="monitor_only")

    assert _row(HIGHER_SCORE_CLIENT)["ai_action"] is None


def test_the_newest_open_proposal_wins(seeded_book) -> None:
    with SessionLocal() as session:
        _proposal(session, created_at=datetime.now(UTC) - timedelta(days=1))
        newest = _proposal(
            session,
            action_code=EMAIL_ACTION,
            response_kind="automated_email",
            group_name=SIGNED_UP_RECENTLY,
        )

    action = _row(HIGHER_SCORE_CLIENT)["ai_action"]

    assert action["proposal_id"] == newest
    assert action["next_step"] == "client_record"


def test_taking_up_an_ai_action_is_saved_and_moves_the_client_down(seeded_book) -> None:
    with SessionLocal() as session:
        proposal_id = _proposal(session)
    assert [row["client_id"] for row in _roster()][:2] == [HIGHER_SCORE_CLIENT, LOWER_SCORE_CLIENT]

    response = _post_choice(HIGHER_SCORE_CLIENT, "ai_action_taken", proposal_id)

    assert response.status_code == 201
    assert response.json()["proposal_id"] == proposal_id
    rows = _roster()
    assert [row["client_id"] for row in rows][:2] == [LOWER_SCORE_CLIENT, UNSUGGESTED_CLIENT]
    assert rows[-1]["client_id"] == HIGHER_SCORE_CLIENT
    assert rows[-1]["ai_action"]["choice"] == "taken"


def test_setting_an_ai_action_aside_is_saved(seeded_book) -> None:
    with SessionLocal() as session:
        proposal_id = _proposal(session)

    assert _post_choice(HIGHER_SCORE_CLIENT, "ai_action_set_aside", proposal_id).status_code == 201

    assert _row(HIGHER_SCORE_CLIENT)["ai_action"]["choice"] == "set_aside"


def test_a_choice_needs_an_open_proposal_that_includes_the_client(seeded_book) -> None:
    with SessionLocal() as session:
        open_for_higher = _proposal(session)
        closed = _proposal(session, status="approved", clients=[LOWER_SCORE_CLIENT])

    other_clients_proposal = _post_choice(LOWER_SCORE_CLIENT, "ai_action_taken", open_for_higher)
    closed_proposal = _post_choice(LOWER_SCORE_CLIENT, "ai_action_taken", closed)
    missing_proposal = _post_choice(LOWER_SCORE_CLIENT, "ai_action_taken", None)
    stray_proposal = _post_choice(LOWER_SCORE_CLIENT, "call_logged", open_for_higher)

    assert other_clients_proposal.status_code == 404
    assert closed_proposal.status_code == 409
    assert missing_proposal.status_code == 422
    assert stray_proposal.status_code == 422


def test_the_profile_carries_the_ai_action(seeded_book) -> None:
    with SessionLocal() as session:
        proposal_id = _proposal(session)

    profile = client.get(f"{ROSTER}/{HIGHER_SCORE_CLIENT}/{FUND}/profile").json()
    without = client.get(f"{ROSTER}/{UNSUGGESTED_CLIENT}/{FUND}/profile").json()

    assert profile["ai_action"]["proposal_id"] == proposal_id
    assert without["ai_action"] is None


def test_a_fund_gets_a_short_name_from_its_initials() -> None:
    assert short_fund_name("Cytonn Money Market Fund") == "CMMF"
    assert short_fund_name("Cytonn High Yield Fund") == "CHYF"
    assert short_fund_name("Fund 957") == "Fund 957"
