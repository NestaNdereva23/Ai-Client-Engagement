"""Who may see client data, and whose book they see: user_access.

One row per person, keyed by the email in Ticketing's signed token. An advisor
sees their own book. An admin sees every book, and can set view_as_email to
see one advisor's book as that advisor sees it. A person with no row, or with
active switched off, sees no client data.

The advisor rows are also the list of account managers the nightly run shares
clients out to: each client goes to the row whose email matches the account
manager email in the feed.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, Integer, Text, func
from sqlalchemy.orm import Mapped, mapped_column, validates

from app.db.base import Base

ROLE_ADVISOR = "advisor"
ROLE_ADMIN = "admin"
USER_ROLES = (ROLE_ADVISOR, ROLE_ADMIN)

DEFAULT_DAILY_CAPACITY = 19

SOURCE_AUTO = "auto"
SOURCE_MANUAL = "manual"
ACCESS_SOURCES = (SOURCE_AUTO, SOURCE_MANUAL)


def _quoted(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def normalize_email(email: str) -> str:
    return email.strip().lower()


class UserAccess(Base):
    """One person who can log in, with their role and what they see.

    fa_id is the id an advisor's clients are filed under. Leave it empty to use
    the email. source says who made the row: the nightly run adds advisors as
    auto rows and never changes an existing row. Editing a row in the admin
    screen makes it manual.
    """

    __tablename__ = "user_access"
    __table_args__ = (
        CheckConstraint(f"role IN ({_quoted(USER_ROLES)})", name="ck_user_access_role"),
        CheckConstraint(f"source IN ({_quoted(ACCESS_SOURCES)})", name="ck_user_access_source"),
    )

    email: Mapped[str] = mapped_column(Text, primary_key=True)
    role: Mapped[str] = mapped_column(Text, nullable=False, server_default=ROLE_ADVISOR)
    name: Mapped[str | None] = mapped_column(Text, nullable=True)
    fa_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    daily_capacity: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=str(DEFAULT_DAILY_CAPACITY)
    )
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    receives_digest: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    view_as_email: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(Text, nullable=False, server_default=SOURCE_MANUAL)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    @validates("email", "view_as_email")
    def _store_lowercase(self, _key: str, value: str | None) -> str | None:
        cleaned = normalize_email(value) if value else None
        return cleaned or None
