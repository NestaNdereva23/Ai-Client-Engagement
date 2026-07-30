"""Request and response shapes for the RAG admin console endpoints."""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel


class RagIngestOut(BaseModel):
    """What a report upload produced."""

    doc_id: int
    version_id: int
    version_no: int
    chunks: int
    created: bool


class RagVersionOut(BaseModel):
    """One ingested version, with its document's title."""

    version_id: int
    doc_id: int
    document_title: str
    version_no: int
    issue: str | None
    published_on: date | None
    is_active: bool
    ingested_at: datetime


class RetrievedChunkOut(BaseModel):
    chunk_id: int
    text: str
    metadata: dict
    score: float
    version_id: int
