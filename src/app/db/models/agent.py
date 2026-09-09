"""The versioned list of actions the proactive agent may take.

Built like the message angle catalogue and never edited in place: a change
ships as a new version with its own validity window, so a decision taken
months ago can still be read against the exact wording that produced it.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    Integer,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

# How much of the message teaches and how much of it asks for something.
CONTENT_MIXES = ("learning_only", "mostly_learning", "balanced", "mostly_ask")

# How much freedom an action has, from weakest to strongest.
PERMISSION_LEVELS = ("suggest_only", "approve_each", "approve_sample", "act_alone")

# The sorts of thing the business can actually do about a finding. The code
# that carries an action out reads this rather than matching on action codes,
# so a new action of a sort that already works needs no new branch.
RESPONSE_KINDS = (
    "automated_email",
    "advisor_task",
    "phone_call",
    "campaign_enrolment",
    "product_teaching",
    "monitor_only",
    "escalate",
    "change_client_state",
    "ask_a_person_first",
)

# The sorts of response that reach the client. The rest are internal: a task,
# an escalation, a change of handling, a note to watch. Only these run the
# checks about contacting somebody.
CONTACTING_RESPONSE_KINDS = (
    "automated_email",
    "phone_call",
    "campaign_enrolment",
    "product_teaching",
)


class AgentActionCatalog(Base):
    """One action the agent may propose, as it stood in one catalogue version."""

    __tablename__ = "agent_action_catalog"
    __table_args__ = (
        UniqueConstraint(
            "version", "action_code", name="uq_agent_action_catalog_version_action_code"
        ),
        CheckConstraint(
            "content_mix IN ('learning_only', 'mostly_learning', 'balanced', 'mostly_ask')",
            name="ck_agent_action_catalog_content_mix",
        ),
        CheckConstraint(
            "default_permission IN ('suggest_only', 'approve_each', 'approve_sample', 'act_alone')",
            name="ck_agent_action_catalog_default_permission",
        ),
        CheckConstraint(
            "money_ceiling_kes IS NULL OR money_ceiling_kes > 0",
            name="ck_agent_action_catalog_money_ceiling_positive",
        ),
        CheckConstraint(
            "response_kind IN ('automated_email', 'advisor_task', 'phone_call', "
            "'campaign_enrolment', 'product_teaching', 'monitor_only', 'escalate', "
            "'change_client_state', 'ask_a_person_first')",
            name="ck_agent_action_catalog_response_kind",
        ),
    )

    catalog_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    # The identifier a proposal records, for example welcome_and_top_up.
    action_code: Mapped[str] = mapped_column(Text, nullable=False)
    # A short name a person reads on the screen.
    title: Mapped[str] = mapped_column(Text, nullable=False)
    # Which sort of response this is, from RESPONSE_KINDS.
    response_kind: Mapped[str] = mapped_column(Text, nullable=False)
    # Which clients the action is for, in plain words.
    who: Mapped[str] = mapped_column(Text, nullable=False)
    # What must be true in the data before the action may be chosen.
    evidence_required: Mapped[str] = mapped_column(Text, nullable=False)
    # The angle the message is written to. Null for an action that sends nothing.
    message_angle: Mapped[str | None] = mapped_column(Text, nullable=True)
    # How the message goes out. Null for an action that sends nothing.
    channel: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_mix: Mapped[str] = mapped_column(Text, nullable=False)
    default_permission: Mapped[str] = mapped_column(Text, nullable=False)
    # Money in the group above which the action may never run on its own.
    # Null means there is no ceiling because the action moves no money.
    money_ceiling_kes: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Stops one action without a release. Everything else in the version stays live.
    paused: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    valid_from: Mapped[date] = mapped_column(Date, nullable=False)
    valid_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
