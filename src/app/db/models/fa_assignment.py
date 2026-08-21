"""Account manager (FA) assignment: fa_assignment.

Every row from the stub source has fa_id = NULL; the digest builder falls
back to grouping by fund whenever fa_id is null. No row is written by this
milestone -- StubFaAssignmentSource answers from active_client_fund
directly -- this table is where a real source's assignments will land.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class FaAssignment(Base):
    """One client-fund's assignment to an account manager."""

    __tablename__ = "fa_assignment"

    client_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    unit_fund_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    # The advisor's login username on the console calling this API, not an
    # arbitrary roster number -- see config.FaRecord.
    fa_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Display only, never sent to a model.
    fa_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    # "stub" today; a real source name once one exists.
    source: Mapped[str] = mapped_column(Text, nullable=False, server_default="stub")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
