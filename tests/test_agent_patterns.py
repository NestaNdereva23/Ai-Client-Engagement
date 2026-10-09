from __future__ import annotations

from datetime import UTC, date, datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select
from test_api_campaigns import accepted_state, make_settings

from app.agents.action_agent import InsightNotActionable, start_action_run
from app.agents.pattern_search import (
    NOT_MESSAGED,
    Counts,
    NotMessaged,
    Observation,
    PatternRules,
    find_patterns,
    run_pattern_search,
    two_proportion_z,
)
from app.agents.pattern_writeup import READ_PATTERN, WRITE_PATTERN_INSIGHT, write_up_patterns
from app.config import get_settings
from app.db.models.action_result import ActionResult
from app.db.models.active_clients import ActiveClientFund, ActiveTransaction
from app.db.models.agent_insight import AgentInsight, AgentInsightFact
from app.db.models.agent_proposal import AgentProposal, AgentProposalClient
from app.db.models.audit import AuditLog
from app.db.models.campaigns import Enrollment, TouchLog
from app.db.models.llmops import GenerationRun
from app.db.models.models import Clients, Funds
from app.db.models.observed_pattern import ObservedPattern
from app.db.models.outreach import Campaign, OutreachMessage
from app.db.session import SessionLocal
from app.llmops.versions import persist_generation_run
from app.main import app
from app.privacy.llm_client import ConversationTurn, LLMUsage, ToolUseRequest

client = TestClient(app)

PATTERNS = "/api/v1/agent/patterns"

FUND_ID = 99770
WINDOW = 30
CAMPAIGN_NAME = "Pattern search test campaign"
GOOD_GROUP = "pattern_test_good_group"
QUIET_GROUP = "pattern_test_quiet_group"
GOOD_ACTION = "pattern_test_good_action"
QUIET_ACTION = "pattern_test_quiet_action"
GOOD_ANGLE = "pattern_test_good_angle"
QUIET_ANGLE = "pattern_test_quiet_angle"
GOOD_TIER = "pattern_test_good_tier"
QUIET_TIER = "pattern_test_quiet_tier"

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
SENT_AT = datetime(2026, 8, 5, 8, 0, tzinfo=UTC)
LEFT_OUT_AT = datetime(2026, 8, 5, 9, 0, tzinfo=UTC)
LEFT_OUT_AGAIN_AT = datetime(2026, 8, 12, 9, 0, tzinfo=UTC)
DEPOSIT_DAY = date(2026, 8, 8)

GOOD_CLIENTS = tuple(range(997701, 997707))
QUIET_CLIENTS = tuple(range(997707, 997713))
LEFT_OUT_CLIENTS = tuple(range(997713, 997719))
DO_NOT_CONTACT_CLIENT = 997719
MESSAGED_CLIENT = 997720
LOADED_CLIENT = 997721
ALL_CLIENTS = (*GOOD_CLIENTS, *QUIET_CLIENTS, *LEFT_OUT_CLIENTS)
EVERY_CLIENT = (*ALL_CLIENTS, DO_NOT_CONTACT_CLIENT, MESSAGED_CLIENT, LOADED_CLIENT)

RULES = PatternRules(
    min_group_size=30, min_gap_points=5.0, min_z=3.0, max_features=3, max_patterns=10
)

_USAGE = LLMUsage(input_tokens=10, output_tokens=10)


def _batch(
    angle: str, band: str, *, total: int, deposited: int, replied: int = 0, group: str = "g"
) -> list[Observation]:
    return [
        Observation(
            features=(("angle", angle), ("risk_band", band)),
            group_name=group,
            replied=index < replied,
            deposited=index < deposited,
        )
        for index in range(total)
    ]


def test_the_gap_is_measured_against_chance() -> None:
    assert two_proportion_z(Counts(6, 5), Counts(6, 0)) == pytest.approx(2.93, abs=0.01)
    assert two_proportion_z(Counts(30, 12), Counts(270, 90)) == pytest.approx(0.73, abs=0.01)
    assert two_proportion_z(Counts(10, 0), Counts(10, 0)) == 0.0


