from __future__ import annotations

from collections import Counter
from datetime import UTC, date, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select
from test_api_campaigns import accepted_state, make_settings

from app.agents import guide_mix, write_tools
from app.agents.action_performance import build_action_performance
from app.agents.proposal_split import (
    ANGLE_TEST,
    GUIDE_TEST,
    SplitRefused,
    assign_sides,
    load_split,
    split_proposal,
)
from app.agents.results_summary import read_results, results_prompt_text
from app.config import get_settings
from app.db.models.action_performance import ActionPerformance
from app.db.models.action_result import ActionResult
from app.db.models.agent_proposal import AgentProposal, AgentProposalClient, AgentProposalVariant
from app.db.models.audit import AuditLog
from app.db.models.llmops import GenerationRun
from app.db.models.models import Clients, Funds
from app.db.models.outreach import Campaign, OutreachMessage
from app.db.models.rag import DOC_TYPE_CLIENT_GUIDE
from app.db.session import SessionLocal
from app.llmops.versions import persist_generation_run
from app.main import app
from app.rag.retrieve import Retrieved

client = TestClient(app)

PROPOSALS = "/api/v1/agent/proposals"

AS_OF = date(2026, 9, 20)
NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
SENT_AT = datetime(2026, 8, 5, 8, 0, tzinfo=UTC)

GROUP = "proposal split test group"
CAMPAIGN_NAME = "Proposal split test campaign"
SENDING_ACTION = "welcome_and_top_up"
SENDING_ANGLE = "welcome_and_top_up"
OTHER_ANGLE = "your_next_deposit"
SILENT_ACTION = "do_nothing"
MEASURED_ACTION = "proposal_split_measured_action"
MEASURED_ANGLE = "proposal_split_measured_angle"

FUND_ID = 99639
OTHER_FUND_ID = 99640
CLIENTS = tuple(range(963901, 963913))

GUIDE = Retrieved(
    chunk_id=7,
    text="Small deposits made regularly are easier to keep up.",
    metadata={},
    score=1.0,
    version_id=1,
    doc_type=DOC_TYPE_CLIENT_GUIDE,
)


def _purge() -> None:
    with SessionLocal() as session:
        proposal_ids = session.scalars(
            select(AgentProposal.proposal_id).where(AgentProposal.group_name == GROUP)
        ).all()
        session.execute(delete(ActionResult).where(ActionResult.proposal_id.in_(proposal_ids)))
        session.execute(
            delete(ActionPerformance).where(ActionPerformance.action_code == MEASURED_ACTION)
        )
        session.execute(delete(AuditLog).where(AuditLog.entity_type == "action_performance"))
        session.execute(
            delete(AuditLog).where(
                AuditLog.entity_type == "agent_proposal",
                AuditLog.entity_id.in_([str(proposal_id) for proposal_id in proposal_ids]),
            )
        )
        session.execute(delete(OutreachMessage).where(OutreachMessage.client_id.in_(CLIENTS)))
        session.execute(delete(GenerationRun).where(GenerationRun.client_id.in_(CLIENTS)))
        session.execute(
            delete(AgentProposalVariant).where(AgentProposalVariant.proposal_id.in_(proposal_ids))
        )
        session.execute(
            delete(AgentProposalClient).where(AgentProposalClient.proposal_id.in_(proposal_ids))
        )
        session.execute(delete(AgentProposal).where(AgentProposal.group_name == GROUP))
        session.execute(delete(Campaign).where(Campaign.name == CAMPAIGN_NAME))
        session.execute(delete(Clients).where(Clients.client_id.in_(CLIENTS)))
        session.execute(delete(Funds).where(Funds.unit_fund_id.in_([FUND_ID, OTHER_FUND_ID])))
        session.commit()


@pytest.fixture
def clean(db: None):
    _purge()
    yield
    _purge()


def _proposal(
    session,
    *,
    status: str = "approved",
    action_code: str = SENDING_ACTION,
    angle: str | None = SENDING_ANGLE,
    content_mix: str | None = "mostly_learning",
    campaign_id: int | None = None,
) -> int:
    row = AgentProposal(
        action_code=action_code,
        catalog_version=1,
        group_name=GROUP,
        client_count=len(CLIENTS),
        evidence="evidence",
        reason="reason",
        angle=angle,
        content_mix=content_mix,
        permission_applied="approve_each",
        status=status,
        campaign_id=campaign_id,
    )
    session.add(row)
    session.flush()
    return row.proposal_id


