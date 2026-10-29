from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.agents.campaign_runs import start_campaign_run
from app.api import agent_scope
from app.config import Settings
from app.db.models.campaign_agent_run import SUGGEST_RUN, CampaignAgentRun
from app.db.models.user_access import ROLE_ADMIN, ROLE_ADVISOR, UserAccess
from app.db.session import SessionLocal
from app.main import app

client = TestClient(app)

RUN = "/api/v1/campaigns/suggestions/run"
ADMIN = "suggestion-run-admin@example.test"
ADVISOR = "suggestion-run-advisor@example.test"


@pytest.fixture(autouse=True)
def _signed_tokens(configured_reviewers):
    yield


@pytest.fixture
def admin_viewing_as_an_advisor(db, monkeypatch):
    monkeypatch.setattr(agent_scope, "get_settings", lambda: Settings(agent_fa_scoped=True))
    with SessionLocal() as session:
        session.add_all(
            [
                UserAccess(email=ADVISOR, role=ROLE_ADVISOR, fa_id="suggestion-run-fa"),
                UserAccess(email=ADMIN, role=ROLE_ADMIN, view_as_email=ADVISOR),
            ]
        )
        session.commit()
    yield
    with SessionLocal() as session:
        session.execute(delete(UserAccess).where(UserAccess.email.in_([ADMIN, ADVISOR])))
        session.commit()


def test_the_latest_run_can_be_read_while_viewing_as_a_scoped_advisor(
    admin_viewing_as_an_advisor, headers_for
) -> None:
    with SessionLocal() as session:
        run_id = start_campaign_run(session, kind=SUGGEST_RUN).run_id
    try:
        response = client.get(RUN, headers=headers_for(ADMIN))

        assert response.status_code == 200
        assert response.json()["run_id"] == run_id
        assert response.json()["state"] == "running"
    finally:
        with SessionLocal() as session:
            session.execute(delete(CampaignAgentRun).where(CampaignAgentRun.run_id == run_id))
            session.commit()