def test_a_group_that_does_much_better_than_the_rest_is_found_with_its_numbers() -> None:
    observations = _batch("warm", "High", total=40, deposited=30) + _batch(
        "plain", "High", total=160, deposited=20
    )

    (found,) = find_patterns(observations, [], RULES)

    assert found.features == (("angle", "warm"), ("risk_band", "High"))
    assert (found.direction, found.outcome, found.compared_with) == (
        "better",
        "deposited",
        "other_messages",
    )
    assert (found.sent_count, found.comparison_count) == (40, 160)
    assert (found.outcome_percent, found.comparison_percent, found.gap_points) == (75.0, 12.5, 62.5)


def test_a_group_that_does_worse_than_clients_left_alone_is_found() -> None:
    observations = _batch("quiet", "Low", total=40, deposited=4, group="quiet_group") + _batch(
        "loud", "Low", total=60, deposited=6, group="loud_group"
    )
    left_alone = [NotMessaged("quiet_group", deposited=index < 40) for index in range(100)]

    (found,) = find_patterns(observations, left_alone, RULES)

    assert found.features == (("angle", "quiet"), ("risk_band", "Low"))
    assert (found.direction, found.outcome, found.compared_with) == (
        "worse",
        "deposited",
        NOT_MESSAGED,
    )
    assert (found.sent_count, found.comparison_count) == (40, 100)
    assert (found.outcome_percent, found.comparison_percent, found.gap_points) == (
        10.0,
        40.0,
        -30.0,
    )


def test_a_small_group_or_a_gap_chance_could_explain_is_not_reported() -> None:
    tiny_but_perfect = _batch("tiny", "High", total=11, deposited=11) + _batch(
        "big", "High", total=200, deposited=0
    )
    assert find_patterns(tiny_but_perfect, [], RULES) == []
    smaller_minimum = PatternRules(10, 5.0, 3.0, 3, 10)
    assert [p.sent_count for p in find_patterns(tiny_but_perfect, [], smaller_minimum)] == [11]

    gap_within_chance = _batch("a", "High", total=30, deposited=12) + _batch(
        "b", "High", total=270, deposited=90
    )
    assert find_patterns(gap_within_chance, [], RULES) == []


def test_the_same_clients_are_described_only_once() -> None:
    three_features = (("angle", "warm"), ("priority_tier", "T1"), ("risk_band", "High"))
    others = (("angle", "plain"), ("priority_tier", "T2"), ("risk_band", "Low"))
    observations = [Observation(three_features, "g", False, index < 30) for index in range(40)] + [
        Observation(others, "g", False, index < 20) for index in range(160)
    ]

    found = [p for p in find_patterns(observations, [], RULES) if p.outcome == "deposited"]

    assert [p.features for p in found] == [(("angle", "warm"), ("priority_tier", "T1"))]


def _purge(session) -> None:
    insight_ids = list(
        session.scalars(
            select(ObservedPattern.insight_id).where(ObservedPattern.insight_id.is_not(None))
        )
    )
    session.execute(delete(ObservedPattern))
    session.execute(delete(AgentInsightFact).where(AgentInsightFact.insight_id.in_(insight_ids)))
    session.execute(delete(AgentInsight).where(AgentInsight.insight_id.in_(insight_ids)))
    session.execute(delete(AgentInsight).where(AgentInsight.title == "pattern test insight"))
    session.execute(delete(ActionResult).where(ActionResult.client_id.in_(EVERY_CLIENT)))
    session.execute(
        delete(TouchLog).where(
            TouchLog.enrollment_id.in_(
                select(Enrollment.enrollment_id).where(Enrollment.client_id.in_(EVERY_CLIENT))
            )
        )
    )
    session.execute(delete(Enrollment).where(Enrollment.client_id.in_(EVERY_CLIENT)))
    session.execute(delete(OutreachMessage).where(OutreachMessage.client_id.in_(EVERY_CLIENT)))
    session.execute(delete(GenerationRun).where(GenerationRun.client_id.in_(EVERY_CLIENT)))
    proposals = select(AgentProposal.proposal_id).where(
        AgentProposal.group_name.in_([GOOD_GROUP, QUIET_GROUP])
    )
    session.execute(
        delete(AgentProposalClient).where(AgentProposalClient.proposal_id.in_(proposals))
    )
    session.execute(
        delete(AgentProposal).where(AgentProposal.group_name.in_([GOOD_GROUP, QUIET_GROUP]))
    )
    session.execute(delete(ActiveTransaction).where(ActiveTransaction.unit_fund_id == FUND_ID))
    session.execute(delete(ActiveClientFund).where(ActiveClientFund.unit_fund_id == FUND_ID))
    session.execute(delete(Campaign).where(Campaign.name == CAMPAIGN_NAME))
    session.execute(delete(Clients).where(Clients.client_id.in_(EVERY_CLIENT)))
    session.execute(delete(Funds).where(Funds.unit_fund_id == FUND_ID))
    session.execute(delete(AuditLog).where(AuditLog.entity_type == "observed_pattern"))
    session.commit()


