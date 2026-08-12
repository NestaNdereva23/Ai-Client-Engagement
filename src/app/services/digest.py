"""Read the persisted morning digest for one FA (or fund) group.

Serves whatever workers/digest.py already wrote; this module never builds a
digest itself, only reads one back for the API.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models.digest import DigestLine, DigestRun


class DigestNotFoundToday(Exception):
    """No digest_run has been generated yet today."""


@dataclass(frozen=True)
class DigestGroupView:
    """One group's lines from today's digest, plus how many were left off."""

    digest_run_id: int
    risk_run_id: str
    generated_at: datetime
    group_key: str
    total_eligible: int
    overflow_count: int
    lines: list[DigestLine]


def latest_digest_run_for_today(session: Session) -> DigestRun | None:
    """The most recently generated digest_run whose date matches today, in
    the database's own time zone -- never the caller's clock.
    """
    return session.scalar(
        select(DigestRun)
        .where(func.date(DigestRun.generated_at) == func.current_date())
        .order_by(DigestRun.generated_at.desc(), DigestRun.digest_run_id.desc())
        .limit(1)
    )


def get_today_digest_group(session: Session, group_key: str) -> DigestGroupView:
    """Today's digest lines for one group, capped exactly as persisted.

    Raises DigestNotFoundToday when no digest has been generated yet today
    at all -- a group with no at-risk clients today is a different, valid
    fact (an empty list), not a missing digest.
    """
    run = latest_digest_run_for_today(session)
    if run is None:
        raise DigestNotFoundToday(group_key)

    lines = list(
        session.scalars(
            select(DigestLine)
            .where(DigestLine.digest_run_id == run.digest_run_id, DigestLine.group_key == group_key)
            .order_by(DigestLine.rank)
        )
    )
    total_eligible = lines[0].group_total if lines else 0
    overflow_count = max(total_eligible - len(lines), 0)

    return DigestGroupView(
        digest_run_id=run.digest_run_id,
        risk_run_id=run.risk_run_id,
        generated_at=run.generated_at,
        group_key=group_key,
        total_eligible=total_eligible,
        overflow_count=overflow_count,
        lines=lines,
    )
