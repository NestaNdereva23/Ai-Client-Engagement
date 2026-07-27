"""Retrieve product facts for a client's product and message angle.

The product picks the report sections to search and the angle shapes the query
text; candidates are then ranked by embedding similarity. The query is embedded
with the same model that indexed the corpus. Only the active version is served.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.rag import RagChunk, RagDocumentVersion
from app.rag.embedding import Embedder, get_embedder

# Which report sections carry a product's facts.
_PRODUCT_SECTIONS: dict[str, list[str]] = {
    "money market": ["Money Markets", "Investment Updates"],
    "equity": ["Equities"],
    "real estate": ["Real Estate"],
    "balanced": ["Money Markets", "Equities", "Fixed Income"],
    "fixed income": ["Fixed Income"],
}
# How a message angle colours the retrieval query.
_ANGLE_INTENT: dict[str, str] = {
    "winback_habit": "resume a regular investing rhythm current yield returns",
    "winback_flexible": "flexible low-pressure return options current yield",
}


@dataclass(frozen=True)
class Retrieved:
    chunk_id: int
    text: str
    metadata: dict
    score: float
    version_id: int


def sections_for_product(product: str) -> list[str] | None:
    p = product.lower()
    for key, sections in _PRODUCT_SECTIONS.items():
        if key in p:
            return sections
    return None


def build_query(product: str, angle: str | None = None, extra: str | None = None) -> str:
    parts = [product]
    if angle and angle in _ANGLE_INTENT:
        parts.append(_ANGLE_INTENT[angle])
    if extra:
        parts.append(extra)
    return " ".join(parts)


def retrieve(
    session: Session,
    query: str,
    *,
    sections: list[str] | None = None,
    active_only: bool = True,
    k: int = 5,
    embedder: Embedder | None = None,
) -> list[Retrieved]:
    """Rank chunks by similarity to the query, filtered by section and active version."""
    embedder = embedder or get_embedder()
    query_vec = embedder.embed([query])[0]
    distance = RagChunk.embedding.cosine_distance(query_vec)

    stmt = select(RagChunk, distance.label("distance")).where(RagChunk.embedding.isnot(None))
    if active_only:
        stmt = stmt.join(
            RagDocumentVersion, RagChunk.version_id == RagDocumentVersion.version_id
        ).where(RagDocumentVersion.is_active.is_(True))
    if sections:
        stmt = stmt.where(RagChunk.chunk_metadata["section"].astext.in_(sections))
    stmt = stmt.order_by(distance).limit(k)

    return [
        Retrieved(
            chunk_id=chunk.chunk_id,
            text=chunk.text,
            metadata=chunk.chunk_metadata or {},
            score=1.0 - float(dist),
            version_id=chunk.version_id,
        )
        for chunk, dist in session.execute(stmt).all()
    ]


def retrieve_product_facts(
    session: Session,
    *,
    product: str,
    angle: str | None = None,
    k: int = 5,
    active_only: bool = True,
    embedder: Embedder | None = None,
) -> list[Retrieved]:
    """Retrieve facts for a client's product and angle: filter by section, rank by similarity."""
    return retrieve(
        session,
        build_query(product, angle),
        sections=sections_for_product(product),
        active_only=active_only,
        k=k,
        embedder=embedder,
    )
