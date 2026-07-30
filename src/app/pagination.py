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
    """One page of a list endpoint's results."""

    items: list[T]
    next_cursor: str | None = None


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


def clamp_limit(limit: int) -> int:
    return max(1, min(limit, MAX_LIMIT))
