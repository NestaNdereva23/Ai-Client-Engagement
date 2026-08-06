"""Human review outcomes as ground truth for judge agreement, keyed by angle and tier.

Every terminal review_action already carries message_angle and priority_tier,
stamped at decide() time from the run it decided on (see app.services.review),
so this module's only job is pairing that label with the judge's own scores
on the same generation run: the pairing a judge-agreement analysis needs. A
run the judge never scored still appears, with null scores, because a
missing evaluation is itself a coverage gap worth seeing, not a row to drop.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.llmops import Evaluation
from app.db.models.outreach import OutreachMessage, ReviewAction

# escalate and hold are waypoints, not a reviewer's final word on a draft;
# only a terminal outcome is a ground-truth label.
_TERMINAL_OUTCOMES = ("approve", "edit_approve", "reject")


@dataclass(frozen=True)
class GroundTruthRow:
    """One terminal reviewer decision, its edit diff, and the judge's scores on the same run."""

    review_action_id: int
    message_id: str
    run_id: str
    message_angle: str | None
    priority_tier: str | None
    outcome: str
    edit_diff: dict | None
    reviewer_id: str
    tone: int | None
    compliance: int | None
    grounding: int | None
    personalization: int | None


def ground_truth_rows(
    session: Session,
    *,
    message_angle: str | None = None,
    priority_tier: str | None = None,
) -> list[GroundTruthRow]:
    """Every terminal review decision, optionally sliced by angle and/or tier,
    left-joined to its judge evaluation.
    """
    query = (
        select(ReviewAction, OutreachMessage.generation_run_id, Evaluation)
        .join(OutreachMessage, ReviewAction.message_id == OutreachMessage.message_id)
        .outerjoin(Evaluation, Evaluation.run_id == OutreachMessage.generation_run_id)
        .where(ReviewAction.outcome.in_(_TERMINAL_OUTCOMES))
        .order_by(ReviewAction.created_at)
    )
    if message_angle is not None:
        query = query.where(ReviewAction.message_angle == message_angle)
    if priority_tier is not None:
        query = query.where(ReviewAction.priority_tier == priority_tier)

    rows = session.execute(query).all()
    return [
        GroundTruthRow(
            review_action_id=action.review_action_id,
            message_id=action.message_id,
            run_id=run_id,
            message_angle=action.message_angle,
            priority_tier=action.priority_tier,
            outcome=action.outcome,
            edit_diff=action.edit_diff,
            reviewer_id=action.reviewer_id,
            tone=evaluation.tone if evaluation else None,
            compliance=evaluation.compliance if evaluation else None,
            grounding=evaluation.grounding if evaluation else None,
            personalization=evaluation.personalization if evaluation else None,
        )
        for action, run_id, evaluation in rows
    ]