def _message(session, campaign_id: int, client_id: int, tier: str) -> str:
    session.add(
        Clients(
            client_id=client_id, unit_fund_id=FUND_ID, n_purchases_returned=0, n_sales_returned=0
        )
    )
    session.commit()
    state = accepted_state(client_id)
    state["priority_tier"] = tier
    generation = persist_generation_run(session, state, make_settings())
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
    return message.message_id


def _proposal(
    session, campaign_id: int | None, action_code: str, group: str, created_at: datetime
) -> int:
    proposal = AgentProposal(
        action_code=action_code,
        catalog_version=1,
        group_name=group,
        client_count=1,
        evidence="evidence",
        reason="reason",
        angle=GOOD_ANGLE if action_code == GOOD_ACTION else QUIET_ANGLE,
        content_mix="balanced",
        permission_applied="approve_each",
        response_kind="automated_email",
        status="sent",
        campaign_id=campaign_id,
        created_at=created_at,
    )
    session.add(proposal)
    session.commit()
    return proposal.proposal_id


def _measured(
    session,
    campaign_id: int,
    proposal_id: int,
    client_id: int,
    tier: str,
    *,
    replied: bool,
    deposited: bool,
) -> None:
    message_id = _message(session, campaign_id, client_id, tier)
    session.add(
        ActionResult(
            proposal_id=proposal_id,
            client_id=client_id,
            unit_fund_id=FUND_ID,
            message_id=message_id,
            sent_at=SENT_AT,
            window_days=WINDOW,
            replied=replied,
            deposited=deposited,
            deposit_amount_kes=1000.0 if deposited else 0.0,
            measured_at=NOW,
        )
    )
    session.commit()


def _left_out(session, proposal_id: int, client_id: int, reason: str) -> None:
    session.add(
        AgentProposalClient(
            proposal_id=proposal_id,
            client_id=client_id,
            unit_fund_id=FUND_ID,
            included=False,
            skip_reason=reason,
        )
    )
    session.commit()


def _deposit(session, txn_id: int, client_id: int) -> None:
    session.add(
        ActiveTransaction(
            txn_id=txn_id,
            txn_type="purchase",
            client_id=client_id,
            unit_fund_id=FUND_ID,
            txn_date=DEPOSIT_DAY,
            amount=1000.0,
        )
    )


