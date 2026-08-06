"""M6 end to end: given a client, the harness returns a validated masked
draft tied to a prompt/model version.

Drives the real path a caller uses: build EmailAgent, register it behind the
Orchestrator, generate for one client, then persist_generation_run. These
prove an accepted draft carries the required placeholders and never a
literal PII pattern, grounding and format/length both pass a good draft and
each independently rejects a bad one, and every terminal run (accepted or
rejected) is stamped with a prompt_version and a model_version.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest
from sqlalchemy import delete, select

from app.agents.email_agent import REQUIRED_PLACEHOLDERS
from app.agents.email_channel import EmailAgent
from app.agents.graph import ClientContext
from app.agents.guardrails import MIN_BODY_LENGTH
from app.agents.orchestrator import Orchestrator
from app.config import Settings
from app.db.models.llmops import (
    GenerationRun,
    LLMRequest,
    LLMResponse,
    ModelVersion,
    PromptVersion,
    TokenUsage,
    ToolCall,
    TraceRef,
)
from app.db.models.models import Clients, Funds
from app.db.session import SessionLocal
from app.llmops.telemetry import persist_generation_telemetry
from app.llmops.versions import persist_generation_run
from app.privacy.scanners import scan_outbound

RAW_CONTEXT = {
    "client_id": 90101,
    "recency_band": "Under 1y",
    "value_band": "High",
    "cadence_band": "Regular",
    "hold_band": "Stayed months",
}


@dataclass(frozen=True)
class FakeChunk:
    chunk_id: int
    text: str


def draft_json(subject: str, body: str) -> str:
    return json.dumps({"subject": subject, "body": body})


def make_context_loader(chunks=()):
    def load(client_id: int, product: str) -> ClientContext:
        return ClientContext(
            raw_context=RAW_CONTEXT,
            angle="back_on_schedule",
            prompt_variant="back_on_schedule",
            chunks=chunks,
        )

    return load


class ScriptedLLMClient:
    model = "stub"

    def __init__(self, drafts: list[str]) -> None:
        self._drafts = list(drafts)

    def generate(self, *, system: str, user: str) -> str:
        return self._drafts.pop(0)


def make_settings() -> Settings:
    return Settings(
        llm_provider="anthropic",
        anthropic_api_key="test-key",
        llm_model="claude-opus-5",
        llm_temperature=None,
        llm_max_tokens=1024,
    )


@pytest.fixture
def client(db: None):
    """Seed one fund and one client so generation_runs' FK is satisfiable."""
    fund_id = 901
    client_id = 90101
    with SessionLocal() as session:
        session.add(Funds(unit_fund_id=fund_id, unit_fund_name="Test Fund"))
        session.commit()
        session.add(
            Clients(
                client_id=client_id,
                unit_fund_id=fund_id,
                n_purchases_returned=0,
                n_sales_returned=0,
            )
        )
        session.commit()

    yield client_id

    with SessionLocal() as session:
        run_ids = session.scalars(
            select(GenerationRun.run_id).where(GenerationRun.client_id == client_id)
        ).all()
        if run_ids:
            request_ids = session.scalars(
                select(LLMRequest.request_id).where(LLMRequest.run_id.in_(run_ids))
            ).all()
            if request_ids:
                session.execute(delete(TokenUsage).where(TokenUsage.request_id.in_(request_ids)))
                session.execute(delete(LLMResponse).where(LLMResponse.request_id.in_(request_ids)))
                session.execute(delete(LLMRequest).where(LLMRequest.request_id.in_(request_ids)))
            session.execute(delete(ToolCall).where(ToolCall.run_id.in_(run_ids)))
            session.execute(delete(TraceRef).where(TraceRef.run_id.in_(run_ids)))
        session.execute(delete(GenerationRun).where(GenerationRun.client_id == client_id))
        session.execute(delete(Clients).where(Clients.client_id == client_id))
        session.execute(delete(Funds).where(Funds.unit_fund_id == fund_id))
        session.commit()


