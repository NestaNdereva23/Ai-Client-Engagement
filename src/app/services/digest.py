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
from app.services.briefing import briefing_available_keys


class DigestNotFoundToday(Exception):
    """No digest_run has been generated yet today."""


@dataclass(frozen=True)
class DigestLineView:
    """One persisted digest_line, plus briefing_available -- computed live
    against the current client_risk_features/active_client_fund state
    rather than persisted, since it answers "can a briefing render right
    now", not "could one have rendered when this digest was built".
    """

    client_id: int
    unit_fund_id: int
    rank: int
    risk_score: int
    risk_band: str
    risk_reasons: str
    risk_reason_tags: list[str]
    aum_at_risk: float
    score_delta: int | None
    route: str
    in_call_queue: bool
    complaint_caveat: bool
    briefing_available: bool


@dataclass(frozen=True)
class DigestGroupView:
    """One group's lines from today's digest, plus how many were left off."""

    digest_run_id: int
    risk_run_id: str
    generated_at: datetime
    group_key: str
    total_eligible: int
    overflow_count: int
    total_aum_at_risk: float
    lines: list[DigestLineView]


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

    rows = list(
        session.scalars(
            select(DigestLine)
            .where(DigestLine.digest_run_id == run.digest_run_id, DigestLine.group_key == group_key)
            .order_by(DigestLine.rank)
        )
    )
    total_eligible = rows[0].group_total if rows else 0
    total_aum_at_risk = rows[0].group_aum_total if rows else 0.0
    overflow_count = max(total_eligible - len(rows), 0)

    available = briefing_available_keys(session, [(r.client_id, r.unit_fund_id) for r in rows])
    lines = [
        DigestLineView(
            client_id=r.client_id,
            unit_fund_id=r.unit_fund_id,
            rank=r.rank,
            risk_score=r.risk_score,
            risk_band=r.risk_band,
            risk_reasons=r.risk_reasons,
            risk_reason_tags=list(r.risk_reason_tags),
            aum_at_risk=r.aum_at_risk,
            score_delta=r.score_delta,
            route=r.route,
            in_call_queue=r.in_call_queue,
            complaint_caveat=r.complaint_caveat,
            briefing_available=(r.client_id, r.unit_fund_id) in available,
        )
        for r in rows
    ]

    return DigestGroupView(
        digest_run_id=run.digest_run_id,
        risk_run_id=run.risk_run_id,
        generated_at=run.generated_at,
        group_key=group_key,
        total_eligible=total_eligible,
        overflow_count=overflow_count,
        total_aum_at_risk=total_aum_at_risk,
        lines=lines,
    )
