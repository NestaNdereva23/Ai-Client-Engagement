"""One cursor shape and one page envelope, shared by every list endpoint.

The cursor encodes (created_at, id) rather than an offset, so a row inserted
or removed while a caller pages through a list can never shift later pages or
duplicate a row across them, an offset-based cursor's usual failure mode.
"""

from __future__ import annotations

import base64
import json
from datetime import datetime
from typing import Generic, TypeVar

from pydantic import BaseModel

DEFAULT_LIMIT = 50
MAX_LIMIT = 200

T = TypeVar("T")


class Page(BaseModel, Generic[T]):
    """One page of a list endpoint's results.

    total_count is the count across every page under the same filters, not
    just this one -- optional because it costs an extra query, so a caller
    only pays for it where a total is actually shown (e.g. a queue badge).
    Left unset, it stays None rather than being silently wrong.
    """

    items: list[T]
    next_cursor: str | None = None
    total_count: int | None = None


class InvalidCursor(Exception):
    """A cursor value could not be decoded."""


def encode_cursor(created_at: datetime, row_id: str) -> str:
    payload = json.dumps([created_at.isoformat(), row_id])
    return base64.urlsafe_b64encode(payload.encode()).decode()


def decode_cursor(cursor: str) -> tuple[datetime, str]:
    try:
        created_at_raw, row_id = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
        return datetime.fromisoformat(created_at_raw), row_id
    except Exception as exc:
        raise InvalidCursor(cursor) from exc


def encode_id_cursor(row_id: int) -> str:
    """For a list keyed by a single ordered id, with no timestamp to pair it with."""
    return base64.urlsafe_b64encode(json.dumps([row_id]).encode()).decode()


def decode_id_cursor(cursor: str) -> int:
    try:
        (row_id,) = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
        return row_id
    except Exception as exc:
        raise InvalidCursor(cursor) from exc


def clamp_limit(limit: int) -> int:
    return max(1, min(limit, MAX_LIMIT))


def encode_pair_cursor(a: int, b: int) -> str:
    """For a list ordered by two integer columns (a composite key, no
    timestamp to pair them with), such as (client_id, unit_fund_id).
    """
    return base64.urlsafe_b64encode(json.dumps([a, b]).encode()).decode()


def decode_pair_cursor(cursor: str) -> tuple[int, int]:
    try:
        a, b = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
        return a, b
    except Exception as exc:
        raise InvalidCursor(cursor) from exc
