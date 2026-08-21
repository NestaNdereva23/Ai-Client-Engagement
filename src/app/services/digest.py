"""Read the persisted morning digest for one FA (or fund) group.

Serves whatever workers/digest.py already wrote; this module never builds a
digest itself, only reads one back for the API.

Every eligible row is persisted (see digest/build.py), split into
cap_per_group-sized batches. This module decides, live, how many of those
batches a caller actually gets to see: batch 0 always, and each later batch
only once every row in the one before it has been acted on -- a call logged,
a snooze, a dismiss, or an email sent. That way an FA (or, for a fund with no
real owner yet, whichever FA is free) who works through their first batch is
simply handed the next one, instead of the rest of the eligible book staying
invisible until tomorrow's run.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models.active_clients import ActiveClientInteraction
from app.db.models.digest import DigestLine, DigestRun
from app.digest.build import is_deprioritized, latest_interactions_for
from app.services.briefing import briefing_available_keys


class DigestNotFoundToday(Exception):
    """No digest_run has been generated yet today."""


@dataclass(frozen=True)
class DigestLineView:
    """One persisted digest_line, plus briefing_available and deprioritized
    -- both computed live rather than served as persisted, since they
    answer "is this true right now", not "was this true when the digest
    was built". briefing_available checks the current
    client_risk_features/active_client_fund state; deprioritized checks
    whatever has been logged in active_client_interaction so far today, so
    a call logged five minutes ago is reflected immediately.
    """

    client_id: int
    unit_fund_id: int
    rank: int
    batch: int
    risk_score: int
    risk_band: str
    risk_reasons: str
    risk_reason_tags: list[str]
    fund_at_risk: float
    score_delta: int | None
    route: str
    in_call_queue: bool
    complaint_caveat: bool
    deprioritized: bool
    briefing_available: bool


@dataclass(frozen=True)
class DigestGroupView:
    """One group's currently unlocked lines from today's digest, plus how
    many are still waiting behind an unfinished batch (overflow_count).
    That count shrinks through the day as batches clear -- it does not
    mean "hidden for good".
    """

    digest_run_id: int
    risk_run_id: str
    generated_at: datetime
    group_key: str
    total_eligible: int
    overflow_count: int
    total_fund_at_risk: float
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


def _unlocked_rows(
    rows: list[DigestLine], touched: dict[tuple[int, int], ActiveClientInteraction]
) -> list[DigestLine]:
    """Batch 0, plus every later batch up to and including the first one
    not yet fully worked.

    A batch is worked once every row in it is_deprioritized -- touched, and
    not escalated back to needing attention since. Batches are walked in
    order and the first unfinished one is still included (that is the one
    an FA is currently on), just nothing after it -- so the visible list
    grows a whole batch at a time as the group gets worked through.
    """
    if not rows:
        return []
    by_batch: dict[int, list[DigestLine]] = defaultdict(list)
    for row in rows:
        by_batch[row.batch].append(row)

    visible: list[DigestLine] = []
    for batch_num in sorted(by_batch):
        batch_rows = by_batch[batch_num]
        visible.extend(batch_rows)
        if not all(is_deprioritized(row, touched) for row in batch_rows):
            break
    return visible


def get_today_digest_group(session: Session, group_key: str) -> DigestGroupView:
    """Today's unlocked digest lines for one group -- batch 0 plus however
    many later batches have since been fully worked (see _unlocked_rows).

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
    total_fund_at_risk = rows[0].group_fund_value_total if rows else 0.0

    touched = latest_interactions_for(session, [(r.client_id, r.unit_fund_id) for r in rows])
    visible_rows = _unlocked_rows(rows, touched)
    overflow_count = max(total_eligible - len(visible_rows), 0)

    available = briefing_available_keys(
        session, [(r.client_id, r.unit_fund_id) for r in visible_rows]
    )
    lines = [
        DigestLineView(
            client_id=r.client_id,
            unit_fund_id=r.unit_fund_id,
            rank=r.rank,
            batch=r.batch,
            risk_score=r.risk_score,
            risk_band=r.risk_band,
            risk_reasons=r.risk_reasons,
            risk_reason_tags=list(r.risk_reason_tags),
            fund_at_risk=r.fund_at_risk,
            score_delta=r.score_delta,
            route=r.route,
            in_call_queue=r.in_call_queue,
            complaint_caveat=r.complaint_caveat,
            deprioritized=is_deprioritized(r, touched),
            briefing_available=(r.client_id, r.unit_fund_id) in available,
        )
        for r in visible_rows
    ]

    return DigestGroupView(
        digest_run_id=run.digest_run_id,
        risk_run_id=run.risk_run_id,
        generated_at=run.generated_at,
        group_key=group_key,
        total_eligible=total_eligible,
        overflow_count=overflow_count,
        total_fund_at_risk=total_fund_at_risk,
        lines=lines,
    )