def _members(session, proposal_id: int, client_ids, *, fund_id: int = FUND_ID) -> None:
    session.add_all(
        AgentProposalClient(
            proposal_id=proposal_id, client_id=client_id, unit_fund_id=fund_id, included=True
        )
        for client_id in client_ids
    )
    session.flush()


def _variants_of(session, proposal_id: int) -> dict[int, set[str | None]]:
    seen: dict[int, set[str | None]] = {}
    for row in session.scalars(
        select(AgentProposalClient).where(AgentProposalClient.proposal_id == proposal_id)
    ):
        seen.setdefault(row.client_id, set()).add(row.variant)
    return seen


def test_clients_are_split_evenly_and_the_same_way_every_time() -> None:
    for count in (10, 11):
        client_ids = list(range(1000, 1000 + count))
        first = assign_sides(7, client_ids)
        again = assign_sides(7, reversed(client_ids))
        sides = list(first.values())

        assert first == again
        assert set(first) == set(client_ids)
        assert abs(sides.count("A") - sides.count("B")) == count % 2


def test_a_split_gives_each_included_client_one_version_and_cannot_be_done_twice(clean) -> None:
    with SessionLocal() as session:
        proposal_id = _proposal(session)
        _members(session, proposal_id, CLIENTS[:6])
        _members(session, proposal_id, CLIENTS[:1], fund_id=OTHER_FUND_ID)
        session.add(
            AgentProposalClient(
                proposal_id=proposal_id,
                client_id=CLIENTS[6],
                unit_fund_id=FUND_ID,
                included=False,
                skip_reason="open_complaint",
            )
        )
        session.commit()

        split = split_proposal(
            session,
            proposal_id,
            test=ANGLE_TEST,
            other_angle=OTHER_ANGLE,
            split_by="tester",
            as_of=AS_OF,
        )
        session.commit()
        first = _variants_of(session, proposal_id)

        with pytest.raises(SplitRefused, match="already split"):
            split_proposal(
                session,
                proposal_id,
                test=ANGLE_TEST,
                other_angle=OTHER_ANGLE,
                split_by="tester",
                as_of=AS_OF,
            )
        session.rollback()

        assert first == _variants_of(session, proposal_id)
        assert load_split(session, proposal_id).variant_of == split.variant_of
        sides = session.scalars(
            select(AgentProposalVariant).where(AgentProposalVariant.proposal_id == proposal_id)
        ).all()

    assert {(side.variant, side.angle, side.content_mix) for side in sides} == {
        ("A", SENDING_ANGLE, "mostly_learning"),
        ("B", OTHER_ANGLE, "mostly_learning"),
    }
    for included_client in CLIENTS[:6]:
        assert len(first[included_client]) == 1
        assert first[included_client] <= {"A", "B"}
    assert first[CLIENTS[6]] == {None}
    given = [next(iter(first[included_client])) for included_client in CLIENTS[:6]]
    assert (given.count("A"), given.count("B")) == (3, 3)


def test_a_proposal_that_would_not_make_a_fair_test_is_refused(clean, monkeypatch) -> None:
    monkeypatch.setattr(guide_mix, "find_guides", lambda *args, **kwargs: [])

    def refusal(proposal_id: int, **asked) -> str:
        with SessionLocal() as session:
            with pytest.raises(SplitRefused) as caught:
                split_proposal(session, proposal_id, split_by="tester", as_of=AS_OF, **asked)
            session.rollback()
        return str(caught.value)

    with SessionLocal() as session:
        running = _proposal(session, status="running")
        silent = _proposal(session, action_code=SILENT_ACTION, angle=None)
        plain = _proposal(session)
        for proposal_id in (running, silent, plain):
            _members(session, proposal_id, CLIENTS[:4])
        lonely = _proposal(session)
        _members(session, lonely, CLIENTS[:1])
        session.commit()

    assert "only a proposal that has not started" in refusal(
        running, test=ANGLE_TEST, other_angle=OTHER_ANGLE
    )
    assert "sends no message" in refusal(silent, test=GUIDE_TEST)
    assert "no approved client guide" in refusal(plain, test=GUIDE_TEST)
    assert "same as the first" in refusal(plain, test=ANGLE_TEST, other_angle=SENDING_ANGLE)
    assert "no angle 'made_up'" in refusal(plain, test=ANGLE_TEST, other_angle="made_up")
    assert "needs at least one" in refusal(lonely, test=ANGLE_TEST, other_angle=OTHER_ANGLE)

    with SessionLocal() as session:
        assert session.scalars(select(AgentProposalVariant)).all() == []


