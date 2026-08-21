"""Reviewer console accounts: reviewer_user.

One row per person who can log in to the reviewer console. Replaces
X-Reviewer-Key (app.api.reviewer_auth) as the real login for a human;
that header stays as the stopgap for programmatic callers of the JSON
review API. See app.auth for hashing and session handling.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, Integer, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

REVIEWER_ROLES = ("fa", "reviewer", "team_lead", "admin", "relationship_manager")


class ReviewerUser(Base):
    """One login: a username, a salted password hash, and one role.

    active gates login without deleting the row, so a departed
    reviewer's past review_action rows still resolve to a real name.
    """

    __tablename__ = "reviewer_user"
    __table_args__ = (
        CheckConstraint(
            "role IN ('fa', 'reviewer', 'team_lead', 'admin', 'relationship_manager')",
            name="ck_reviewer_user_role",
        ),
    )

    user_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(Text, nullable=False, unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(Text, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
