from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from app.agents.campaign_runs import (
    CampaignRunInProgress,
    fail_stale_campaign_runs,
    start_campaign_run,
)
from app.agents.campaign_suggester import NothingSaved, execute_suggestion_run
from app.agents.campaign_tool_runner import make_campaign_tool_runner
from app.api import agent_scope
from app.config import Settings
from app.db.models.campaign_agent_run import (
    PLAN_RUN,
    SUGGEST_RUN,
    CampaignAgentRun,
    CampaignAgentToolCall,
)
from app.db.models.outreach import Campaign
from app.db.session import SessionLocal
from app.editable_settings import SettingValueError
from app.main import app
from app.privacy.llm_client import (
    CampaignAgentNotConfigured,
    ConversationTurn,
    LLMUsage,
    get_campaign_agent_llm_client,
)
from app.services.app_settings import update_app_settings

client = TestClient(app)


@pytest.fixture(autouse=True)
def _signed_tokens(configured_reviewers):
    yield


@pytest.fixture
def campaign_ids(db):
    ids: list[int] = []
    with SessionLocal() as session:
        for name in ("campaign runs test one", "campaign runs test two"):
            campaign = Campaign(name=name, campaign_type="dormant_reengagement")
            session.add(campaign)
            session.flush()
            ids.append(campaign.campaign_id)
        session.commit()
    yield ids
    with SessionLocal() as session:
        runs = select(CampaignAgentRun.run_id).where(CampaignAgentRun.campaign_id.in_(ids))
        session.execute(delete(CampaignAgentToolCall).where(CampaignAgentToolCall.run_id.in_(runs)))
        session.execute(delete(CampaignAgentRun).where(CampaignAgentRun.campaign_id.in_(ids)))
        session.execute(delete(Campaign).where(Campaign.campaign_id.in_(ids)))
        session.commit()


def test_only_one_run_goes_for_a_campaign_at_a_time(campaign_ids) -> None:
    first, second = campaign_ids
    with SessionLocal() as session:
        run = start_campaign_run(session, kind=PLAN_RUN, campaign_id=first)

        with pytest.raises(CampaignRunInProgress):
            start_campaign_run(session, kind=PLAN_RUN, campaign_id=first)
        other = start_campaign_run(session, kind=PLAN_RUN, campaign_id=second)

        run.state = "completed"
        other.state = "completed"
        session.commit()
        assert start_campaign_run(session, kind=PLAN_RUN, campaign_id=first).run_id != run.run_id


def test_the_database_refuses_a_second_running_run_even_if_the_check_is_skipped(
    campaign_ids,
) -> None:
    with SessionLocal() as session:
        session.add(CampaignAgentRun(kind=PLAN_RUN, campaign_id=campaign_ids[0]))
        session.commit()
        session.add(CampaignAgentRun(kind=PLAN_RUN, campaign_id=campaign_ids[0]))
        with pytest.raises(IntegrityError, match="uq_campaign_agent_run_one_running"):
            session.commit()
        session.rollback()


def test_a_run_that_never_finished_is_marked_failed(campaign_ids) -> None:
    with SessionLocal() as session:
        run = start_campaign_run(session, kind=PLAN_RUN, campaign_id=campaign_ids[0])
        run.started_at = datetime.now(UTC) - timedelta(hours=2)
        session.commit()

        assert fail_stale_campaign_runs(session) == [run.run_id]

        session.refresh(run)
        assert run.state == "failed"
        assert "did not finish" in run.failure_reason
        assert start_campaign_run(session, kind=PLAN_RUN, campaign_id=campaign_ids[0])


def test_the_tool_runner_runs_only_the_tools_it_was_given(campaign_ids) -> None:
    with SessionLocal() as session:
        run = start_campaign_run(session, kind=PLAN_RUN, campaign_id=campaign_ids[0])
        run_id = run.run_id
        call = make_campaign_tool_runner(session, run_id, {"ping": lambda session: {"ok": True}})

        assert call("ping", {}) == {"ok": True}
        refused = call("get_client_facts", {"client_id": 1})
        wrong_input = call("ping", {"surprise": 1})
        session.rollback()

        assert refused["error"] == "unknown_tool"
        assert wrong_input["error"] == "invalid_input"
        saved = session.scalars(
            select(CampaignAgentToolCall.tool_name)
            .where(CampaignAgentToolCall.run_id == run_id)
            .order_by(CampaignAgentToolCall.ordinal)
        ).all()
        assert saved == ["ping", "get_client_facts", "ping"]


def test_the_campaign_model_must_be_chosen_and_have_its_key() -> None:
    with pytest.raises(CampaignAgentNotConfigured, match="pick one in Settings"):
        get_campaign_agent_llm_client(Settings(campaign_agent_llm_model=""))
    with pytest.raises(CampaignAgentNotConfigured, match="CAMPAIGN_AGENT_LLM_API_KEY"):
        get_campaign_agent_llm_client(
            Settings(campaign_agent_llm_provider="anthropic", campaign_agent_llm_model="m")
        )
    assert get_campaign_agent_llm_client(Settings(campaign_agent_llm_model="phi4-mini"))


def test_a_provider_without_its_credentials_cannot_be_chosen_on_the_settings_screen(
    monkeypatch,
) -> None:
    from app.services import app_settings

    monkeypatch.setattr(app_settings, "get_settings", lambda: Settings())

    with SessionLocal() as session:
        with pytest.raises(SettingValueError, match="CAMPAIGN_AGENT_LLM_API_KEY"):
            update_app_settings(
                session,
                changes={"campaign_agent_llm_provider": "anthropic"},
                reason="trying a provider",
                confirm_live=False,
                actor_id="test",
            )


def test_a_campaign_can_be_read_by_someone_with_no_advisor_book_while_scoping_is_on(
    campaign_ids, monkeypatch, headers_for
) -> None:
    monkeypatch.setattr(agent_scope, "get_settings", lambda: Settings(agent_fa_scoped=True))

    response = client.get(
        f"/api/v1/campaigns/{campaign_ids[0]}/agent-overview",
        headers=headers_for("nobody-has-access@example.test"),
    )

    assert response.status_code == 200
    assert response.json()["campaign"]["name"] == "campaign runs test one"


class _AnswersInPlainText:
    model = "plain-text-model"

    def converse(self, *, system, messages, tools=()):
        usage = LLMUsage(input_tokens=1, output_tokens=1)
        return ConversationTurn("Nothing to suggest.", (), usage, "end_turn")


def test_a_suggestion_run_that_saves_no_draft_is_a_failed_run(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.agents.campaign_suggester.waiting_pool_summary", lambda session: {"total": 5}
    )
    with SessionLocal() as session:
        run_id = start_campaign_run(session, kind=SUGGEST_RUN).run_id
    try:
        with SessionLocal() as session:
            with pytest.raises(NothingSaved):
                execute_suggestion_run(session, run_id, llm_client=_AnswersInPlainText())
        with SessionLocal() as session:
            run = session.get(CampaignAgentRun, run_id)
            assert run.state == "failed"
            assert "saved no drafts and made 0 tool calls" in run.failure_reason
    finally:
        with SessionLocal() as session:
            session.execute(delete(CampaignAgentRun).where(CampaignAgentRun.run_id == run_id))
            session.commit()
