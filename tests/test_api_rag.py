"""The RAG admin console API: upload a report, manage versions, debug retrieval.

_minimal_pdf hand-builds a tiny, valid single-page PDF with a real content
stream (correct byte offsets computed, not guessed) so the upload endpoint
can be driven with real multipart bytes end to end, through the same
pypdf-based loader a real weekly report goes through, without needing an
actual report file on disk.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.db.models.rag import RagChunk, RagDocument, RagDocumentVersion
from app.db.session import SessionLocal
from app.main import app

client = TestClient(app)

RAG = "/api/v1/rag"


@pytest.fixture(autouse=True)
def _authed(configured_reviewers, reviewer_1_headers):
    client.headers.update(reviewer_1_headers)
    yield
    client.headers.pop("Authorization", None)


def test_missing_token_is_401(configured_reviewers) -> None:
    response = TestClient(app).get(f"{RAG}/versions")
    assert response.status_code == 401


def test_no_reviewer_configured_is_503(unconfigured_reviewers, reviewer_1_headers) -> None:
    response = TestClient(app).get(f"{RAG}/versions", headers=reviewer_1_headers)
    assert response.status_code == 503


_SOURCE = "api-test-weekly"


def _minimal_pdf(text: str) -> bytes:
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /Resources << /Font << /F1 4 0 R >> >> "
        b"/MediaBox [0 0 612 792] /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    stream = f"BT /F1 12 Tf 72 712 Td ({text}) Tj ET".encode()
    objects.append(
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream"
    )

    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for i, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref_offset = len(out)
    n = len(objects) + 1
    out += f"xref\n0 {n}\n".encode() + b"0000000000 65535 f \n"
    for off in offsets[1:]:
        out += f"{off:010d} 00000 n \n".encode()
    out += b"trailer\n<< /Size " + str(n).encode() + b" /Root 1 0 R >>\nstartxref\n"
    out += str(xref_offset).encode() + b"\n%%EOF"
    return bytes(out)


def _purge(source: str) -> None:
    with SessionLocal() as session:
        doc = session.scalar(select(RagDocument).where(RagDocument.source == source))
        if not doc:
            return
        vids = session.scalars(
            select(RagDocumentVersion.version_id).where(RagDocumentVersion.doc_id == doc.doc_id)
        ).all()
        session.execute(delete(RagChunk).where(RagChunk.version_id.in_(vids)))
        session.execute(delete(RagDocumentVersion).where(RagDocumentVersion.doc_id == doc.doc_id))
        session.execute(delete(RagDocument).where(RagDocument.doc_id == doc.doc_id))
        session.commit()


@pytest.fixture
def clean(db: None):
    _purge(_SOURCE)
    yield
    _purge(_SOURCE)


def _upload(text: str, source: str = _SOURCE):
    pdf = _minimal_pdf(text)
    return client.post(
        f"{RAG}/reports",
        files={"file": ("report.pdf", pdf, "application/pdf")},
        data={"document_source": source},
    )


def test_upload_creates_a_new_active_version(clean) -> None:
    response = _upload("Cytonn Weekly Report. Cytonn Weekly 29/2026. Money Markets: 11.35 percent.")
    assert response.status_code == 201
    body = response.json()
    assert body["created"] is True
    assert body["version_no"] == 1
    assert body["chunks"] > 0


def test_upload_without_an_issue_tag_is_a_422(clean) -> None:
    response = _upload("A report with no Cytonn Weekly issue tag anywhere in it.")
    assert response.status_code == 422


def test_reuploading_the_same_issue_refreshes_in_place(clean) -> None:
    first = _upload("Cytonn Weekly Report. Cytonn Weekly 31/2026. Money Markets: 11.00 percent.")
    again = _upload("Cytonn Weekly Report. Cytonn Weekly 31/2026. Money Markets: 11.20 percent.")
    assert again.json()["created"] is False
    assert again.json()["version_id"] == first.json()["version_id"]


def test_get_versions_lists_the_uploaded_report(clean) -> None:
    uploaded = _upload("Cytonn Weekly Report. Cytonn Weekly 32/2026. Money Markets: 10.90 percent.")
    version_id = uploaded.json()["version_id"]

    response = client.get(f"{RAG}/versions")
    assert response.status_code == 200
    rows = [r for r in response.json() if r["version_id"] == version_id]
    assert len(rows) == 1
    assert rows[0]["issue"] == "32/2026"
    assert rows[0]["is_active"] is True


def test_activating_an_older_version_restores_it(clean) -> None:
    first = _upload("Cytonn Weekly Report. Cytonn Weekly 33/2026. Money Markets: 9.50 percent.")
    second = _upload("Cytonn Weekly Report. Cytonn Weekly 34/2026. Money Markets: 9.60 percent.")
    first_id = first.json()["version_id"]
    second_id = second.json()["version_id"]

    response = client.post(f"{RAG}/versions/{first_id}/activate")
    assert response.status_code == 200
    assert response.json()["is_active"] is True

    versions = {r["version_id"]: r["is_active"] for r in client.get(f"{RAG}/versions").json()}
    assert versions[first_id] is True
    assert versions[second_id] is False


def test_activate_404s_for_an_unknown_version(db: None) -> None:
    response = client.post(f"{RAG}/versions/999999999/activate")
    assert response.status_code == 404


def test_search_with_q_finds_the_uploaded_fact(clean) -> None:
    _upload("Cytonn Weekly Report. Cytonn Weekly 35/2026. Money Markets: a distinctive 7.77 rate.")
    response = client.get(f"{RAG}/search", params={"q": "distinctive 7.77 percent rate"})
    assert response.status_code == 200
    results = response.json()
    assert results
    assert "7.77" in results[0]["text"]


def test_search_requires_product_or_q(db: None) -> None:
    response = client.get(f"{RAG}/search")
    assert response.status_code == 422
