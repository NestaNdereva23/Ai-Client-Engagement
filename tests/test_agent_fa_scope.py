from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.agents.fa_scope import fa_scope
from app.agents.watchlist import situation_group
from app.api import agent_scope
from app.config import Settings
from app.db.models.active_clients import ActiveClientFund
from app.db.models.agent_insight import AgentInsight
from app.db.models.fa_assignment import FaAssignment
from app.db.models.signals import ClientSituationState, SignalRun
from app.db.models.user_access import ROLE_ADMIN, ROLE_ADVISOR, UserAccess
from app.db.session import SessionLocal
from app.main import app

client = TestClient(app)

INSIGHTS = "/api/v1/agent/insights"
FUND = 9992
CLIENT_ASHA = 99920
CLIENT_BRIAN = 99921
ASHA = "scope-asha@example.test"
BRIAN = "scope-brian@example.test"
ADMIN = "scope-admin@example.test"
EMAILS = [ASHA, BRIAN, ADMIN]
SITUATION = "deposit_size_falling"
SIGNAL_RUN = "scope-test-signal-run"


@pytest.fixture(autouse=True)
def _signed_tokens(configured_reviewers):
    yield


@pytest.fixture
def scoped(monkeypatch):
    monkeypatch.setattr(agent_scope, "get_settings", lambda: Settings(agent_fa_scoped=True))


@pytest.fixture
def book(db):
    with SessionLocal() as session:
        _clear(session)
        session.add_all(
            [
                UserAccess(email=ASHA, role=ROLE_ADVISOR, fa_id="scope-fa-asha", ai_assistant=True),
                UserAccess(
                    email=BRIAN, role=ROLE_ADVISOR, fa_id="scope-fa-brian", ai_assistant=True
                ),
                UserAccess(email=ADMIN, role=ROLE_ADMIN),
            ]
        )
        for client_id, fa_id in ((CLIENT_ASHA, "scope-fa-asha"), (CLIENT_BRIAN, "scope-fa-brian")):
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
                FaAssignment(client_id=client_id, unit_fund_id=FUND, fa_id=fa_id, source="feed")
            )
        session.commit()
    yield
    with SessionLocal() as session:
        _clear(session)


def _clear(session) -> None:
    clients = [CLIENT_ASHA, CLIENT_BRIAN]
    session.execute(delete(ClientSituationState).where(ClientSituationState.client_id.in_(clients)))
    session.execute(delete(SignalRun).where(SignalRun.run_id == SIGNAL_RUN))
    session.execute(delete(AgentInsight).where(AgentInsight.title.like("scope test%")))
    session.execute(delete(FaAssignment).where(FaAssignment.client_id.in_(clients)))
    session.execute(delete(ActiveClientFund).where(ActiveClientFund.client_id.in_(clients)))
    session.execute(delete(UserAccess).where(UserAccess.email.in_(EMAILS)))
    session.commit()


def _insight(fa_id: str, title: str) -> int:
    with SessionLocal() as session:
        row = AgentInsight(
            fa_id=fa_id,
            kind="risk",
            title=title,
            group_name=SITUATION,
            client_count=1,
            confidence="medium",
            confidence_reason="test",
            suggestion="test",
            why_now="test",
        )
        session.add(row)
        session.commit()
        return row.insight_id


def _titles(headers: dict[str, str]) -> set[str]:
    response = client.get(INSIGHTS, params={"limit": 200}, headers=headers)
    assert response.status_code == 200, response.text
    return {
        row["title"] for row in response.json()["items"] if row["title"].startswith("scope test")
    }


def test_an_advisor_sees_only_their_own_findings(book, scoped, headers_for) -> None:
    mine = _insight("scope-fa-asha", "scope test asha")
    theirs = _insight("scope-fa-brian", "scope test brian")

    assert _titles(headers_for(ASHA)) == {"scope test asha"}
    assert client.get(f"{INSIGHTS}/{mine}", headers=headers_for(ASHA)).status_code == 200
    assert client.get(f"{INSIGHTS}/{theirs}", headers=headers_for(ASHA)).status_code == 404


def test_an_admin_sees_every_advisors_findings(book, scoped, headers_for) -> None:
    _insight("scope-fa-asha", "scope test asha")
    _insight("scope-fa-brian", "scope test brian")

    assert _titles(headers_for(ADMIN)) == {"scope test asha", "scope test brian"}


def test_findings_are_not_filtered_while_the_setting_is_off(book, headers_for) -> None:
    _insight("scope-fa-asha", "scope test asha")
    _insight("scope-fa-brian", "scope test brian")

    assert _titles(headers_for(ASHA)) == {"scope test asha", "scope test brian"}


def test_a_scoped_run_builds_groups_from_that_advisors_clients_only(book) -> None:
    with SessionLocal() as session:
        session.add(SignalRun(run_id=SIGNAL_RUN, state="completed"))
        session.flush()
        for client_id in (CLIENT_ASHA, CLIENT_BRIAN):
            session.add(
                ClientSituationState(
                    client_id=client_id,
                    unit_fund_id=FUND,
                    situation_code=SITUATION,
                    is_active=True,
                    signal_codes=[],
                    since=date.today(),
                    run_id=SIGNAL_RUN,
                )
            )
        session.commit()

        everyone = {m.client_id for m in situation_group(session, SITUATION).members}
        with fa_scope("scope-fa-asha"):
            asha_only = {m.client_id for m in situation_group(session, SITUATION).members}

    assert {CLIENT_ASHA, CLIENT_BRIAN} <= everyone
    assert asha_only & {CLIENT_ASHA, CLIENT_BRIAN} == {CLIENT_ASHA}
