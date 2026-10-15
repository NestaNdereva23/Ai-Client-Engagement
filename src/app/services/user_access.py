"""Who may see client data and who gets clients: lookups, the advisor link from
the feed, view as, and the digest recipients."""

from __future__ import annotations

from sqlalchemy import func, literal, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.advisors import FaRecord
from app.audit.log import record_audit
from app.config import Settings
from app.db.models.active_clients import ActiveClientFund
from app.db.models.routing import ClientSide
from app.db.models.user_access import (
    ROLE_ADMIN,
    ROLE_ADVISOR,
    SOURCE_AUTO,
    UserAccess,
    normalize_email,
)
from app.routing.sides import SIDE_ACTIVE

ENTITY_TYPE = "user_access"
VIEW_AS_SET = "view_as_set"
VIEW_AS_CLEARED = "view_as_cleared"


class NotAnAdmin(PermissionError):
    pass


class NotAnAdvisor(LookupError):
    pass


def find_access(session: Session, email: str) -> UserAccess | None:
    return session.get(UserAccess, normalize_email(email))


def advisor_fa_id(row: UserAccess) -> str:
    """The id this advisor's clients are filed under: the row's own, else the email."""
    return row.fa_id or row.email


def advisor_record(row: UserAccess) -> FaRecord:
    return FaRecord(
        fa_id=advisor_fa_id(row),
        name=row.name or row.email,
        email=row.email,
        daily_capacity=row.daily_capacity,
    )


def list_active_advisors(session: Session) -> list[UserAccess]:
    return list(
        session.scalars(
            select(UserAccess)
            .where(UserAccess.role == ROLE_ADVISOR, UserAccess.active.is_(True))
            .order_by(UserAccess.email)
        )
    )


def load_advisors(session: Session) -> list[FaRecord]:
    """Every active advisor, as the nightly run shares clients out to them."""
    return [advisor_record(row) for row in list_active_advisors(session)]


def link_advisors_from_feed(session: Session) -> int:
    """Add an advisor row for every account manager email that has clients on the
    active side, named from the feed. A row that already exists is never changed,
    so an admin row or a manual edit is safe. Returns how many rows were added."""
    email = func.lower(func.btrim(ActiveClientFund.fa_email))
    feed_advisors = (
        select(
            email, literal(ROLE_ADVISOR), literal(SOURCE_AUTO), func.max(ActiveClientFund.fa_name)
        )
        .join(ClientSide, ClientSide.client_id == ActiveClientFund.client_id)
        .where(
            ClientSide.side == SIDE_ACTIVE,
            ActiveClientFund.fa_email.is_not(None),
            func.btrim(ActiveClientFund.fa_email) != "",
        )
        .group_by(email)
    )
    added = session.execute(
        insert(UserAccess)
        .from_select(["email", "role", "source", "name"], feed_advisors)
        .on_conflict_do_nothing(index_elements=["email"])
        .returning(UserAccess.email)
    ).all()
    return len(added)


def set_view_as(session: Session, admin_email: str, advisor_email: str) -> UserAccess:
    """Make this admin see one advisor's book. Records who did it and for whom."""
    admin = _require_admin(session, admin_email)
    advisor = find_access(session, advisor_email)
    if advisor is None or advisor.role != ROLE_ADVISOR or not advisor.active:
        raise NotAnAdvisor(advisor_email)
    previous = admin.view_as_email
    admin.view_as_email = advisor.email
    _audit_swap(session, admin, VIEW_AS_SET, previous, advisor.email)
    return admin


def clear_view_as(session: Session, admin_email: str) -> UserAccess:
    admin = _require_admin(session, admin_email)
    previous = admin.view_as_email
    admin.view_as_email = None
    _audit_swap(session, admin, VIEW_AS_CLEARED, previous, None)
    return admin


def _require_admin(session: Session, email: str) -> UserAccess:
    row = find_access(session, email)
    if row is None or row.role != ROLE_ADMIN or not row.active:
        raise NotAnAdmin(email)
    return row


def _audit_swap(
    session: Session, admin: UserAccess, action: str, previous: str | None, current: str | None
) -> None:
    record_audit(
        session,
        entity_type=ENTITY_TYPE,
        action=action,
        entity_id=admin.email,
        actor_id=admin.email,
        detail={"from": previous, "to": current},
    )
    session.flush()


def digest_recipients(session: Session, settings: Settings) -> list[FaRecord]:
    """The advisors who get the digest under the current mode. View as never
    matters here: the digest goes to the advisor, not to whoever is looking."""
    if settings.digest_mode == "off":
        return []
    rows = list_active_advisors(session)
    if settings.digest_mode == "selected":
        rows = [row for row in rows if row.receives_digest]
    return [advisor_record(row) for row in rows]
