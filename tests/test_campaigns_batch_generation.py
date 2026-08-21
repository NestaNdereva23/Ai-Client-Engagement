"""Batch generation: submitting a cohort to the provider's batch endpoint in
one call, and turning its results into the same pending-review messages the
synchronous /campaigns/{id}/generate path produces.

submit_batch covers building one provider request per eligible enrollment
and logging a touch for each, plus the no-eligible-clients case where the
provider is never called at all. ingest_batch covers a batch the provider
has not finished yet, an accepted result becoming a pending-review message,
a rejected result persisting a rejected run with no message, and ingesting
the same batch twice being a no-op the second time. The service-layer
wrappers cover CampaignNotFound and BatchNotFound, including a batch that
exists but under a different campaign.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, select

from app.agents.graph import ClientContext
from app.campaigns.batch_generation import BatchNotFound, ingest_batch, submit_batch
from app.campaigns.enrollment import enroll_cohort
from app.config import Settings
from app.db.models.campaigns import CampaignStep, Enrollment, TouchLog
from app.db.models.generation_batch import GenerationBatch, GenerationBatchItem
from app.db.models.llmops import (
    Evaluation,
    GenerationRun,
    LLMRequest,
    LLMResponse,
    TokenUsage,
    ToolCall,
    TraceRef,
)
from app.db.models.models import ClientFeatures, Clients, Funds, PiiVault
from app.db.models.outreach import Campaign, OutreachMessage, ReviewAction
from app.db.session import SessionLocal
from app.services.campaigns import (
    CampaignNotFound,
    add_campaign_step,
    ingest_campaign_batch,
    submit_campaign_batch,
)

FUND_ID = 987
CLIENT_ID = 98701


def make_settings() -> Settings:
    return Settings(
        llm_provider="anthropic",
        anthropic_api_key="test-key",
        llm_model="claude-opus-5",
        llm_temperature=None,
        llm_max_tokens=1024,
    )


def make_context_loader():
    def load(client_id: int, product: str) -> ClientContext:
        return ClientContext(
            raw_context={"client_id": client_id},
            angle="winback_habit",
            prompt_variant="winback_habit",
            chunks=(),
        )

    return load


def make_varying_facts_context_loader(facts_by_client: dict[int, dict]):
    """Same angle, tier, and chunks for every client -- only facts differ,
    exactly the shape a real cohort sharing one angle and product takes.
    """

    def load(client_id: int, product: str) -> ClientContext:
        return ClientContext(
            raw_context={"client_id": client_id},
            angle="winback_habit",
            prompt_variant="winback_habit",
            chunks=(),
            facts=facts_by_client[client_id],
        )

    return load


def draft_json(subject: str = "Come back to {{fund_name}}", body: str = "") -> str:
    return json.dumps({"subject": subject, "body": body})


def _touch_query(campaign_id: int, client_id: int):
    """The single touch_log row this test's own enrollment produced.

    Scoped by campaign and client rather than a bare select(TouchLog): the
    test database is shared and long-lived, not reset per test, so an
    unscoped query picks up every other test's and every manual run's rows
    too and .one() fails the moment there is more than the one this test
    created.
    """
    return select(TouchLog).where(
        TouchLog.enrollment_id.in_(
            select(Enrollment.enrollment_id).where(
                Enrollment.campaign_id == campaign_id, Enrollment.client_id == client_id
            )
        )
    )


@dataclass
class FakeBatches:
    """A fake anthropic.Anthropic().messages.batches namespace: enough of
    the real shape (create/retrieve/results, request_counts) for
    submit_batch and ingest_batch to run against, with no network call.
    """

    batch_id: str = "msgbatch_test"
    processing_status: str = "ended"
    results_to_return: list = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.created_requests: list = []
        if self.results_to_return is None:
            self.results_to_return = []

    def create(self, *, requests):
        self.created_requests = list(requests)
        return SimpleNamespace(id=self.batch_id)

    def retrieve(self, batch_id):
        succeeded = sum(1 for r in self.results_to_return if r.result.type == "succeeded")
        errored = len(self.results_to_return) - succeeded
        return SimpleNamespace(
            id=batch_id,
            processing_status=self.processing_status,
            request_counts=SimpleNamespace(
                succeeded=succeeded, errored=errored, canceled=0, expired=0
            ),
        )

    def results(self, batch_id):
        return iter(self.results_to_return)


class FakeBatchClient:
    def __init__(self, **kwargs) -> None:
        self.batches = FakeBatches(**kwargs)
        self.messages = SimpleNamespace(batches=self.batches)


def succeeded_result(custom_id: str, raw: str, *, stop_reason: str = "end_turn"):
    message = SimpleNamespace(
        content=[SimpleNamespace(type="text", text=raw)],
        usage=SimpleNamespace(input_tokens=12, output_tokens=34),
        stop_reason=stop_reason,
    )
    return SimpleNamespace(
        custom_id=custom_id, result=SimpleNamespace(type="succeeded", message=message)
    )


def errored_result(custom_id: str):
    return SimpleNamespace(custom_id=custom_id, result=SimpleNamespace(type="errored"))


class SpyTracer:
    """Records start_span/end_span calls in order; never talks to Langfuse.

    Same shape as test_agents_graph.SpyTracer, so a batch-drafted run's
    spans can be checked against the synchronous path's node names.
    """

    def __init__(self) -> None:
        self.started: list[dict] = []
        self.ended: list[dict] = []
        self.flushed = 0

    def start_span(self, *, trace_id, name, input, metadata=None, as_type="span", model=None):
        handle = object()
        self.started.append(
            {
                "trace_id": trace_id,
                "name": name,
                "input": input,
                "metadata": metadata,
                "as_type": as_type,
                "model": model,
            }
        )
        return handle

    def end_span(self, handle, *, output, usage_details=None) -> None:
        self.ended.append({"handle": handle, "output": output, "usage_details": usage_details})

    def get_trace_url(self, trace_id: str) -> None:
        return None

    def flush(self) -> None:
        self.flushed += 1

    def shutdown(self) -> None:
        pass


@pytest.fixture
def client(db: None):
    with SessionLocal() as session:
        session.add(Funds(unit_fund_id=FUND_ID, unit_fund_name="Cytonn Money Market Fund"))
        session.commit()
        session.add(
            Clients(
                client_id=CLIENT_ID,
                unit_fund_id=FUND_ID,
                n_purchases_returned=0,
                n_sales_returned=0,
            )
        )
        session.commit()
        session.add(
            PiiVault(client_id=CLIENT_ID, client_name="Jane Doe", contact_email="jane@example.com")
        )
        session.add(
            ClientFeatures(client_id=CLIENT_ID, fund_type="money_market", purchase_depth="single")
        )
        session.commit()

    yield CLIENT_ID

    with SessionLocal() as session:
        session.execute(delete(OutreachMessage).where(OutreachMessage.client_id == CLIENT_ID))
        run_ids = session.scalars(
            select(GenerationRun.run_id).where(GenerationRun.client_id == CLIENT_ID)
        ).all()
        if run_ids:
            request_ids = session.scalars(
                select(LLMRequest.request_id).where(LLMRequest.run_id.in_(run_ids))
            ).all()
            if request_ids:
                session.execute(delete(TokenUsage).where(TokenUsage.request_id.in_(request_ids)))
                session.execute(delete(LLMResponse).where(LLMResponse.request_id.in_(request_ids)))
                session.execute(delete(LLMRequest).where(LLMRequest.run_id.in_(run_ids)))
                session.execute(delete(ToolCall).where(ToolCall.run_id.in_(run_ids)))
            session.execute(delete(TraceRef).where(TraceRef.run_id.in_(run_ids)))
            session.execute(delete(Evaluation).where(Evaluation.run_id.in_(run_ids)))
            session.execute(delete(GenerationRun).where(GenerationRun.run_id.in_(run_ids)))
        session.execute(delete(ClientFeatures).where(ClientFeatures.client_id == CLIENT_ID))
        session.execute(delete(PiiVault).where(PiiVault.client_id == CLIENT_ID))
        session.execute(delete(Clients).where(Clients.client_id == CLIENT_ID))
        session.execute(delete(Funds).where(Funds.unit_fund_id == FUND_ID))
        session.commit()


@pytest.fixture
def campaign(client: int):
    with SessionLocal() as session:
        campaign_row = Campaign(name="batch generation test campaign")
        session.add(campaign_row)
        session.commit()
        campaign_id = campaign_row.campaign_id
        add_campaign_step(session, campaign_id, offset_days=0, message_angle="winback_habit")
        session.commit()
        enroll_cohort(session, campaign_id=campaign_id, client_ids=[client])
        session.commit()

    yield campaign_id

    with SessionLocal() as session:
        batch_ids = session.scalars(
            select(GenerationBatch.generation_batch_id).where(
                GenerationBatch.campaign_id == campaign_id
            )
        ).all()
        if batch_ids:
            session.execute(
                delete(GenerationBatchItem).where(
                    GenerationBatchItem.generation_batch_id.in_(batch_ids)
                )
            )
            session.execute(
                delete(GenerationBatch).where(GenerationBatch.generation_batch_id.in_(batch_ids))
            )
        enrollment_ids = session.scalars(
            select(Enrollment.enrollment_id).where(
                Enrollment.campaign_id == campaign_id, Enrollment.client_id == client
            )
        ).all()
        if enrollment_ids:
            session.execute(delete(TouchLog).where(TouchLog.enrollment_id.in_(enrollment_ids)))
        session.execute(
            delete(Enrollment).where(
                Enrollment.campaign_id == campaign_id, Enrollment.client_id == client
            )
        )
        session.execute(delete(CampaignStep).where(CampaignStep.campaign_id == campaign_id))
        message_ids = session.scalars(
            select(OutreachMessage.message_id).where(OutreachMessage.campaign_id == campaign_id)
        ).all()
        if message_ids:
            session.execute(delete(ReviewAction).where(ReviewAction.message_id.in_(message_ids)))
        session.execute(delete(OutreachMessage).where(OutreachMessage.campaign_id == campaign_id))
        session.execute(delete(Campaign).where(Campaign.campaign_id == campaign_id))
        session.commit()


def test_submit_batch_bundles_the_due_enrollment_into_one_provider_request(
    campaign: int, client: int
) -> None:
    fake_client = FakeBatchClient()
    settings = make_settings()

    with SessionLocal() as session:
        batch = submit_batch(
            session,
            campaign,
            settings=settings,
            context_loader=make_context_loader(),
            client=fake_client,
        )
        session.commit()
        batch_id = batch.generation_batch_id

    assert batch.status == "submitted"
    assert batch.requested_count == 1
    assert batch.provider_batch_id == fake_client.batches.batch_id
    assert len(fake_client.batches.created_requests) == 1

    with SessionLocal() as session:
        items = session.scalars(
            select(GenerationBatchItem).where(GenerationBatchItem.generation_batch_id == batch_id)
        ).all()
        assert len(items) == 1
        assert items[0].client_id == client
        assert items[0].status == "pending"

        touch = session.scalars(_touch_query(campaign, client)).one()
        assert touch.message_id is None  # nothing drafted yet, only submitted


def test_submit_batch_with_nothing_eligible_never_calls_the_provider(db: None) -> None:
    with SessionLocal() as session:
        campaign_row = Campaign(name="empty batch test campaign")
        session.add(campaign_row)
        session.commit()
        campaign_id = campaign_row.campaign_id

    fake_client = FakeBatchClient()
    with SessionLocal() as session:
        batch = submit_batch(
            session,
            campaign_id,
            settings=make_settings(),
            context_loader=make_context_loader(),
            client=fake_client,
        )
        session.commit()

    assert batch.status == "no_eligible_clients"
    assert batch.provider_batch_id is None
    assert fake_client.batches.created_requests == []

    with SessionLocal() as session:
        session.execute(delete(GenerationBatch).where(GenerationBatch.campaign_id == campaign_id))
        session.execute(delete(Campaign).where(Campaign.campaign_id == campaign_id))
        session.commit()


def test_ingest_batch_not_yet_ended_reports_status_with_no_outcomes(
    campaign: int, client: int
) -> None:
    fake_client = FakeBatchClient(processing_status="in_progress")
    settings = make_settings()
    with SessionLocal() as session:
        batch = submit_batch(
            session,
            campaign,
            settings=settings,
            context_loader=make_context_loader(),
            client=fake_client,
        )
        session.commit()
        batch_id = batch.generation_batch_id

    with SessionLocal() as session:
        result = ingest_batch(session, batch_id, settings=settings, client=fake_client)
        session.commit()

    assert result.outcomes == []
    assert result.batch.status == "in_progress"


def test_ingest_batch_accepted_result_creates_a_pending_review_message(
    campaign: int, client: int
) -> None:
    settings = make_settings()
    with SessionLocal() as session:
        batch = submit_batch(
            session,
            campaign,
            settings=settings,
            context_loader=make_context_loader(),
            client=FakeBatchClient(),  # only used to capture the custom_id below
        )
        session.commit()
        batch_id = batch.generation_batch_id
        custom_id = session.scalars(
            select(GenerationBatchItem.custom_id).where(
                GenerationBatchItem.generation_batch_id == batch_id
            )
        ).one()

    good_draft = draft_json(body="Dear {{first_name}}, {{fund_name}} misses you back on schedule.")
    fake_client = FakeBatchClient(results_to_return=[succeeded_result(custom_id, good_draft)])

    with SessionLocal() as session:
        result = ingest_batch(session, batch_id, settings=settings, client=fake_client)
        session.commit()

    assert len(result.outcomes) == 1
    assert result.outcomes[0].status == "accepted"
    assert result.batch.status == "ingested"
    assert result.batch.succeeded_count == 1

    with SessionLocal() as session:
        messages = session.scalars(
            select(OutreachMessage).where(OutreachMessage.campaign_id == campaign)
        ).all()
        assert len(messages) == 1
        assert messages[0].status == "pending_review"
        assert messages[0].client_id == client

        touch = session.scalars(_touch_query(campaign, client)).one()
        assert touch.message_id == messages[0].message_id

        item = session.scalars(
            select(GenerationBatchItem).where(GenerationBatchItem.custom_id == custom_id)
        ).one()
        assert item.status == "accepted"


def test_ingest_batch_errored_result_persists_a_rejected_run_and_no_message(
    campaign: int, client: int
) -> None:
    settings = make_settings()
    with SessionLocal() as session:
        batch = submit_batch(
            session,
            campaign,
            settings=settings,
            context_loader=make_context_loader(),
            client=FakeBatchClient(),
        )
        session.commit()
        batch_id = batch.generation_batch_id
        custom_id = session.scalars(
            select(GenerationBatchItem.custom_id).where(
                GenerationBatchItem.generation_batch_id == batch_id
            )
        ).one()

    fake_client = FakeBatchClient(results_to_return=[errored_result(custom_id)])
    with SessionLocal() as session:
        result = ingest_batch(session, batch_id, settings=settings, client=fake_client)
        session.commit()

    assert result.outcomes[0].status == "rejected"
    assert result.outcomes[0].reason == "batch result errored"

    with SessionLocal() as session:
        messages = session.scalars(
            select(OutreachMessage).where(OutreachMessage.campaign_id == campaign)
        ).all()
        assert messages == []

        run = session.scalars(select(GenerationRun).where(GenerationRun.run_id == custom_id)).one()
        assert run.status == "rejected"

        touch = session.scalars(_touch_query(campaign, client)).one()
        assert touch.message_id is None


def test_ingest_batch_is_a_no_op_the_second_time(campaign: int, client: int) -> None:
    settings = make_settings()
    with SessionLocal() as session:
        batch = submit_batch(
            session,
            campaign,
            settings=settings,
            context_loader=make_context_loader(),
            client=FakeBatchClient(),
        )
        session.commit()
        batch_id = batch.generation_batch_id
        custom_id = session.scalars(
            select(GenerationBatchItem.custom_id).where(
                GenerationBatchItem.generation_batch_id == batch_id
            )
        ).one()

    fake_client = FakeBatchClient(results_to_return=[errored_result(custom_id)])
    with SessionLocal() as session:
        ingest_batch(session, batch_id, settings=settings, client=fake_client)
        session.commit()

    with SessionLocal() as session:
        second = ingest_batch(session, batch_id, settings=settings, client=fake_client)
        session.commit()

    assert second.outcomes == []
    assert second.batch.status == "ingested"


def test_submit_campaign_batch_raises_campaign_not_found(db: None) -> None:
    with SessionLocal() as session, pytest.raises(CampaignNotFound):
        submit_campaign_batch(session, 999_999, settings=make_settings())


def test_ingest_campaign_batch_raises_batch_not_found_for_unknown_id(
    campaign: int, client: int
) -> None:
    with SessionLocal() as session, pytest.raises(BatchNotFound):
        ingest_campaign_batch(session, campaign, "not-a-real-batch-id", settings=make_settings())


def test_ingest_campaign_batch_raises_batch_not_found_for_the_wrong_campaign(
    campaign: int, client: int, db: None
) -> None:
    settings = make_settings()
    with SessionLocal() as session:
        batch = submit_batch(
            session,
            campaign,
            settings=settings,
            context_loader=make_context_loader(),
            client=FakeBatchClient(),
        )
        session.commit()
        batch_id = batch.generation_batch_id

    with SessionLocal() as session:
        other = Campaign(name="a different campaign entirely")
        session.add(other)
        session.commit()
        other_id = other.campaign_id

    with SessionLocal() as session, pytest.raises(BatchNotFound):
        ingest_campaign_batch(session, other_id, batch_id, settings=settings)

    with SessionLocal() as session:
        session.execute(delete(Campaign).where(Campaign.campaign_id == other_id))
        session.commit()


def test_submit_batch_traces_retrieve_context_and_assemble_prompt(
    campaign: int, client: int
) -> None:
    tracer = SpyTracer()
    with SessionLocal() as session:
        batch = submit_batch(
            session,
            campaign,
            settings=make_settings(),
            context_loader=make_context_loader(),
            tracer=tracer,
            client=FakeBatchClient(),
        )
        session.commit()
        item_trace_id = session.scalars(
            select(GenerationBatchItem.trace_id).where(
                GenerationBatchItem.generation_batch_id == batch.generation_batch_id
            )
        ).one()

    node_names = [call["name"] for call in tracer.started]
    assert node_names == ["retrieve_context", "assemble_prompt"]
    assert all(call["trace_id"] == item_trace_id for call in tracer.started)
    assert len(tracer.ended) == len(tracer.started)
    assert tracer.flushed >= 1


def test_ingest_batch_traces_generate_and_guardrails_under_the_same_trace(
    campaign: int, client: int
) -> None:
    submit_tracer = SpyTracer()
    settings = make_settings()
    with SessionLocal() as session:
        batch = submit_batch(
            session,
            campaign,
            settings=settings,
            context_loader=make_context_loader(),
            tracer=submit_tracer,
            client=FakeBatchClient(),
        )
        session.commit()
        batch_id = batch.generation_batch_id
        item_trace_id = session.scalars(
            select(GenerationBatchItem.trace_id).where(
                GenerationBatchItem.generation_batch_id == batch_id
            )
        ).one()
        custom_id = session.scalars(
            select(GenerationBatchItem.custom_id).where(
                GenerationBatchItem.generation_batch_id == batch_id
            )
        ).one()

    good_draft = draft_json(body="Dear {{first_name}}, {{fund_name}} misses you back on schedule.")
    fake_client = FakeBatchClient(results_to_return=[succeeded_result(custom_id, good_draft)])
    ingest_tracer = SpyTracer()

    with SessionLocal() as session:
        result = ingest_batch(
            session, batch_id, settings=settings, tracer=ingest_tracer, client=fake_client
        )
        session.commit()

    assert result.outcomes[0].status == "accepted"

    node_names = [call["name"] for call in ingest_tracer.started]
    assert node_names == ["generate", "guardrails"]
    assert all(call["trace_id"] == item_trace_id for call in ingest_tracer.started)
    # The same trace_id submit_batch already logged retrieve_context and
    # assemble_prompt under -- one Langfuse trace ends up with all four
    # spans, even though the two halves ran a day apart.
    assert item_trace_id in {call["trace_id"] for call in submit_tracer.started}

    generate_call = next(c for c in ingest_tracer.started if c["name"] == "generate")
    assert generate_call["as_type"] == "generation"
    assert generate_call["model"] == settings.llm_model
    generate_end = ingest_tracer.ended[node_names.index("generate")]
    assert generate_end["usage_details"] == {"input": 12, "output": 34}
    assert ingest_tracer.flushed >= 1


def test_ingest_batch_traces_only_generate_for_a_provider_side_failure(
    campaign: int, client: int
) -> None:
    settings = make_settings()
    with SessionLocal() as session:
        batch = submit_batch(
            session,
            campaign,
            settings=settings,
            context_loader=make_context_loader(),
            client=FakeBatchClient(),
        )
        session.commit()
        batch_id = batch.generation_batch_id
        custom_id = session.scalars(
            select(GenerationBatchItem.custom_id).where(
                GenerationBatchItem.generation_batch_id == batch_id
            )
        ).one()

    fake_client = FakeBatchClient(results_to_return=[errored_result(custom_id)])
    ingest_tracer = SpyTracer()

    with SessionLocal() as session:
        ingest_batch(session, batch_id, settings=settings, tracer=ingest_tracer, client=fake_client)
        session.commit()

    # No message ever came back from the provider for this item, so there is
    # nothing to trace as a generation or a guardrail check.
    assert ingest_tracer.started == []


def test_submit_batch_gives_two_clients_on_the_same_angle_an_identical_cached_block(
    db: None,
) -> None:
    """The end-to-end proof prompt caching depends on: two different real
    clients, submitted in the same batch, on the same angle/tier/product but
    with different facts, produce provider requests whose first system block
    (the one carrying cache_control) is byte-for-byte identical. Only the
    second, uncached block may differ.
    """
    fund_id = 989
    client_ids = (98901, 98902)
    with SessionLocal() as session:
        session.add(Funds(unit_fund_id=fund_id, unit_fund_name="Cytonn Money Market Fund"))
        session.commit()
        for client_id in client_ids:
            session.add(
                Clients(
                    client_id=client_id,
                    unit_fund_id=fund_id,
                    n_purchases_returned=0,
                    n_sales_returned=0,
                )
            )
        session.commit()
        for client_id in client_ids:
            session.add(
                PiiVault(
                    client_id=client_id,
                    client_name=f"Client {client_id}",
                    contact_email=f"client{client_id}@example.com",
                )
            )
            session.add(
                ClientFeatures(
                    client_id=client_id, fund_type="money_market", purchase_depth="single"
                )
            )
        session.commit()

        campaign_row = Campaign(name="shared cache batch test campaign")
        session.add(campaign_row)
        session.commit()
        campaign_id = campaign_row.campaign_id
        add_campaign_step(session, campaign_id, offset_days=0, message_angle="winback_habit")
        session.commit()
        enroll_cohort(session, campaign_id=campaign_id, client_ids=list(client_ids))
        session.commit()

    context_loader = make_varying_facts_context_loader(
        {
            client_ids[0]: {
                "cadence_band": "Regular",
                "invested_every_n_days": 30,
                "stale_contact": True,
            },
            client_ids[1]: {"stale_contact": False},
        }
    )
    fake_client = FakeBatchClient()

    with SessionLocal() as session:
        batch = submit_batch(
            session,
            campaign_id,
            settings=make_settings(),
            context_loader=context_loader,
            client=fake_client,
        )
        session.commit()

    assert batch.requested_count == 2
    requests = fake_client.batches.created_requests
    assert len(requests) == 2

    cached_blocks = [r["params"]["system"][0] for r in requests]
    dynamic_blocks = [r["params"]["system"][1] for r in requests]
    assert cached_blocks[0] == cached_blocks[1]
    assert cached_blocks[0]["cache_control"] == {"type": "ephemeral"}
    assert dynamic_blocks[0] != dynamic_blocks[1]

    with SessionLocal() as session:
        session.execute(
            delete(GenerationBatchItem).where(
                GenerationBatchItem.generation_batch_id == batch.generation_batch_id
            )
        )
        session.execute(
            delete(GenerationBatch).where(
                GenerationBatch.generation_batch_id == batch.generation_batch_id
            )
        )
        enrollment_ids = session.scalars(
            select(Enrollment.enrollment_id).where(Enrollment.campaign_id == campaign_id)
        ).all()
        if enrollment_ids:
            session.execute(delete(TouchLog).where(TouchLog.enrollment_id.in_(enrollment_ids)))
        session.execute(delete(Enrollment).where(Enrollment.campaign_id == campaign_id))
        session.execute(delete(CampaignStep).where(CampaignStep.campaign_id == campaign_id))
        session.execute(delete(Campaign).where(Campaign.campaign_id == campaign_id))
        for client_id in client_ids:
            session.execute(delete(ClientFeatures).where(ClientFeatures.client_id == client_id))
            session.execute(delete(PiiVault).where(PiiVault.client_id == client_id))
            session.execute(delete(Clients).where(Clients.client_id == client_id))
        session.execute(delete(Funds).where(Funds.unit_fund_id == fund_id))
        session.commit()
