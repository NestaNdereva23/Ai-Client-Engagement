from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import delete, select

from app.config import Settings
from app.db.models.llmops import GenerationRun
from app.db.models.models import ClientFeatures, Clients, Funds, PiiVault
from app.db.models.outreach import Campaign, OutreachMessage, ReviewAction
from app.db.models.rules import ClientMessageIndicators
from app.db.session import SessionLocal
from app.llmops.versions import persist_generation_run
from app.main import app
from app.privacy.fact_block import ModelFactBlock
from app.services.prompt_testing import PromptTestingError, generate_test_draft
from app.services.review import create_outreach_message, decide
from app.services.review_metrics import angle_version_metrics

client = TestClient(app)

FUND_ID = 953
CLIENT_ID = 95301
ANGLE = "prompt_testing_fixture_angle"


class ScriptedLLMClient:
    model = "scripted"

    def __init__(self, reply: str) -> None:
        self._reply = reply
        self.last_usage = None

    def generate(self, *, system: str, user: str) -> str:
        return self._reply


def scripted_llm_client() -> ScriptedLLMClient:
    return ScriptedLLMClient('{"subject": "A quick note", "body": "Hi there, we miss you."}')


def make_settings(**overrides) -> Settings:
    defaults = {
        "llm_provider": "anthropic",
        "anthropic_api_key": "test-key",
        "llm_model": "claude-opus-5",
        "llm_temperature": None,
        "llm_max_tokens": 1024,
    }
    defaults.update(overrides)
    return Settings(**defaults)


def accepted_state(*, angle_catalog_version: int, attempts: int = 1) -> dict:
    return {
        "run_id": str(uuid4()),
        "trace_id": uuid4().hex,
        "client_id": CLIENT_ID,
        "product": "money market",
        "angle": ANGLE,
        "priority_tier": None,
        "prompt_variant": ANGLE,
        "angle_catalog_version": angle_catalog_version,
        "status": "accepted",
        "attempts": attempts,
        "failed_guardrail": None,
        "reason": None,
        "raw_structured_output": {"subject": "s", "body": "b"},
    }


@pytest.fixture
def review_fixture(db: None):
    with SessionLocal() as session:
        session.add(Funds(unit_fund_id=FUND_ID, unit_fund_name="Test Fund"))
        session.commit()
        session.add(
            Clients(
                client_id=CLIENT_ID,
                unit_fund_id=FUND_ID,
                n_purchases_returned=0,
                n_sales_returned=0,
            )
        )
        session.add(PiiVault(client_id=CLIENT_ID, client_name="Jane Doe"))
        session.commit()

        campaign = Campaign(name="prompt testing metrics fixture campaign")
        session.add(campaign)
        session.commit()
        campaign_id = campaign.campaign_id

        message_ids = []
        run_ids = []
        outcomes_and_versions = [
            ("approve", 1, 1),
            ("approve", 1, 1),
            ("edit_approve", 1, 1),
            ("reject", 1, 2),
            ("approve", 2, 1),
        ]
        for outcome, angle_catalog_version, attempts in outcomes_and_versions:
            run = persist_generation_run(
                session,
                accepted_state(angle_catalog_version=angle_catalog_version, attempts=attempts),
                make_settings(),
            )
            message = create_outreach_message(session, run, campaign_id=campaign_id)
            session.commit()
            edited = {"subject": "s", "body": "edited"} if outcome == "edit_approve" else None
            decide(
                session,
                message.message_id,
                outcome=outcome,
                reviewer_id="fa-1",
                edited_content=edited,
            )
            session.commit()
            message_ids.append(message.message_id)
            run_ids.append(run.run_id)

        yield

        session.execute(delete(ReviewAction).where(ReviewAction.message_id.in_(message_ids)))
        session.execute(delete(OutreachMessage).where(OutreachMessage.message_id.in_(message_ids)))
        session.execute(delete(GenerationRun).where(GenerationRun.run_id.in_(run_ids)))
        session.execute(delete(Campaign).where(Campaign.campaign_id == campaign_id))
        session.execute(delete(PiiVault).where(PiiVault.client_id == CLIENT_ID))
        session.execute(delete(Clients).where(Clients.client_id == CLIENT_ID))
        session.execute(delete(Funds).where(Funds.unit_fund_id == FUND_ID))
        session.commit()


def test_angle_version_metrics_computes_rates_per_version(review_fixture: None) -> None:
    with SessionLocal() as session:
        metrics = angle_version_metrics(session, ANGLE)

    by_version = {m.angle_catalog_version: m for m in metrics}
    assert by_version[1].review_count == 4
    assert by_version[1].approval_rate == pytest.approx(0.5)
    assert by_version[1].edit_rate == pytest.approx(0.25)
    assert by_version[1].rejection_rate == pytest.approx(0.25)
    assert by_version[1].regeneration_rate == pytest.approx(0.25)

    assert by_version[2].review_count == 1
    assert by_version[2].approval_rate == pytest.approx(1.0)
    assert by_version[2].regeneration_rate == pytest.approx(0.0)


