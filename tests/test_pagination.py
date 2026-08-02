"""The shared cursor helpers: pure, no database needed."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.pagination import (
    InvalidCursor,
    clamp_limit,
    decode_cursor,
    decode_id_cursor,
    encode_cursor,
    encode_id_cursor,
)


def test_encode_decode_cursor_round_trips() -> None:
    when = datetime(2026, 7, 30, 12, 0, 0, tzinfo=UTC)
    cursor = encode_cursor(when, "abc123")
    assert decode_cursor(cursor) == (when, "abc123")


def test_decode_cursor_rejects_garbage() -> None:
    with pytest.raises(InvalidCursor):
        decode_cursor("not-a-real-cursor")


def test_decode_cursor_rejects_a_well_formed_but_wrong_shaped_payload() -> None:
    import base64
    import json

    bad = base64.urlsafe_b64encode(json.dumps(["only-one-part"]).encode()).decode()
    with pytest.raises(InvalidCursor):
        decode_cursor(bad)


def test_encode_decode_id_cursor_round_trips() -> None:
    cursor = encode_id_cursor(42)
    assert decode_id_cursor(cursor) == 42


def test_decode_id_cursor_rejects_garbage() -> None:
    with pytest.raises(InvalidCursor):
        decode_id_cursor("not-a-real-cursor")


def test_clamp_limit_keeps_a_value_in_range() -> None:
    assert clamp_limit(10) == 10


def test_clamp_limit_floors_at_one() -> None:
    assert clamp_limit(0) == 1
    assert clamp_limit(-5) == 1


def test_clamp_limit_caps_at_max() -> None:
    assert clamp_limit(10_000) == 200