def test_a_good_draft_is_accepted_with_placeholders_no_pii_and_stamped_versions(
    client: int,
) -> None:
    chunks = [FakeChunk(chunk_id=1, text="the fund yielded 11.35% this week")]
    llm = ScriptedLLMClient(
        [
            draft_json(
                subject="Come back to {{fund_name}}",
                body="Dear {{first_name}}, {{fund_name}} returned 11.35% last week.",
            )
        ]
    )
    agent = EmailAgent(context_loader=make_context_loader(chunks), llm_client=llm)
    orchestrator = Orchestrator()
    orchestrator.register(agent)

    result = orchestrator.generate("email", client_id=client, product="money market")

    assert result["status"] == "accepted"

    # The draft carries the required placeholders...
    combined = f"{result['subject']}\n{result['body']}"
    for token in REQUIRED_PLACEHOLDERS:
        assert token in combined

    # ...and never a literal PII pattern: the same scan the boundary already
    # ran, asserted here explicitly against the accepted content.
    scan_outbound(result["body"])  # does not raise
    scan_outbound(result["subject"])  # does not raise

    settings = make_settings()
    with SessionLocal() as session:
        run = persist_generation_run(session, result, settings)
        persist_generation_telemetry(session, run, result)
        session.commit()
        run_id = run.run_id

    with SessionLocal() as session:
        stored = session.get(GenerationRun, run_id)
        prompt_version = session.get(PromptVersion, stored.prompt_version_id)
        model_version = session.get(ModelVersion, stored.model_version_id)
        requests = session.scalars(select(LLMRequest).where(LLMRequest.run_id == run_id)).all()
        tool_calls = session.scalars(select(ToolCall).where(ToolCall.run_id == run_id)).all()
        trace_ref = session.get(TraceRef, run_id)

    assert stored.prompt_version_id is not None
    assert stored.model_version_id is not None
    assert prompt_version.prompt_variant == "back_on_schedule"
    assert model_version.model_id == "claude-opus-5"

    assert len(requests) == 1
    assert requests[0].model_version_id == stored.model_version_id
    assert {t.tool_name for t in tool_calls} == {"context_fetch", "rag_retrieval"}
    assert trace_ref.trace_id == stored.trace_id


def test_grounding_passes_a_good_draft_and_rejects_a_bad_one(client: int) -> None:
    chunks = [FakeChunk(chunk_id=1, text="the fund yielded 11.35% this week")]

    good = EmailAgent(
        context_loader=make_context_loader(chunks),
        llm_client=ScriptedLLMClient(
            [draft_json(subject="Come back to {{fund_name}}", body="{{first_name}}, 11.35%!")]
        ),
    )
    good_result = good.generate(client_id=client, product="money market")
    assert good_result["status"] == "accepted"

    bad = EmailAgent(
        context_loader=make_context_loader(chunks),
        llm_client=ScriptedLLMClient(
            [
                draft_json(subject="Come back to {{fund_name}}", body="{{first_name}}, 99.99%!"),
                draft_json(subject="Come back to {{fund_name}}", body="{{first_name}}, 42.00%!"),
            ]
        ),
        max_attempts=2,
    )
    bad_result = bad.generate(client_id=client, product="money market")

    assert bad_result["status"] == "rejected"
    assert bad_result["failed_guardrail"] == "grounding"

    with SessionLocal() as session:
        run = persist_generation_run(session, bad_result, make_settings())
        session.commit()
        stored = session.get(GenerationRun, run.run_id)

    # Stamped even though the run was rejected: rejection reasons still need
    # to be traceable to the version that produced them.
    assert stored.prompt_version_id is not None
    assert stored.model_version_id is not None
    # Grounding fails after structured output parses, so the last attempt's
    # content is still on record.
    assert stored.ai_draft_content is not None


def test_format_length_passes_a_good_draft_and_rejects_a_too_short_one(client: int) -> None:
    good = EmailAgent(
        context_loader=make_context_loader(),
        llm_client=ScriptedLLMClient(
            [
                draft_json(
                    subject="Come back to {{fund_name}}",
                    body="Dear {{first_name}}, we would love to see you invest again soon.",
                )
            ]
        ),
    )
    good_result = good.generate(client_id=client, product="money market")
    assert good_result["status"] == "accepted"

    too_short = EmailAgent(
        context_loader=make_context_loader(),
        llm_client=ScriptedLLMClient(
            [draft_json(subject="Hi {{first_name}}", body="{{fund_name}}")]
        ),
        max_attempts=1,
    )
    short_result = too_short.generate(client_id=client, product="money market")

    assert len(short_result["body"]) < MIN_BODY_LENGTH
    assert short_result["status"] == "rejected"
    assert short_result["failed_guardrail"] == "format_length"

    with SessionLocal() as session:
        run = persist_generation_run(session, short_result, make_settings())
        session.commit()
        stored = session.get(GenerationRun, run.run_id)

    assert stored.prompt_version_id is not None
    assert stored.model_version_id is not None