def test_only_version_a_of_a_guide_test_is_drafted_with_the_guide(clean, monkeypatch) -> None:
    monkeypatch.setattr(guide_mix, "find_guides", lambda *args, **kwargs: [GUIDE])
    with SessionLocal() as session:
        campaign = Campaign(
            name=CAMPAIGN_NAME, campaign_type="agent_proposal", status="running", start_date=AS_OF
        )
        session.add(campaign)
        session.flush()
        proposal_id = _proposal(session, campaign_id=campaign.campaign_id)
        _members(session, proposal_id, CLIENTS[:6])
        session.flush()
        split = split_proposal(
            session, proposal_id, test=GUIDE_TEST, split_by="tester", as_of=AS_OF
        )
        session.commit()
        campaign_id = campaign.campaign_id

    used: dict[int, SimpleNamespace] = {}

    def fake_orchestrator(session, settings, **kwargs):
        return SimpleNamespace(**kwargs)

    def fake_generate(session, enrollment, step_no, *, orchestrator, channel, settings):
        return orchestrator

    def fake_run_due(session, *, campaign_id, generate, limit):
        for client_id in CLIENTS[:6]:
            used[client_id] = generate(session, SimpleNamespace(client_id=client_id), 1)
        return []

    monkeypatch.setattr(write_tools, "build_default_orchestrator", fake_orchestrator)
    monkeypatch.setattr(write_tools, "generate_for_enrollment", fake_generate)
    monkeypatch.setattr(write_tools, "run_due_enrollments", fake_run_due)

    with SessionLocal() as session:
        write_tools.draft_into_review_queue(session, campaign_id=campaign_id, prohibitions=())

    def prompt_for(client_id: int) -> str:
        orchestrator = used[client_id]
        return orchestrator.prompt_builder(
            angle=None, prompt_variant=None, chunks=orchestrator.extra_chunks
        )

    for client_id, variant in split.variant_of.items():
        prompt = prompt_for(client_id)
        if variant == "A":
            assert "mostly explains" in prompt
            assert GUIDE.text in prompt
        else:
            assert "How much to explain and how much to ask" not in prompt
            assert GUIDE.text not in prompt
    assert {split.variant_of[client_id] for client_id in CLIENTS[:6]} == {"A", "B"}


OUTCOMES = {
    "A": [(True, 1000.0), (True, 1000.0), (False, 1000.0), (False, 0.0), (False, 0.0)],
    "B": [(True, 500.0), (False, 0.0), (False, 0.0), (False, 0.0), (False, 0.0)],
}


def _sent(
    session,
    campaign_id: int,
    proposal_id: int,
    client_id: int,
    variant: str,
    *,
    replied: bool,
    deposit: float,
) -> None:
    session.add(
        Clients(
            client_id=client_id, unit_fund_id=FUND_ID, n_purchases_returned=0, n_sales_returned=0
        )
    )
    session.commit()
    generation = persist_generation_run(session, accepted_state(client_id), make_settings())
    message = OutreachMessage(
        message_id=uuid4().hex,
        campaign_id=campaign_id,
        generation_run_id=generation.run_id,
        client_id=client_id,
        ai_draft_content={"subject": "Subject", "body": "Body"},
        status="approved",
    )
    session.add(message)
    session.commit()
    session.add(
        AgentProposalClient(
            proposal_id=proposal_id,
            client_id=client_id,
            unit_fund_id=FUND_ID,
            included=True,
            variant=variant,
        )
    )
    session.add(
        ActionResult(
            proposal_id=proposal_id,
            client_id=client_id,
            unit_fund_id=FUND_ID,
            message_id=message.message_id,
            sent_at=SENT_AT,
            window_days=30,
            replied=replied,
            deposited=deposit > 0,
            deposit_amount_kes=deposit,
            measured_at=NOW,
        )
    )
    session.commit()