def test_angle_metrics_endpoint(
    review_fixture: None, configured_reviewers: None, reviewer_1_headers: dict[str, str]
) -> None:
    response = client.get(
        f"/api/v1/prompt-config/angles/{ANGLE}/metrics", headers=reviewer_1_headers
    )
    assert response.status_code == 200
    body = response.json()
    assert {row["angle_catalog_version"] for row in body} == {1, 2}


@pytest.fixture
def test_draft_client(db: None):
    fund_id = 954
    client_id = 95401
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
        session.add(PiiVault(client_id=client_id, client_name="Jane Doe"))
        session.commit()
        session.add(ClientFeatures(client_id=client_id))
        session.commit()

    yield client_id

    with SessionLocal() as session:
        session.execute(delete(GenerationRun).where(GenerationRun.client_id == client_id))
        session.execute(
            delete(ClientMessageIndicators).where(ClientMessageIndicators.client_id == client_id)
        )
        session.execute(delete(ClientFeatures).where(ClientFeatures.client_id == client_id))
        session.execute(delete(PiiVault).where(PiiVault.client_id == client_id))
        session.execute(delete(Clients).where(Clients.client_id == client_id))
        session.execute(delete(Funds).where(Funds.unit_fund_id == fund_id))
        session.commit()


def test_generate_test_draft_from_a_fact_profile_never_writes_an_outreach_message(db: None) -> None:
    run_id = None
    try:
        with SessionLocal() as session:
            runs = generate_test_draft(
                session,
                angle=None,
                tier=None,
                angle_version=None,
                tier_version=None,
                voice_version=1,
                safety_version=1,
                output_version=1,
                personalization_version=1,
                fact_profile={"fund_name": "Cytonn Money Market Fund"},
                settings=make_settings(),
                llm_client=scripted_llm_client(),
            )
            session.commit()
            run_id = runs[0].run_id

            assert runs[0].run_kind == "test"
            assert runs[0].client_id is None
            found = session.scalar(
                select(OutreachMessage).where(OutreachMessage.generation_run_id == run_id)
            )
            assert found is None
    finally:
        if run_id is not None:
            with SessionLocal() as session:
                session.execute(delete(GenerationRun).where(GenerationRun.run_id == run_id))
                session.commit()


def test_generate_test_draft_requires_exactly_one_source(db: None) -> None:
    with SessionLocal() as session:
        with pytest.raises(PromptTestingError):
            generate_test_draft(
                session,
                angle=None,
                tier=None,
                angle_version=None,
                tier_version=None,
                voice_version=1,
                safety_version=1,
                output_version=1,
                personalization_version=1,
                client_id=1,
                fact_profile={"fund_name": "x"},
            )


def test_generate_test_draft_from_a_real_client_leaves_indicators_untouched(
    test_draft_client: int,
) -> None:
    with SessionLocal() as session:
        runs = generate_test_draft(
            session,
            angle=None,
            tier=None,
            angle_version=None,
            tier_version=None,
            voice_version=1,
            safety_version=1,
            output_version=1,
            personalization_version=1,
            client_id=test_draft_client,
            settings=make_settings(),
            llm_client=scripted_llm_client(),
        )
        session.commit()

        assert runs[0].run_kind == "test"
        assert runs[0].client_id == test_draft_client
        assert session.get(ClientMessageIndicators, test_draft_client) is None


@pytest.fixture
def explain_fixture(db: None):
    fund_id = 955
    client_id = 95501
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

        state = accepted_state(angle_catalog_version=3)
        state["client_id"] = client_id
        state["voice_contract_version"] = 1
        state["safety_policy_version"] = 1
        state["output_policy_version"] = 1
        state["personalization_policy_version"] = 1
        state["context"] = {"fund_name": "Cytonn Money Market Fund"}
        run = persist_generation_run(session, state, make_settings())
        session.commit()
        run_id = run.run_id

    yield run_id

    with SessionLocal() as session:
        session.execute(delete(GenerationRun).where(GenerationRun.run_id == run_id))
        session.execute(delete(Clients).where(Clients.client_id == client_id))
        session.execute(delete(Funds).where(Funds.unit_fund_id == fund_id))
        session.commit()


def test_explain_endpoint_reports_resolved_versions_and_fact_usage(
    explain_fixture: str, configured_reviewers: None, reviewer_1_headers: dict[str, str]
) -> None:
    response = client.get(
        f"/api/v1/prompt-config/generations/{explain_fixture}/explain", headers=reviewer_1_headers
    )
    assert response.status_code == 200
    body = response.json()
    assert body["angle_catalog_version"] == 3
    assert body["voice_contract_version"] == 1
    facts_by_field = {row["field"]: row for row in body["facts"]}
    assert facts_by_field["fund_name"]["used"] is True
    assert facts_by_field["fund_name"]["value"] == "Cytonn Money Market Fund"
    assert facts_by_field["exit_reason"]["used"] is False


def test_model_fact_block_rejects_an_unknown_field_in_a_test_profile() -> None:
    with pytest.raises(ValidationError):
        ModelFactBlock(not_a_real_field="x")
