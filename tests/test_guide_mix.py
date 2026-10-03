from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import delete, select

from app.agents import write_tools
from app.agents.action_catalog import load_action
from app.agents.email_agent import build_system_prompt
from app.agents.guide_mix import (
    MIX_INSTRUCTIONS,
    GuideBrief,
    guide_brief_for_campaign,
    mix_instruction,
)
from app.db.models.agent import CONTENT_MIXES
from app.db.models.agent_proposal import AgentProposal
from app.db.models.audit import AuditLog
from app.db.models.outreach import Campaign
from app.db.models.rag import DOC_TYPE_CLIENT_GUIDE, RagChunk, RagDocument, RagDocumentVersion
from app.db.session import SessionLocal
from app.rag.embedding import HashingEmbedder
from app.rag.guides import add_guide, approve_guide_version
from app.rag.retrieve import Retrieved

EMB = HashingEmbedder()
AS_OF = date(2026, 9, 20)
ACTION = "welcome_and_top_up"
GROUP = "guide mix test group"
TITLE_PREFIX = "Guide mix test: "
GUIDE_TITLE = f"{TITLE_PREFIX}small regular deposits"
OTHER_GUIDE_TITLE = f"{TITLE_PREFIX}a guide for another action"


def test_every_content_mix_has_an_instruction_for_the_brief() -> None:
    assert set(MIX_INSTRUCTIONS) == set(CONTENT_MIXES)
    assert mix_instruction(None) is None
    assert mix_instruction("chatty") is None


def test_the_mix_instruction_and_the_guide_reach_the_prompt() -> None:
    guide = Retrieved(
        chunk_id=7,
        text="Small deposits made regularly are easier to keep up.",
        metadata={},
        score=1.0,
        version_id=1,
        doc_type=DOC_TYPE_CLIENT_GUIDE,
    )

    prompt = build_system_prompt(
        angle=None,
        prompt_variant=None,
        chunks=[guide],
        mix_instruction=mix_instruction("balanced"),
    )

    assert "How much to explain and how much to ask" in prompt
    assert "about equal parts" in prompt
    assert guide.text in prompt.split("Facts you may cite")[1]


def _purge() -> None:
    with SessionLocal() as session:
        proposals = session.scalars(
            select(AgentProposal).where(AgentProposal.group_name == GROUP)
        ).all()
        campaign_ids = [p.campaign_id for p in proposals if p.campaign_id is not None]
        session.execute(delete(AgentProposal).where(AgentProposal.group_name == GROUP))
        session.execute(delete(Campaign).where(Campaign.campaign_id.in_(campaign_ids)))
        doc_ids = session.scalars(
            select(RagDocument.doc_id).where(RagDocument.title.like(f"{TITLE_PREFIX}%"))
        ).all()
        version_ids = session.scalars(
            select(RagDocumentVersion.version_id).where(RagDocumentVersion.doc_id.in_(doc_ids))
        ).all()
        session.execute(
            delete(AuditLog).where(
                AuditLog.entity_type == "rag_document_version",
                AuditLog.entity_id.in_([str(v) for v in version_ids]),
            )
        )
        session.execute(delete(RagChunk).where(RagChunk.version_id.in_(version_ids)))
        session.execute(delete(RagDocumentVersion).where(RagDocumentVersion.doc_id.in_(doc_ids)))
        session.execute(delete(RagDocument).where(RagDocument.doc_id.in_(doc_ids)))
        session.commit()


@pytest.fixture
def clean(db: None):
    _purge()
    yield
    _purge()


def _proposal(session, *, content_mix: str | None) -> int:
    campaign = Campaign(
        name="Guide mix test campaign",
        campaign_type="agent_proposal",
        status="running",
        start_date=AS_OF,
    )
    session.add(campaign)
    session.flush()
    session.add(
        AgentProposal(
            action_code=ACTION,
            catalog_version=1,
            group_name=GROUP,
            client_count=1,
            evidence="evidence",
            reason="reason",
            permission_applied="approve_each",
            content_mix=content_mix,
            campaign_id=campaign.campaign_id,
        )
    )
    session.commit()
    return campaign.campaign_id


def test_a_campaign_is_drafted_with_its_proposals_mix_and_the_guide_made_for_its_action(
    clean,
) -> None:
    with SessionLocal() as session:
        action = load_action(session, ACTION, AS_OF)
        made_for_it = add_guide(
            session,
            title=GUIDE_TITLE,
            topic="Saving habit",
            text="Small deposits made regularly work well.",
            action_codes=[ACTION],
            created_by="writer",
            embedder=EMB,
        )
        closer_but_for_another_action = add_guide(
            session,
            title=OTHER_GUIDE_TITLE,
            topic="Fees",
            text=f"{action.title}. {action.who}.",
            action_codes=["fee_warning"],
            created_by="writer",
            embedder=EMB,
        )
        for guide in (made_for_it, closer_but_for_another_action):
            approve_guide_version(session, guide.version_id, approved_by="lead")
        campaign_id = _proposal(session, content_mix="mostly_learning")
        brief = guide_brief_for_campaign(session, campaign_id, at=AS_OF)

    assert "mostly explains" in brief.mix_instruction
    assert [hit.text for hit in brief.guides] == [made_for_it.text]


def test_a_campaign_with_no_mix_is_drafted_as_before(clean) -> None:
    with SessionLocal() as session:
        no_mix = _proposal(session, content_mix=None)
        brief = guide_brief_for_campaign(session, no_mix, at=AS_OF)
        unknown = guide_brief_for_campaign(session, 999999999, at=AS_OF)

    assert brief == GuideBrief()
    assert unknown == GuideBrief()


def test_the_drafting_step_hands_the_mix_and_the_guide_to_the_prompt(monkeypatch) -> None:
    guide = Retrieved(
        chunk_id=7,
        text="Small deposits made regularly are easier to keep up.",
        metadata={},
        score=1.0,
        version_id=1,
        doc_type=DOC_TYPE_CLIENT_GUIDE,
    )
    brief = GuideBrief(mix_instruction=mix_instruction("mostly_ask"), guides=(guide,))
    captured: dict = {}

    def fake_orchestrator(session, settings, **kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(
        write_tools, "guide_brief_for_campaign", lambda session, campaign_id, variant=None: brief
    )
    monkeypatch.setattr(write_tools, "load_split_for_campaign", lambda session, campaign_id: None)
    monkeypatch.setattr(write_tools, "build_default_orchestrator", fake_orchestrator)
    monkeypatch.setattr(write_tools, "run_due_enrollments", lambda *args, **kwargs: [])

    write_tools.draft_into_review_queue(None, campaign_id=1, prohibitions=("Never say X.",))

    prompt = captured["prompt_builder"](
        angle=None, prompt_variant=None, chunks=captured["extra_chunks"]
    )
    assert "Lead with the ask" in prompt
    assert guide.text in prompt
    assert "Never say X." in prompt
