from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, insert, select

from app.agents.campaign_prompt import (
    CAMPAIGN_PLAN,
    CAMPAIGN_SUGGEST,
    DEFAULTS,
    PLACEHOLDERS,
    build_suggest_prompt,
)
from app.campaigns.agent_prompts import PROMPT_KEYS, campaign_prompt_text
from app.db.models.campaign_agent_prompt import CampaignAgentPrompt
from app.db.session import SessionLocal
from app.main import app

client = TestClient(app)

PROMPTS = "/api/v1/prompt-config/campaign_agent_prompt"
FAR_FUTURE = date(2090, 6, 1)
PROMPTS_KEYS = list(PROMPT_KEYS)


@pytest.fixture(autouse=True)
def _signed_tokens(configured_reviewers):
    yield


@pytest.fixture
def restored_prompts(db):
    columns = [column.name for column in CampaignAgentPrompt.__table__.columns]
    with SessionLocal() as session:
        saved = [
            {name: getattr(row, name) for name in columns}
            for row in session.scalars(
                select(CampaignAgentPrompt).where(CampaignAgentPrompt.prompt_name.in_(PROMPTS_KEYS))
            )
        ]
    yield
    with SessionLocal() as session:
        session.execute(
            delete(CampaignAgentPrompt).where(CampaignAgentPrompt.prompt_name.in_(PROMPTS_KEYS))
        )
        if saved:
            session.execute(insert(CampaignAgentPrompt), saved)
        session.commit()


def _add(session, **values) -> None:
    session.add(CampaignAgentPrompt(prompt_name=CAMPAIGN_PLAN, **values))
    session.commit()


def test_every_instruction_the_agents_get_is_a_prompt_with_its_fields_declared() -> None:
    assert set(PROMPT_KEYS) == set(DEFAULTS) == set(PLACEHOLDERS)


def test_the_live_version_is_used_and_a_draft_is_not(restored_prompts) -> None:
    with SessionLocal() as session:
        _add(
            session,
            version=900,
            template="live text",
            valid_from=date(2090, 1, 1),
            status="published",
        )
        _add(session, version=901, template="draft text", status="draft")

        assert campaign_prompt_text(session, CAMPAIGN_PLAN, FAR_FUTURE) == "live text"


def test_the_built_in_text_is_used_when_no_version_is_live(restored_prompts) -> None:
    with SessionLocal() as session:
        session.execute(delete(CampaignAgentPrompt))
        session.commit()

        for key in PROMPT_KEYS:
            assert campaign_prompt_text(session, key, FAR_FUTURE) == DEFAULTS[key]


def test_nothing_is_added_to_a_prompt_in_code_except_the_numbers() -> None:
    plain = {**DEFAULTS, CAMPAIGN_SUGGEST: "Only this."}

    assert build_suggest_prompt(plain, 8) == "Only this."
    assert "Save at most 8 drafts" in build_suggest_prompt(DEFAULTS, 8)
    switched_off = build_suggest_prompt(DEFAULTS, 8, unavailable=("a", "b"))
    assert "do not call them: a, b" in switched_off


def test_a_prompt_can_be_drafted_published_and_read_back(restored_prompts, headers_for) -> None:
    headers = headers_for("studio-editor@example.test")
    draft = client.post(
        f"{PROMPTS}/draft",
        json={"component_key": CAMPAIGN_PLAN, "rows": [{"template": "New wording."}]},
        headers=headers,
    )
    assert draft.status_code == 200
    version = draft.json()["version"]

    with SessionLocal() as session:
        assert campaign_prompt_text(session, CAMPAIGN_PLAN, date.today()) != "New wording."

    published = client.post(
        f"{PROMPTS}/{version}/publish", json={"component_key": CAMPAIGN_PLAN}, headers=headers
    )
    assert published.status_code == 204
    with SessionLocal() as session:
        assert campaign_prompt_text(session, CAMPAIGN_PLAN, date.today()) == "New wording."


def test_an_unknown_prompt_a_blank_text_and_a_lost_field_are_refused(
    restored_prompts, headers_for
) -> None:
    headers = headers_for("studio-editor@example.test")

    def draft(key: str, text: str):
        return client.post(
            f"{PROMPTS}/draft",
            json={"component_key": key, "rows": [{"template": text}]},
            headers=headers,
        )

    unknown = draft("something_else", "Text.")
    blank = draft(CAMPAIGN_PLAN, "   ")
    lost_field = draft(CAMPAIGN_SUGGEST, "Suggest campaigns but forget the limit.")

    assert unknown.status_code == 422
    assert "not a campaign agent prompt" in unknown.text
    assert blank.status_code == 422
    assert "needs some text" in blank.text
    assert lost_field.status_code == 422
    assert "must keep these fields exactly" in lost_field.text
