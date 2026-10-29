from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.agents.web_research import WebResult
from app.campaigns import agent_information
from app.config import Settings
from app.db.models.rag import DOC_TYPE_REPORT, RagDocument, RagDocumentVersion
from app.db.session import SessionLocal
from app.main import app
from app.rag.retrieve import Retrieved

client = TestClient(app)

TITLE = "campaign information test weekly report"


@pytest.fixture(autouse=True)
def _signed_tokens(configured_reviewers):
    yield


@pytest.fixture
def weekly_report(db):
    with SessionLocal() as session:
        document = RagDocument(title=TITLE, doc_type=DOC_TYPE_REPORT)
        session.add(document)
        session.flush()
        version = RagDocumentVersion(
            doc_id=document.doc_id, version_no=1, published_on=date(2026, 10, 26), is_active=True
        )
        session.add(version)
        session.commit()
        ids = (document.doc_id, version.version_id)
    yield ids
    with SessionLocal() as session:
        session.execute(delete(RagDocumentVersion).where(RagDocumentVersion.doc_id == ids[0]))
        session.execute(delete(RagDocument).where(RagDocument.doc_id == ids[0]))
        session.commit()


def _hit(version_id: int, text: str) -> Retrieved:
    return Retrieved(
        chunk_id=1,
        text=text,
        metadata={"section": "Money Markets"},
        score=0.9,
        version_id=version_id,
    )


def test_the_planner_reads_the_weekly_report_with_its_title_and_date(
    weekly_report, monkeypatch
) -> None:
    passage = "The money market fund yielded 15.2% over the week."
    monkeypatch.setattr(
        agent_information,
        "retrieve_product_facts",
        lambda session, product, k: [_hit(weekly_report[1], passage)],
    )

    with SessionLocal() as session:
        answer = agent_information.get_weekly_report_facts(session, product=" Money Market ")

    assert answer["product"] == "money market"
    assert answer["report"] == TITLE
    assert answer["published_on"] == "2026-10-26"
    assert answer["passages"] == [{"text": passage, "section": "Money Markets"}]


def test_the_planner_is_told_plainly_when_the_report_has_nothing(monkeypatch) -> None:
    monkeypatch.setattr(agent_information, "retrieve_product_facts", lambda session, product, k: [])

    with SessionLocal() as session:
        answer = agent_information.get_weekly_report_facts(session, product="money market")

    assert answer["error"] == "no_weekly_report_facts"


class _Tidier:
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.system = ""
        self.user = ""

    def generate(self, *, system: str, user: str) -> str:
        self.system, self.user = system, user
        return self.reply


def _search(monkeypatch, results: list[WebResult], reply: str) -> _Tidier:
    tidier = _Tidier(reply)
    monkeypatch.setattr(
        agent_information,
        "get_settings",
        lambda: Settings(exa_api_key="key", campaign_agent_web_domains="cytonn.com"),
    )
    monkeypatch.setattr(
        agent_information, "search_verified_web", lambda settings, query, k, domains: results
    )
    monkeypatch.setattr(agent_information, "get_campaign_agent_llm_client", lambda settings: tidier)
    return tidier


RESULTS = [
    WebResult("Weekly note", "https://cytonn.com/research/a", "The fund returned 15.2% this week."),
    WebResult("Other", "https://cytonn.com/research/b", "Rates held at 9.75% on Tuesday."),
]


def test_web_results_are_tidied_by_the_saved_prompt_before_the_agent_sees_them(
    monkeypatch,
) -> None:
    tidier = _search(
        monkeypatch,
        RESULTS,
        '{"findings": [{"source": 1, "text": "The fund returned 15.2% this week."},'
        ' {"source": 2, "text": "The central bank rate stayed at 9.75%."}]}',
    )

    with SessionLocal() as session:
        answer = agent_information.campaign_search_web(session, query="money market rate")

    assert [finding["url"] for finding in answer["findings"]] == [
        "https://cytonn.com/research/a",
        "https://cytonn.com/research/b",
    ]
    assert "tidy web search results" in tidier.system
    assert "at most 5 findings" in tidier.system
    assert "[1] Weekly note | The fund returned 15.2% this week." in tidier.user


def test_a_tidied_fact_with_a_number_the_page_never_gave_is_dropped(monkeypatch) -> None:
    _search(
        monkeypatch,
        RESULTS,
        '{"findings": [{"source": 1, "text": "The fund returned 18.4% this week."},'
        ' {"source": 2, "text": "Rates held at 9.75% on Tuesday."},'
        ' {"source": 2, "text": "A fact with no figure at all."}]}',
    )

    with SessionLocal() as session:
        answer = agent_information.campaign_search_web(session, query="money market rate")

    assert [finding["text"] for finding in answer["findings"]] == [
        "Rates held at 9.75% on Tuesday."
    ]


def test_a_reply_that_is_not_json_is_an_error_not_raw_text(monkeypatch) -> None:
    _search(monkeypatch, RESULTS, "Here are some facts you might like.")

    with SessionLocal() as session:
        answer = agent_information.campaign_search_web(session, query="money market rate")

    assert answer["error"] == "refine_failed"
    assert "findings" not in answer


def test_web_results_holding_contact_details_are_not_used(monkeypatch) -> None:
    _search(
        monkeypatch,
        [WebResult("Contact", "https://cytonn.com/c", "Write to someone@example.com for 9.75%.")],
        '{"findings": []}',
    )

    with SessionLocal() as session:
        answer = agent_information.campaign_search_web(session, query="money market rate")

    assert answer["error"] == "web_results_withheld"


def test_the_studio_lists_every_tool_with_the_description_the_model_gets(headers_for) -> None:
    response = client.get(
        "/api/v1/campaign-agent-studio/tools", headers=headers_for("studio-reader@example.test")
    )

    tools = {tool["name"]: tool for tool in response.json()}
    assert response.status_code == 200
    assert {"choose_touch_plan", "save_campaign_draft", "get_weekly_report_facts"} <= set(tools)
    assert tools["search_approved_sites"]["switch"] == "campaign_agent_use_web_research"
    assert tools["get_touch_history"]["switch"] is None
    assert "Planning" in tools["choose_touch_plan"]["used_by"]


def test_the_studio_shows_the_active_weekly_report(weekly_report, headers_for) -> None:
    response = client.get(
        "/api/v1/campaign-agent-studio/weekly-report",
        headers=headers_for("studio-reader@example.test"),
    )

    assert response.status_code == 200
    assert response.json()["title"] == TITLE
    assert response.json()["version_no"] == 1
