"""The embedder is deterministic, batched, and chosen in one place.

Pure tests: the same text embeds identically, related text sits closer than
unrelated text, batching matches a single pass, and the factory honours config.
"""

from __future__ import annotations

import math
import types

import pytest

from app.db.models.rag import EMBEDDING_DIM
from app.rag import embedding as emb
from app.rag.embedding import HashingEmbedder, embed_batched, get_embedder
from app.rag.retrieve import build_query, sections_for_product


def _cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


def test_embedding_is_deterministic_and_the_right_width() -> None:
    e = HashingEmbedder()
    assert e.dim == EMBEDDING_DIM
    v1 = e.embed(["Cytonn Money Market Fund yield"])[0]
    v2 = e.embed(["Cytonn Money Market Fund yield"])[0]
    assert v1 == v2
    assert len(v1) == EMBEDDING_DIM
    assert math.isclose(_cosine(v1, v1), 1.0, abs_tol=1e-6)


def test_related_text_is_closer_than_unrelated_text() -> None:
    e = HashingEmbedder()
    query, related, unrelated = e.embed(
        [
            "money market fund current yield",
            "the money market fund yield this week",
            "real estate residential supply in Nairobi",
        ]
    )
    assert _cosine(query, related) > _cosine(query, unrelated)


def test_batching_matches_a_single_pass() -> None:
    e = HashingEmbedder()
    texts = [f"chunk number {i} about yields" for i in range(5)]
    assert embed_batched(e, texts, batch_size=2) == e.embed(texts)


def test_factory_returns_the_configured_embedder() -> None:
    assert isinstance(get_embedder(), HashingEmbedder)


def test_factory_rejects_an_unknown_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        emb,
        "get_settings",
        lambda: types.SimpleNamespace(embedding_provider="bogus", embedding_model="x"),
    )
    with pytest.raises(ValueError, match="unknown embedding provider"):
        get_embedder()


def test_sections_for_product_maps_the_fund() -> None:
    assert sections_for_product("Money Market Fund") == ["Money Markets", "Investment Updates"]
    assert sections_for_product("Equity Fund") == ["Equities"]
    assert sections_for_product("Unknown Thing") is None


def test_build_query_folds_in_the_angle() -> None:
    q = build_query("Money Market Fund", angle="winback_habit")
    assert q.startswith("Money Market Fund")
    assert "rhythm" in q
    assert build_query("Money Market Fund") == "Money Market Fund"
