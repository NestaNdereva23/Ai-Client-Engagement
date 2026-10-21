from __future__ import annotations

import json
from datetime import date

import pytest
from sqlalchemy import select

from app.agents.angle_brief import (
    AngleBriefRejected,
    angle_for_proposal,
    generate_angle_brief,
)
from app.config import get_settings
from app.db.models.agent import AgentActionCatalog
from app.db.models.audit import AuditLog
from app.db.models.rules import MessageAngleCatalog
from app.db.session import SessionLocal
from app.llmops.tracing import NullTracer
from app.privacy.fact_block import ProposalFactBlock
from app.rules.catalog import load_angle

AS_OF = date(2026, 9, 20)


def _item(**changes) -> dict:
    base = {
        "headline": "Check in on the group",
        "who": "Clients in this situation",
        "claim": "The team has noticed they may want an update",
        "ask": "Ask them to share what matters most right now",
        "never": "Never name a specific figure",
        "use": "Not used in this test",
    }
    return {**base, **changes}


def _reply(**changes) -> str:
    return json.dumps(_item(**changes))


class _ScriptedLLM:
    model = "angle-brief-stub"

    def __init__(self, *replies: str) -> None:
        self.replies = list(replies)
        self.seen: list[str] = []

    def generate(self, *, system: str, user: str) -> str:
        self.seen.extend([system, user])
        return self.replies.pop(0)


def _facts() -> ProposalFactBlock:
    return ProposalFactBlock(
        group_name="deposit_size_falling",
        action_code="test_action",
        action_title="Test action",
        action_who="Test clients",
        client_count=47,
        money_total_kes=12_000_000,
        evidence="47 clients, KES 12,000,000 held",
    )


def _action(message_angle: str | None = "back_on_schedule") -> AgentActionCatalog:
    return AgentActionCatalog(
        action_code="test_action",
        title="Test action",
        who="Test clients",
        message_angle=message_angle,
    )


def test_a_good_reply_becomes_a_brief() -> None:
    llm = _ScriptedLLM(_reply())
    brief = generate_angle_brief(
        _facts(),
        llm,
        entity_id="1",
        audit=lambda record: None,
        instructions="INSTRUCTIONS",
        tracer=NullTracer(),
        trace_id="test-trace",
    )
    assert brief.headline == "Check in on the group"
    assert brief.never == "Never name a specific figure"


def test_a_missing_field_is_retried_and_told_why() -> None:
    bad = json.dumps({k: v for k, v in _item().items() if k != "ask"})
    llm = _ScriptedLLM(bad, _reply())
    brief = generate_angle_brief(
        _facts(),
        llm,
        entity_id="1",
        audit=lambda record: None,
        instructions="INSTRUCTIONS",
        tracer=NullTracer(),
        trace_id="test-trace",
    )
    assert brief.ask == "Ask them to share what matters most right now"
    assert "REFUSED" in llm.seen[2]
    assert "ask" in llm.seen[2]


def test_an_off_limits_word_in_claim_is_rejected() -> None:
    bad = _reply(claim="Their balance has been getting smaller")
    llm = _ScriptedLLM(bad, _reply())
    generate_angle_brief(
        _facts(),
        llm,
        entity_id="1",
        audit=lambda record: None,
        instructions="INSTRUCTIONS",
        tracer=NullTracer(),
        trace_id="test-trace",
    )
    assert "balance" in llm.seen[2]


def test_avoidance_phrasing_in_ask_is_rejected() -> None:
    bad = _reply(ask="Ask them to share an update, without mentioning numbers")
    llm = _ScriptedLLM(bad, _reply())
    generate_angle_brief(
        _facts(),
        llm,
        entity_id="1",
        audit=lambda record: None,
        instructions="INSTRUCTIONS",
        tracer=NullTracer(),
        trace_id="test-trace",
    )
    assert "without" in llm.seen[2]


def test_the_never_field_may_use_the_word_never() -> None:
    llm = _ScriptedLLM(_reply(never="Never state an exact figure"))
    brief = generate_angle_brief(
        _facts(),
        llm,
        entity_id="1",
        audit=lambda record: None,
        instructions="INSTRUCTIONS",
        tracer=NullTracer(),
        trace_id="test-trace",
    )
    assert brief.never == "Never state an exact figure"


def test_three_bad_replies_exhaust_retries() -> None:
    bad = _reply(claim="Their balance has been getting smaller")
    llm = _ScriptedLLM(bad, bad, bad)
    with pytest.raises(AngleBriefRejected):
        generate_angle_brief(
            _facts(),
            llm,
            entity_id="1",
            audit=lambda record: None,
            instructions="INSTRUCTIONS",
            tracer=NullTracer(),
            trace_id="test-trace",
        )
    assert llm.replies == []


