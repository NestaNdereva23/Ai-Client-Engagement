from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.agents.tools import search_knowledge
from app.db.models.audit import AuditLog
from app.db.models.rag import (
    DOC_TYPE_CLIENT_GUIDE,
    RagChunk,
    RagDocument,
    RagDocumentVersion,
)
from app.db.session import SessionLocal
from app.main import app
from app.rag.chunking import ReportChunk
from app.rag.embedding import HashingEmbedder
from app.rag.guides import (
    GUIDE_MAX_CHARS,
    GuideRejected,
    add_guide,
    approve_guide_version,
)
from app.rag.index import index_chunks
from app.rag.retrieve import retrieve, retrieve_product_facts
from app.services.rag import VersionNotApproved, activate_version

client = TestClient(app)
RAG = "/api/v1/rag"
EMB = HashingEmbedder()

TITLE_PREFIX = "Guide test: "
TITLE = f"{TITLE_PREFIX}saving a little each month"
TEXT = "Saving a little each month is easier to keep up than saving one large amount."
NEWER_TEXT = "Saving a little each month adds up, and you never have to pick the perfect moment."
REPORT_SOURCE = "client-guide-test-report"


def _purge() -> None:
    with SessionLocal() as session:
        doc_ids = session.scalars(
            select(RagDocument.doc_id).where(
                RagDocument.doc_type == DOC_TYPE_CLIENT_GUIDE,
                RagDocument.title.like(f"{TITLE_PREFIX}%"),
            )
        ).all()
        doc_ids += session.scalars(
            select(RagDocument.doc_id).where(RagDocument.source == REPORT_SOURCE)
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


@pytest.fixture
def authed(configured_reviewers, reviewer_1_headers):
    client.headers.update(reviewer_1_headers)
    yield
    client.headers.pop("Authorization", None)


def _guide_texts(session, query: str) -> list[str]:
    hits = retrieve(session, query, doc_type=DOC_TYPE_CLIENT_GUIDE, embedder=EMB, k=10)
    return [hit.text for hit in hits]


def test_a_guide_is_found_only_after_a_person_approves_it(clean) -> None:
    with SessionLocal() as session:
        guide = add_guide(
            session, title=TITLE, topic="Saving habit", text=TEXT, created_by="writer", embedder=EMB
        )
        before = search_knowledge(session, query=TEXT)["results"]
        approve_guide_version(session, guide.version_id, approved_by="lead")
        session.commit()
        after = search_knowledge(session, query=TEXT)["results"]

    assert not [row for row in before if row["kind"] == DOC_TYPE_CLIENT_GUIDE]
    assert after[0]["kind"] == DOC_TYPE_CLIENT_GUIDE
    assert after[0]["text"] == TEXT


def test_a_new_version_waits_while_the_approved_one_keeps_serving(clean) -> None:
    with SessionLocal() as session:
        first = add_guide(
            session, title=TITLE, topic="Saving habit", text=TEXT, created_by="writer", embedder=EMB
        )
        approve_guide_version(session, first.version_id, approved_by="lead")
        second = add_guide(
            session,
            title=TITLE,
            topic="Saving habit",
            text=NEWER_TEXT,
            created_by="writer",
            embedder=EMB,
        )
        session.commit()
        while_waiting = _guide_texts(session, NEWER_TEXT)
        approve_guide_version(session, second.version_id, approved_by="lead")
        session.commit()
        after_approval = _guide_texts(session, NEWER_TEXT)

    assert second.version_no == 2
    assert while_waiting == [TEXT]
    assert after_approval == [NEWER_TEXT]


def test_a_product_search_finds_a_guide_but_a_draft_never_gets_one(clean) -> None:
    guide_text = "A money market fund keeps your money easy to reach and earns a steady return."
    with SessionLocal() as session:
        guide = add_guide(
            session,
            title=f"{TITLE_PREFIX}money market fund",
            topic="Products",
            text=guide_text,
            created_by="writer",
            embedder=EMB,
        )
        approve_guide_version(session, guide.version_id, approved_by="lead")
        document = RagDocument(title="Weekly Report", source=REPORT_SOURCE)
        session.add(document)
        session.flush()
        report_version = RagDocumentVersion(doc_id=document.doc_id, version_no=1, is_active=True)
        session.add(report_version)
        session.flush()
        report_version_id = report_version.version_id
        session.commit()

    with SessionLocal() as session:
        index_chunks(
            session,
            report_version_id,
            [
                ReportChunk(
                    0,
                    "The money market fund closed the week at a steady yield.",
                    {"section": "Money Markets"},
                )
            ],
            embedder=EMB,
        )

    with SessionLocal() as session:
        searched = retrieve(
            session, "money market fund", sections=["Money Markets"], embedder=EMB, k=10
        )
        drafted = retrieve_product_facts(
            session, product="Money Market Fund", angle="winback_habit", embedder=EMB, k=10
        )

    assert {hit.doc_type for hit in searched} == {DOC_TYPE_CLIENT_GUIDE, "report"}
    assert drafted
    assert {hit.doc_type for hit in drafted} == {"report"}


@pytest.mark.parametrize(
    "text",
    [
        "x" * (GUIDE_MAX_CHARS + 1),
        "Write to us at someone@example.com for more help.",
        "Call 0712 345 678 for more help.",
    ],
)
def test_a_guide_must_be_short_and_hold_no_contact_details(clean, text: str) -> None:
    with SessionLocal() as session:
        with pytest.raises(GuideRejected):
            add_guide(session, title=TITLE, topic="Saving habit", text=text, created_by="writer")
        session.rollback()
        assert session.scalar(select(RagDocument).where(RagDocument.title == TITLE)) is None


def test_a_guide_may_only_name_actions_that_exist(clean) -> None:
    with SessionLocal() as session:
        with pytest.raises(GuideRejected, match="no_such_action"):
            add_guide(
                session,
                title=TITLE,
                topic="Saving habit",
                text=TEXT,
                action_codes=["fee_warning", "no_such_action"],
                created_by="writer",
            )
        session.rollback()
        assert session.scalar(select(RagDocument).where(RagDocument.title == TITLE)) is None


def test_the_rollback_path_will_not_make_an_unapproved_guide_live(clean) -> None:
    with SessionLocal() as session:
        guide = add_guide(
            session, title=TITLE, topic="Saving habit", text=TEXT, created_by="writer", embedder=EMB
        )
        with pytest.raises(VersionNotApproved):
            activate_version(session, guide.version_id)
        session.rollback()


def test_a_guide_goes_from_writing_to_live_through_the_api(clean, authed) -> None:
    body = {
        "title": TITLE,
        "topic": "Saving habit",
        "text": TEXT,
        "action_codes": ["welcome_and_top_up"],
    }

    created = client.post(f"{RAG}/guides", json=body)
    assert created.status_code == 201
    version_id = created.json()["version_id"]
    assert created.json()["status"] == "waiting_for_approval"
    assert created.json()["action_codes"] == ["welcome_and_top_up"]

    assert version_id not in {row["version_id"] for row in client.get(f"{RAG}/versions").json()}
    assert client.post(f"{RAG}/versions/{version_id}/activate").status_code == 409

    approved = client.post(f"{RAG}/guides/versions/{version_id}/approve")
    assert approved.status_code == 200
    assert approved.json()["status"] == "live"
    assert approved.json()["approved_by"] == "fa-1"

    listed = {row["version_id"]: row["status"] for row in client.get(f"{RAG}/guides").json()}
    assert listed[version_id] == "live"

    too_long = client.post(f"{RAG}/guides", json={**body, "text": "x" * (GUIDE_MAX_CHARS + 1)})
    assert too_long.status_code == 422
    assert client.post(f"{RAG}/guides/versions/999999999/approve").status_code == 404
