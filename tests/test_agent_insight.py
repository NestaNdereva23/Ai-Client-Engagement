"""The insight tables themselves: a finding reads back whole, and a fact
cannot be written without the filter it was counted from.

client_id and unit_fund_id on agent_insight_client are plain numbers with no
foreign key, the same choice already made for agent_proposal_client, because
a finding can be about either the dormant book or the active book.
"""

from __future__ import annotations

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from app.db.models.agent_insight import AgentInsight, AgentInsightClient, AgentInsightFact
from app.db.models.agent_proposal import AgentProposal
from app.db.models.agent_run import AgentRun
from app.db.session import SessionLocal

_FUND_ID = 993
_CLIENT_A = 99301
_CLIENT_B = 99302


def _new_insight(**overrides) -> AgentInsight:
    values = {
        "kind": "risk",
        "title": "twelve big clients stopped topping up",
        "group_name": "test insight group",
        "client_count": 12,
        "money_total_kes": 4_500_000.0,
        "confidence": "high",
        "confidence_reason": "every client in the group shows the same three signs",
        "suggestion": "call them before the end of the month",
        "why_now": "the drop started this quarter and is still going",
    }
    values.update(overrides)
    return AgentInsight(**values)


@pytest.fixture
def insight(db: None):
    with SessionLocal() as session:
        row = _new_insight()
        session.add(row)
        session.commit()
        insight_id = row.insight_id

    yield insight_id

    with SessionLocal() as session:
        session.execute(delete(AgentInsight).where(AgentInsight.insight_id == insight_id))
        session.commit()


def test_an_insight_can_be_created_by_hand(insight: int) -> None:
    with SessionLocal() as session:
        row = session.get(AgentInsight, insight)
    assert row.state == "new"
    assert row.dismissed_reason is None
    assert row.decided_by is None
    assert row.run_id is None


def test_an_insight_reads_back_whole_with_its_facts_and_clients(insight: int) -> None:
    with SessionLocal() as session:
        session.add_all(
            [
                AgentInsightFact(
                    insight_id=insight,
                    fact_text="clients in the group who stopped topping up",
                    fact_value="12",
                    source_filter={"risk_band": "high", "months_since_top_up": {"gte": 3}},
                    source_table="client_risk_features",
                ),
                AgentInsightFact(
                    insight_id=insight,
                    fact_text="money the group holds",
                    fact_value="4500000",
                    source_filter={"risk_band": "high"},
                    source_table="active_client_fund",
                ),
            ]
        )
        session.add_all(
            [
                AgentInsightClient(insight_id=insight, client_id=_CLIENT_A, unit_fund_id=_FUND_ID),
                AgentInsightClient(insight_id=insight, client_id=_CLIENT_B, unit_fund_id=_FUND_ID),
            ]
        )
        session.commit()

    with SessionLocal() as session:
        row = session.get(AgentInsight, insight)
        facts = (
            session.execute(select(AgentInsightFact).where(AgentInsightFact.insight_id == insight))
            .scalars()
            .all()
        )
        clients = (
            session.execute(
                select(AgentInsightClient).where(AgentInsightClient.insight_id == insight)
            )
            .scalars()
            .all()
        )

    assert row.title == "twelve big clients stopped topping up"
    assert row.confidence == "high"
    assert len(facts) == 2
    assert {fact.source_table for fact in facts} == {
        "client_risk_features",
        "active_client_fund",
    }
    assert all(fact.source_filter for fact in facts)
    assert {client.client_id for client in clients} == {_CLIENT_A, _CLIENT_B}


