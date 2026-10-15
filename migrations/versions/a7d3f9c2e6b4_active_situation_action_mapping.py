"""situation_action_mapping 6: the eight active situations, and suggest only for their actions
The old mappings are not carried across."""

from collections.abc import Sequence
from datetime import date

import sqlalchemy as sa
from alembic import op
from sqlalchemy.orm import Session

from app.agents.permissions import set_permission
from app.agents.situation_action_mapping import MappingSpec, save_situation_action_mapping_version
from app.db.models.agent_permission import AgentPermission

revision: str = "a7d3f9c2e6b4"
down_revision: str | Sequence[str] | None = "f4a9c2d7b1e8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_VERSION = 6
_PREVIOUS = 5
_VALID_FROM = date(2026, 10, 13)
_CHANGED_BY = "active_tiers"
_SUGGEST_ONLY = "suggest_only"

_MAPPINGS = [
    MappingSpec(
        situation="gradual_depositor_due",
        action_code="give_advisor_a_task",
        objective="encourage",
        angle="your_next_deposit",
        evidence_required="No recent purchase, no real withdrawal, and past their own usual gap",
        channel="email",
    ),
    MappingSpec(
        situation="gradual_depositor_withdrawing",
        action_code="give_advisor_a_task",
        objective="protect",
        angle="sitting_still",
        evidence_required="No recent purchase, and a real withdrawal in the review period",
        channel="email",
    ),
    MappingSpec(
        situation="frequent_depositor_one_fund",
        action_code="suggest_second_fund",
        objective="encourage",
        angle="suggest_second_fund",
        evidence_required="A recent purchase, no real withdrawal, and exactly one fund held",
        channel="email",
    ),
    MappingSpec(
        situation="frequent_depositor_withdrawing",
        action_code="send_learning_note",
        objective="educate",
        angle="send_learning_note",
        evidence_required="A recent purchase, a real withdrawal, and withdrawals matching deposits",
        channel="email",
    ),
    MappingSpec(
        situation="deposit_size_falling",
        action_code="ask_what_changed",
        objective="clarify",
        angle="ask_what_changed",
        evidence_required="The shrinking deposits flag, for a client in the active book",
        channel="email",
    ),
    MappingSpec(
        situation="first_deposit_no_repeat",
        action_code="welcome_and_top_up",
        objective="welcome",
        angle="welcome_and_top_up",
        evidence_required="Exactly one deposit, inside the new client window",
        channel="email",
    ),
    MappingSpec(
        situation="fa_call_overdue",
        action_code="follow_up_when_no_one_called",
        objective="follow_up",
        angle="follow_up_when_no_one_called",
        evidence_required="An open call the advisor scheduled that is past its date",
        channel="email",
    ),
    MappingSpec(
        situation="risk_rose_no_call",
        action_code="check_in_rising_risk",
        objective="protect",
        angle="sitting_still",
        evidence_required="A worse risk band in the latest run with no response since",
        channel="email",
    ),
]


def _make_suggest_only(session: Session, action_code: str) -> None:
    row = (
        session.query(AgentPermission)
        .filter_by(action_code=action_code, priority_tier=None, risk_band=None)
        .one_or_none()
    )
    if row is not None and row.permission == _SUGGEST_ONLY:
        return
    reason = (
        "new active situations are suggest only"
        if row is None
        else f"was {row.permission} before the active tiers change"
    )
    set_permission(
        session, action_code, _SUGGEST_ONLY, changed_by=_CHANGED_BY, changed_reason=reason
    )


def upgrade() -> None:
    session = Session(bind=op.get_bind())
    save_situation_action_mapping_version(session, _VERSION, _MAPPINGS, valid_from=_VALID_FROM)
    for action_code in sorted({spec.action_code for spec in _MAPPINGS}):
        _make_suggest_only(session, action_code)
    session.flush()


def downgrade() -> None:
    op.execute(
        sa.text("DELETE FROM situation_action_mapping WHERE version = :version").bindparams(
            version=_VERSION
        )
    )
    op.execute(
        sa.text(
            "UPDATE situation_action_mapping SET valid_to = NULL "
            "WHERE version = :previous AND valid_to = :valid_from"
        ).bindparams(previous=_PREVIOUS, valid_from=_VALID_FROM)
    )
    op.execute(
        sa.text(
            "UPDATE active_configuration SET active_version = :previous "
            "WHERE component_type = 'situation_action_mapping' AND active_version = :version"
        ).bindparams(previous=_PREVIOUS, version=_VERSION)
    )
    op.execute(
        sa.text(
            "UPDATE agent_permission "
            "SET permission = split_part(changed_reason, ' ', 2), changed_by = NULL, "
            "changed_reason = NULL "
            "WHERE changed_by = :by AND changed_reason LIKE 'was %'"
        ).bindparams(by=_CHANGED_BY)
    )
    op.execute(
        sa.text("DELETE FROM agent_permission WHERE changed_by = :by").bindparams(by=_CHANGED_BY)
    )
