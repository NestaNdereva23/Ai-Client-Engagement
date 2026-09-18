from __future__ import annotations

from app.audit.log import record_audit
from app.campaigns.touch import SendBlocked
from app.db.session import restricted_session
from app.delivery.test_list import active_test_contacts


def pick_test_recipient(client_id: int, channel: str) -> str:
    with restricted_session() as session:
        contacts = active_test_contacts(session, channel)
        if not contacts:
            raise SendBlocked("no_test_recipient")
        # The same client always lands on the same tester, so a whole sequence reads in one inbox.
        to = contacts[client_id % len(contacts)]
        record_audit(
            session,
            entity_type="test_recipient",
            action="read",
            entity_id=str(client_id),
            detail={"purpose": "test_send_redirect", "channel": channel},
        )
        session.commit()
        return to


def ensure_test_recipient(to: str, channel: str) -> None:
    with restricted_session() as session:
        allowed = active_test_contacts(session, channel)
    if to not in allowed:
        raise SendBlocked("recipient_not_on_test_list")