def test_angle_for_proposal_returns_none_for_a_no_message_action(db: None) -> None:
    with SessionLocal() as session:
        result = angle_for_proposal(
            session,
            action=_action(message_angle=None),
            group_name="deposit_size_falling",
            client_count=1,
            money_total_kes=None,
            evidence="x",
            evidence_detail=None,
            run_id=999001,
            as_of=AS_OF,
            llm_client=_ScriptedLLM(),
            audit=lambda record: None,
            tracer=NullTracer(),
            trace_id="test-trace",
        )
    assert result is None


def test_angle_for_proposal_is_a_no_op_when_the_flag_is_off(db: None, monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "agent_angle_generation_enabled", False)
    with SessionLocal() as session:
        before = session.scalar(select(AuditLog).order_by(AuditLog.log_id.desc()).limit(1))
        before_id = before.log_id if before else 0
        result = angle_for_proposal(
            session,
            action=_action(),
            group_name="deposit_size_falling",
            client_count=1,
            money_total_kes=None,
            evidence="x",
            evidence_detail=None,
            run_id=999002,
            as_of=AS_OF,
            llm_client=_ScriptedLLM(),
            audit=lambda record: None,
            tracer=NullTracer(),
            trace_id="test-trace",
        )
        session.commit()
        after = session.scalar(select(AuditLog).order_by(AuditLog.log_id.desc()).limit(1))
        after_id = after.log_id if after else 0
    assert result == "back_on_schedule"
    assert after_id == before_id


@pytest.fixture
def generated_run_angle():
    versions: list[tuple[str, int]] = []

    def register(action_code: str, group_name: str, run_id: int) -> str:
        slug = f"{action_code}__{group_name}__run{run_id}"
        versions.append((slug, run_id))
        return slug

    yield register
    if not versions:
        return
    from sqlalchemy import delete

    from app.db.models.prompt_config import ActiveConfiguration

    with SessionLocal() as session:
        for slug, _run_id in versions:
            session.execute(delete(MessageAngleCatalog).where(MessageAngleCatalog.angle == slug))
            session.execute(
                delete(ActiveConfiguration).where(
                    ActiveConfiguration.component_type == "message_angle_catalog",
                    ActiveConfiguration.component_key == slug,
                )
            )
        session.commit()


def test_angle_for_proposal_generates_and_publishes_a_brief(
    db: None, monkeypatch, generated_run_angle
) -> None:
    monkeypatch.setattr(get_settings(), "agent_angle_generation_enabled", True)
    run_id = 999003
    with SessionLocal() as session:
        fallback = load_angle(session, "back_on_schedule", AS_OF)
        assert fallback is not None
        fallback_cta = fallback.cta

        slug = angle_for_proposal(
            session,
            action=_action(),
            group_name="deposit_size_falling",
            client_count=47,
            money_total_kes=12_000_000,
            evidence="47 clients, KES 12,000,000 held",
            evidence_detail={"client_count": 47},
            run_id=run_id,
            as_of=AS_OF,
            llm_client=_ScriptedLLM(_reply()),
            audit=lambda record: None,
            tracer=NullTracer(),
            trace_id="test-trace",
        )
        session.commit()
    generated_run_angle("test_action", "deposit_size_falling", run_id)

    assert slug == f"test_action__deposit_size_falling__run{run_id}"
    with SessionLocal() as session:
        row = load_angle(session, slug, AS_OF)
    assert row is not None
    assert row.headline == "Check in on the group"
    assert row.cta == fallback_cta


def test_angle_for_proposal_falls_back_and_audits_on_rejection(db: None, monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "agent_angle_generation_enabled", True)
    bad = _reply(claim="Their balance has been getting smaller")
    run_id = 999004
    with SessionLocal() as session:
        slug = angle_for_proposal(
            session,
            action=_action(),
            group_name="deposit_size_falling",
            client_count=1,
            money_total_kes=None,
            evidence="x",
            evidence_detail=None,
            run_id=run_id,
            as_of=AS_OF,
            llm_client=_ScriptedLLM(bad, bad, bad),
            audit=lambda record: None,
            tracer=NullTracer(),
            trace_id="test-trace",
        )
        session.commit()
        fallback_audit = session.scalar(
            select(AuditLog)
            .where(
                AuditLog.entity_type == "agent_proposal_angle",
                AuditLog.run_id == str(run_id),
            )
            .order_by(AuditLog.log_id.desc())
            .limit(1)
        )
    assert slug == "back_on_schedule"
    assert fallback_audit is not None
    assert fallback_audit.detail["action_code"] == "test_action"