def _build_scenario(session) -> None:
    session.add(Funds(unit_fund_id=FUND_ID, unit_fund_name="A pattern search test fund"))
    campaign = Campaign(name=CAMPAIGN_NAME, status="running")
    session.add(campaign)
    session.commit()
    campaign_id = campaign.campaign_id

    good = _proposal(session, campaign_id, GOOD_ACTION, GOOD_GROUP, SENT_AT)
    quiet = _proposal(session, campaign_id, QUIET_ACTION, QUIET_GROUP, SENT_AT)
    for index, client_id in enumerate(GOOD_CLIENTS):
        _measured(
            session, campaign_id, good, client_id, GOOD_TIER, replied=index < 4, deposited=index < 5
        )
    for client_id in QUIET_CLIENTS:
        _measured(
            session, campaign_id, quiet, client_id, QUIET_TIER, replied=False, deposited=False
        )

    idle = _proposal(session, None, "do_nothing", QUIET_GROUP, LEFT_OUT_AT)
    idle_again = _proposal(session, None, "do_nothing", QUIET_GROUP, LEFT_OUT_AGAIN_AT)
    for client_id in LEFT_OUT_CLIENTS:
        _left_out(session, idle, client_id, "not_selected_tonight")
    _left_out(session, idle_again, LEFT_OUT_CLIENTS[0], "no_action_decided")
    _left_out(session, idle, DO_NOT_CONTACT_CLIENT, "on_do_not_contact_list")
    _left_out(session, idle, MESSAGED_CLIENT, "not_selected_tonight")

    sent_message = _message(session, campaign_id, MESSAGED_CLIENT, QUIET_TIER)
    enrollment = Enrollment(campaign_id=campaign_id, client_id=MESSAGED_CLIENT)
    session.add(enrollment)
    session.commit()
    session.add(
        TouchLog(
            enrollment_id=enrollment.enrollment_id,
            step_no=1,
            message_id=sent_message,
            sent_at=LEFT_OUT_AT,
            delivery_status="sent",
        )
    )
    session.add(
        ActiveClientFund(
            client_id=LOADED_CLIENT, unit_fund_id=FUND_ID, n_deposits=0, n_withdrawals=0
        )
    )
    for offset, client_id in enumerate((*LEFT_OUT_CLIENTS[:5], DO_NOT_CONTACT_CLIENT)):
        _deposit(session, 99770000 + offset, client_id)
    _deposit(session, 99770010, MESSAGED_CLIENT)
    session.commit()


@pytest.fixture
def pattern_settings(monkeypatch):
    monkeypatch.setenv("PATTERN_MIN_GROUP_SIZE", "5")
    monkeypatch.setenv("PATTERN_MIN_Z", "2")
    get_settings.cache_clear()
    yield
    monkeypatch.undo()
    get_settings.cache_clear()


@pytest.fixture
def scenario(db: None, pattern_settings):
    with SessionLocal() as session:
        _purge(session)
        _build_scenario(session)
    yield
    with SessionLocal() as session:
        _purge(session)


def _saved(session) -> dict[tuple[str, str, str], ObservedPattern]:
    rows = session.scalars(select(ObservedPattern)).all()
    return {(row.direction, row.outcome, row.compared_with): row for row in rows}


def test_the_search_finds_both_directions_from_stored_results(scenario) -> None:
    with SessionLocal() as session:
        result = run_pattern_search(session, now=NOW, window_days=WINDOW)
        saved = _saved(session)

    assert (result.measured_messages, result.not_messaged_clients) == (12, 6)
    assert (result.found, result.new) == (3, 3)
    assert set(saved) == {
        ("better", "deposited", "other_messages"),
        ("better", "replied", "other_messages"),
        ("worse", "deposited", "not_messaged"),
    }
    worse = saved[("worse", "deposited", "not_messaged")]
    assert worse.features == {"action_code": QUIET_ACTION, "angle": QUIET_ANGLE}
    assert (worse.sent_count, worse.comparison_count) == (6, 6)
    assert (worse.outcome_percent, worse.comparison_percent, worse.gap_points) == (0.0, 83.3, -83.3)
    assert worse.status == "new"


def test_a_second_search_keeps_what_a_person_decided(scenario) -> None:
    with SessionLocal() as session:
        run_pattern_search(session, now=NOW, window_days=WINDOW)
        pattern = _saved(session)[("worse", "deposited", "not_messaged")]
        pattern.status = "worth_a_group_filter"
        pattern.reviewed_by = "reviewer-1"
        pattern.reviewed_at = NOW
        session.commit()

        again = run_pattern_search(session, now=NOW, window_days=WINDOW)
        reviewed = _saved(session)[("worse", "deposited", "not_messaged")]

    assert (again.found, again.new) == (3, 0)
    assert (reviewed.status, reviewed.reviewed_by) == ("worth_a_group_filter", "reviewer-1")


