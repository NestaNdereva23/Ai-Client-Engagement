from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.test_recipient import TestRecipient

SMS_CHANNEL = "sms"


def active_test_contacts(session: Session, channel: str) -> list[str]:
    column = TestRecipient.phone if channel == SMS_CHANNEL else TestRecipient.email
    rows = session.execute(
        select(column)
        .where(TestRecipient.active.is_(True), column.is_not(None))
        .order_by(TestRecipient.recipient_id)
    )
    return list(rows.scalars())
