"""The ordered record of what happened during one agent run.

One row per thing worth showing a person: a step starting, a tool call, a
finding written, a warning, an error. The rows are written as the run goes
and read back afterwards, so watching a run live and opening a finished one
are the same view over the same rows.

The detail is structured, never a sentence to be read out. Nothing here
carries a client name or an exact figure tied to one client, the same rule
everything else the agent produces follows.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

RUN_STARTED = "run_started"
RUN_STATUS = "run_status"
RUN_COMPLETED = "run_completed"
STEP_STARTED = "step_started"
STEP_COMPLETED = "step_completed"
TOOL_STARTED = "tool_started"
TOOL_COMPLETED = "tool_completed"
INSIGHT_CREATED = "insight_created"
INSIGHT_UPDATED = "insight_updated"
INSIGHT_DISMISSED = "insight_dismissed"
PROPOSAL_CREATED = "proposal_created"
APPROVAL_NEEDED = "approval_needed"
APPROVAL_GIVEN = "approval_given"
ACTION_STARTED = "action_started"
ACTION_COMPLETED = "action_completed"
WARNING = "warning"
ERROR = "error"
PAUSED = "paused"
RESUMED = "resumed"

AGENT_EVENT_KINDS: tuple[str, ...] = (
    RUN_STARTED,
    RUN_STATUS,
    RUN_COMPLETED,
    STEP_STARTED,
    STEP_COMPLETED,
    TOOL_STARTED,
    TOOL_COMPLETED,
    INSIGHT_CREATED,
    INSIGHT_UPDATED,
    INSIGHT_DISMISSED,
    PROPOSAL_CREATED,
    APPROVAL_NEEDED,
    APPROVAL_GIVEN,
    ACTION_STARTED,
    ACTION_COMPLETED,
    WARNING,
    ERROR,
    PAUSED,
    RESUMED,
)

_KINDS_IN_SQL = ", ".join(f"'{kind}'" for kind in AGENT_EVENT_KINDS)

AGENT_EVENT_KIND_CHECK = f"kind IN ({_KINDS_IN_SQL})"


class AgentEvent(Base):
    """One thing that happened during one run, in the order it happened."""

    __tablename__ = "agent_event"
    __table_args__ = (
        UniqueConstraint("run_id", "ordinal", name="uq_agent_event_run_ordinal"),
        CheckConstraint(AGENT_EVENT_KIND_CHECK, name="ck_agent_event_kind"),
        CheckConstraint("ordinal > 0", name="ck_agent_event_ordinal_positive"),
    )

    event_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("agent_run.run_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    detail: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