def test_a_fact_with_no_source_filter_is_refused(insight: int) -> None:
    with SessionLocal() as session:
        session.add(
            AgentInsightFact(
                insight_id=insight,
                fact_text="clients in the group",
                fact_value="12",
                source_filter={},
                source_table="client_risk_features",
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()


def test_a_fact_with_a_blank_source_table_is_refused(insight: int) -> None:
    with SessionLocal() as session:
        session.add(
            AgentInsightFact(
                insight_id=insight,
                fact_text="clients in the group",
                fact_value="12",
                source_filter={"risk_band": "high"},
                source_table="   ",
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()


def test_the_same_client_fund_cannot_be_listed_twice(insight: int) -> None:
    with SessionLocal() as session:
        session.add_all(
            [
                AgentInsightClient(insight_id=insight, client_id=_CLIENT_A, unit_fund_id=_FUND_ID),
                AgentInsightClient(insight_id=insight, client_id=_CLIENT_A, unit_fund_id=_FUND_ID),
            ]
        )
        with pytest.raises(IntegrityError):
            session.commit()


def test_a_kind_outside_the_six_is_refused(db: None) -> None:
    with SessionLocal() as session:
        session.add(_new_insight(kind="hunch"))
        with pytest.raises(IntegrityError):
            session.commit()


def test_every_one_of_the_six_kinds_is_accepted(db: None) -> None:
    kinds = ("risk", "opportunity", "lifecycle_change", "anomaly", "pattern", "campaign")
    with SessionLocal() as session:
        rows = [_new_insight(kind=kind) for kind in kinds]
        session.add_all(rows)
        session.commit()
        insight_ids = [row.insight_id for row in rows]

    with SessionLocal() as session:
        stored = (
            session.execute(
                select(AgentInsight.kind).where(AgentInsight.insight_id.in_(insight_ids))
            )
            .scalars()
            .all()
        )
        assert set(stored) == set(kinds)
        session.execute(delete(AgentInsight).where(AgentInsight.insight_id.in_(insight_ids)))
        session.commit()


def test_dismissed_without_a_reason_is_refused(db: None) -> None:
    with SessionLocal() as session:
        session.add(_new_insight(state="dismissed"))
        with pytest.raises(IntegrityError):
            session.commit()


def test_an_insight_can_carry_the_run_it_came_from(db: None) -> None:
    with SessionLocal() as session:
        run = AgentRun(trigger="manual")
        session.add(run)
        session.commit()
        run_id = run.run_id

        row = _new_insight(run_id=run_id)
        session.add(row)
        session.commit()
        insight_id = row.insight_id

    with SessionLocal() as session:
        assert session.get(AgentInsight, insight_id).run_id == run_id
        session.execute(delete(AgentInsight).where(AgentInsight.insight_id == insight_id))
        session.commit()
        session.execute(delete(AgentRun).where(AgentRun.run_id == run_id))
        session.commit()


def test_a_proposal_points_at_the_finding_behind_it(insight: int) -> None:
    with SessionLocal() as session:
        proposal = AgentProposal(
            action_code="start_win_back",
            catalog_version=1,
            group_name="test insight group",
            client_count=12,
            evidence="twelve clients stopped topping up",
            reason="win them back before they leave",
            permission_applied="suggest_only",
            insight_id=insight,
        )
        session.add(proposal)
        session.commit()
        proposal_id = proposal.proposal_id

    with SessionLocal() as session:
        assert session.get(AgentProposal, proposal_id).insight_id == insight
        session.execute(delete(AgentProposal).where(AgentProposal.proposal_id == proposal_id))
        session.commit()


def test_deleting_an_insight_takes_its_facts_and_clients_with_it(db: None) -> None:
    with SessionLocal() as session:
        row = _new_insight()
        session.add(row)
        session.commit()
        insight_id = row.insight_id

        session.add(
            AgentInsightFact(
                insight_id=insight_id,
                fact_text="clients in the group",
                fact_value="12",
                source_filter={"risk_band": "high"},
                source_table="client_risk_features",
            )
        )
        session.add(
            AgentInsightClient(insight_id=insight_id, client_id=_CLIENT_A, unit_fund_id=_FUND_ID)
        )
        session.commit()

        session.execute(delete(AgentInsight).where(AgentInsight.insight_id == insight_id))
        session.commit()

        facts = session.execute(
            select(AgentInsightFact).where(AgentInsightFact.insight_id == insight_id)
        ).all()
        clients = session.execute(
            select(AgentInsightClient).where(AgentInsightClient.insight_id == insight_id)
        ).all()
    assert facts == []
    assert clients == []
