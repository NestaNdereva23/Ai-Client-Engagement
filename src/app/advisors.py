"""An advisor as the nightly run and the digest see them."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FaRecord:
    """One account manager, read from a user_access row.

    fa_id is the id their clients are filed under. It is the email unless the
    row sets another one."""

    fa_id: str
    name: str
    email: str
    daily_capacity: int