class WriterClient:
    model = "fake-writer"

    def converse(self, *, system, messages, tools=()):
        answered = sum(1 for message in messages if message["role"] == "assistant")
        if answered == 0:
            return self._call(READ_PATTERN, {})
        if answered == 1:
            return self._call(
                WRITE_PATTERN_INSIGHT,
                {
                    "title": "pattern test insight",
                    "suggestion": "Look at who this group is made of.",
                    "why_now": "The gap has held for a month.",
                    "confidence": "medium",
                    "confidence_reason": "Only a few messages sit behind it.",
                },
            )
        return ConversationTurn(
            text="Done.", tool_requests=(), usage=_USAGE, stop_reason="end_turn"
        )

    @staticmethod
    def _call(name: str, tool_input: dict) -> ConversationTurn:
        request = ToolUseRequest(call_id=f"call_{name}", tool_name=name, tool_input=tool_input)
        return ConversationTurn(
            text="", tool_requests=(request,), usage=_USAGE, stop_reason="tool_use"
        )


def test_the_model_writes_each_pattern_up_as_an_insight_with_its_numbers(scenario) -> None:
    with SessionLocal() as session:
        run_pattern_search(session, now=NOW, window_days=WINDOW)
        first = write_up_patterns(session, WriterClient(), seen_at=NOW)
        second = write_up_patterns(session, WriterClient(), seen_at=NOW)
        patterns = list(session.scalars(select(ObservedPattern)))
        insights = [session.get(AgentInsight, row.insight_id) for row in patterns]
        facts = list(
            session.scalars(
                select(AgentInsightFact).where(
                    AgentInsightFact.insight_id == insights[0].insight_id
                )
            )
        )

    assert (first.written, first.failed) == (3, 0)
    assert (second.written, second.failed) == (0, 0)
    assert {insight.kind for insight in insights} == {"pattern"}
    assert {insight.state for insight in insights} == {"new"}
    assert [pattern.sent_count for pattern in patterns] == [i.client_count for i in insights]
    assert len(facts) == 5
    assert all(fact.source_filter["features"] for fact in facts)


def test_accepting_a_pattern_never_starts_an_action(db: None) -> None:
    with SessionLocal() as session:
        _purge(session)
        insight = AgentInsight(
            kind="pattern",
            title="pattern test insight",
            group_name="angle warm",
            client_count=40,
            confidence="low",
            confidence_reason="few messages",
            suggestion="look into it",
            why_now="now",
            state="accepted",
        )
        session.add(insight)
        session.commit()

        with pytest.raises(InsightNotActionable):
            start_action_run(session, insight.insight_id)
        _purge(session)


@pytest.fixture
def stored_pattern(db: None, configured_reviewers, reviewer_1_headers):
    client.headers.update(reviewer_1_headers)
    with SessionLocal() as session:
        _purge(session)
        pattern = ObservedPattern(
            pattern_key="pattern_test_endpoint",
            description="angle warm, risk band High",
            features={"angle": "warm", "risk_band": "High"},
            outcome="deposited",
            compared_with="other_messages",
            direction="better",
            window_days=WINDOW,
            outcome_percent=75.0,
            comparison_percent=12.5,
            gap_points=62.5,
            sent_count=40,
            comparison_count=160,
        )
        session.add(pattern)
        session.commit()
        pattern_id = pattern.pattern_id
    yield pattern_id
    client.headers.pop("Authorization", None)
    with SessionLocal() as session:
        _purge(session)


def test_a_person_can_mark_a_pattern_worth_a_group_filter(stored_pattern: int) -> None:
    listed = client.get(PATTERNS, params={"status": "new"}).json()
    assert [item["pattern_id"] for item in listed["items"]] == [stored_pattern]
    assert listed["items"][0]["gap_points"] == 62.5

    reviewed = client.post(
        f"{PATTERNS}/{stored_pattern}/review", json={"status": "worth_a_group_filter"}
    )

    assert reviewed.status_code == 200
    body = reviewed.json()
    assert body["status"] == "worth_a_group_filter"
    assert body["reviewed_by"]
    assert client.get(f"{PATTERNS}/counts").json()["counts_by_status"] == {
        "worth_a_group_filter": 1
    }
    assert (
        client.post(f"{PATTERNS}/{stored_pattern}/review", json={"status": "new"}).status_code
        == 422
    )
    assert client.post(f"{PATTERNS}/0/review", json={"status": "set_aside"}).status_code == 404