@pytest.fixture
def measured(clean):
    with SessionLocal() as session:
        session.add(Funds(unit_fund_id=FUND_ID, unit_fund_name="A proposal split test fund"))
        campaign = Campaign(name=CAMPAIGN_NAME, status="running")
        session.add(campaign)
        session.flush()
        proposal_id = _proposal(
            session,
            status="sent",
            action_code=MEASURED_ACTION,
            angle=MEASURED_ANGLE,
            content_mix="balanced",
            campaign_id=campaign.campaign_id,
        )
        unsplit_id = _proposal(session, action_code=MEASURED_ACTION, angle=MEASURED_ANGLE)
        session.add_all(
            [
                AgentProposalVariant(
                    proposal_id=proposal_id,
                    variant="A",
                    angle=MEASURED_ANGLE,
                    content_mix="balanced",
                ),
                AgentProposalVariant(
                    proposal_id=proposal_id, variant="B", angle=MEASURED_ANGLE, content_mix=None
                ),
            ]
        )
        session.commit()
        clients = iter(CLIENTS)
        for variant, outcomes in OUTCOMES.items():
            for replied, deposit in outcomes:
                _sent(
                    session,
                    campaign.campaign_id,
                    proposal_id,
                    next(clients),
                    variant,
                    replied=replied,
                    deposit=deposit,
                )
    yield proposal_id, unsplit_id


@pytest.fixture
def reading(monkeypatch):
    monkeypatch.setenv("ACTION_PERFORMANCE_PERIOD", "7d")
    monkeypatch.setenv("ACTION_PERFORMANCE_LOOKBACK", "36500d")
    monkeypatch.setenv("ACTION_PERFORMANCE_READ_WINDOW_DAYS", "30")
    monkeypatch.setenv("AGENT_QUERY_MIN_GROUP_SIZE", "3")
    get_settings.cache_clear()
    yield
    monkeypatch.undo()
    get_settings.cache_clear()


def test_both_versions_show_up_in_the_weekly_summary(measured, reading) -> None:
    with SessionLocal() as session:
        build_action_performance(session, now=NOW, period_hours=168, windows=(30,))
        rows = session.scalars(
            select(ActionPerformance).where(ActionPerformance.action_code == MEASURED_ACTION)
        ).all()
        summary = read_results(session, action_code=MEASURED_ACTION, now=NOW)

    sent: Counter[tuple[str, str, str]] = Counter()
    for row in rows:
        sent[(row.variant, row.content_mix, row.angle)] += row.sent_count
    assert sent == {
        ("A", "balanced", MEASURED_ANGLE): 5,
        ("B", "none", MEASURED_ANGLE): 5,
    }

    version_a, version_b = summary.lines
    assert (version_a.variant, version_a.guide_mix) == ("A", "balanced")
    assert (version_a.reply_percent, version_a.deposit_percent) == (40.0, 60.0)
    assert version_a.money_in_kes == 3000
    assert (version_b.variant, version_b.guide_mix) == ("B", "none")
    assert (version_b.reply_percent, version_b.deposit_percent) == (20.0, 20.0)
    assert version_b.money_in_kes == 500

    text = results_prompt_text(summary, ask="Pick one.")
    assert "version A (guide mix: Balanced)" in text
    assert "version B (no client guide)" in text


def test_the_versions_endpoint_puts_the_two_sides_next_to_each_other(
    measured, reading, configured_reviewers, reviewer_1_headers
) -> None:
    proposal_id, unsplit_id = measured

    response = client.get(
        f"{PROPOSALS}/{proposal_id}/versions",
        params={"window_days": 30},
        headers=reviewer_1_headers,
    )

    assert response.status_code == 200
    body = response.json()
    assert (body["differs_in"], body["window_days"], body["enough_to_compare"]) == (
        "guide",
        30,
        True,
    )
    fields = ("variant", "guide_label", "clients", "measured", "reply_percent")
    version_a, version_b = body["sides"]
    assert tuple(version_a[field] for field in fields) == ("A", "Balanced", 5, 5, 40.0)
    assert tuple(version_b[field] for field in fields) == ("B", "No client guide", 5, 5, 20.0)
    assert (version_a["deposit_percent"], version_a["money_in_kes"]) == (60.0, 3000.0)
    assert (version_b["deposit_percent"], version_b["money_in_kes"]) == (20.0, 500.0)

    for missing in (unsplit_id, 999999999):
        not_found = client.get(f"{PROPOSALS}/{missing}/versions", headers=reviewer_1_headers)
        assert not_found.status_code == 404
