"""Console reads over the client segment: bucketed features only, never PII.

Every query here joins client_features and client_message_indicators, the
model-safe projection the rest of the codebase already treats as the source
of truth for a client's bucket; no query here ever touches pii_vault. A
client's name is deliberately never re-attached on any of these reads: that
step is gated on the reviewer key (see app.api.reviewer_auth) ahead of a
real role, and the endpoint that calls get_client_name is the only place in
this module allowed to read pii_vault at all.

latest_call_brief is the one un-gated exception: it carries no name or PII
to begin with (agents.email_agent.render_call_brief never takes any), so
surfacing it here re-exposes nothing new.

get_client_profile is a wider read than list_clients/get_client, but it
stays inside the same boundary: every column it touches lives on clients,
client_features, client_message_indicators, funds, enrollment, touch_log,
outreach_message (status fields only, never ai_draft_content or
personalized_content), contact_events, or suppression -- never pii_vault.

get_client_name is different on purpose: it is the one function here that
reads pii_vault, through the restricted role, and it audits every read.
Its caller is responsible for gating access to it (see app.api.reviewer_auth).
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import Row, func, select
from sqlalchemy.orm import Session

from app.audit.log import record_audit
from app.db.models.campaigns import ContactEvent, Enrollment, TouchLog
from app.db.models.models import ClientFeatures, Clients, Funds, PiiVault
from app.db.models.outreach import OutreachMessage
from app.db.models.rules import ClientMessageIndicators
from app.db.models.suppression import Suppression
from app.db.session import restricted_session
from app.pagination import DEFAULT_LIMIT, clamp_limit, decode_id_cursor, encode_id_cursor

_CLIENT_COLUMNS = (
    Clients.client_id,
    Clients.unit_fund_id,
    ClientFeatures.recency_band,
    ClientFeatures.value_band,
    ClientFeatures.cadence_band,
    ClientFeatures.hold_band,
    ClientFeatures.purchase_depth,
    ClientMessageIndicators.message_angle,
    ClientMessageIndicators.priority_tier,
)


def _base_query(*columns):
    return (
        select(*(columns or _CLIENT_COLUMNS))
        .select_from(Clients)
        .join(ClientFeatures, ClientFeatures.client_id == Clients.client_id, isouter=True)
        .join(
            ClientMessageIndicators,
            ClientMessageIndicators.client_id == Clients.client_id,
            isouter=True,
        )
    )


def _apply_bucket_filters(
    query,
    *,
    client_id: int | None,
    fund_id: int | None,
    value_band: str | None,
    recency_band: str | None,
    purchase_depth: str | None,
    cadence_band: str | None,
    message_angle: str | None,
    newly_dormant: bool | None,
):
    """The allow-listed bucket filters every client query accepts, applied in
    one place so list_clients, get_client, and cohort resolution for a new
    campaign can never drift apart on what "matching" means.
    """
    if client_id is not None:
        query = query.where(Clients.client_id == client_id)
    if fund_id is not None:
        query = query.where(Clients.unit_fund_id == fund_id)
    if value_band is not None:
        query = query.where(ClientFeatures.value_band == value_band)
    if recency_band is not None:
        query = query.where(ClientFeatures.recency_band == recency_band)
    if purchase_depth is not None:
        query = query.where(ClientFeatures.purchase_depth == purchase_depth)
    if cadence_band is not None:
        query = query.where(ClientFeatures.cadence_band == cadence_band)
    if message_angle is not None:
        query = query.where(ClientMessageIndicators.message_angle == message_angle)
    if newly_dormant is not None:
        query = query.where(ClientFeatures.newly_dormant == newly_dormant)
    return query


class ClientNotFound(Exception):
    """No client exists with the given id."""


def list_clients(
    session: Session,
    *,
    client_id: int | None = None,
    fund_id: int | None = None,
    value_band: str | None = None,
    recency_band: str | None = None,
    purchase_depth: str | None = None,
    cadence_band: str | None = None,
    message_angle: str | None = None,
    newly_dormant: bool | None = None,
    cursor: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> tuple[list[Row], str | None]:
    """Clients matching the given bucket filters, oldest client_id first."""
    limit = clamp_limit(limit)
    query = _apply_bucket_filters(
        _base_query(),
        client_id=client_id,
        fund_id=fund_id,
        value_band=value_band,
        recency_band=recency_band,
        purchase_depth=purchase_depth,
        cadence_band=cadence_band,
        message_angle=message_angle,
        newly_dormant=newly_dormant,
    )
    if cursor is not None:
        after_id = decode_id_cursor(cursor)
        query = query.where(Clients.client_id > after_id)
    query = query.order_by(Clients.client_id).limit(limit + 1)

    rows = list(session.execute(query).all())
    next_cursor = None
    if len(rows) > limit:
        rows = rows[:limit]
        next_cursor = encode_id_cursor(rows[-1].client_id)
    return rows, next_cursor


def resolve_cohort_client_ids(
    session: Session,
    *,
    fund_id: int | None = None,
    value_band: str | None = None,
    recency_band: str | None = None,
    purchase_depth: str | None = None,
    cadence_band: str | None = None,
    message_angle: str | None = None,
    newly_dormant: bool | None = None,
) -> list[int]:
    """Every client_id matching the given bucket filters, unpaginated.

    A new campaign's cohort is the whole match, not one page of it, so this
    is the one place a client query intentionally skips the cursor/limit
    every console-facing list uses.
    """
    query = _apply_bucket_filters(
        _base_query(Clients.client_id),
        client_id=None,
        fund_id=fund_id,
        value_band=value_band,
        recency_band=recency_band,
        purchase_depth=purchase_depth,
        cadence_band=cadence_band,
        message_angle=message_angle,
        newly_dormant=newly_dormant,
    )
    return list(session.scalars(query).all())


def get_client(session: Session, client_id: int) -> Row:
    """One client's buckets, or raise ClientNotFound.

    Same non-PII projection as list_clients: a name still isn't re-attached
    here, for the same reason the list endpoint never re-attaches one (see
    module docstring).
    """
    row = session.execute(_base_query().where(Clients.client_id == client_id)).one_or_none()
    if row is None:
        raise ClientNotFound(client_id)
    return row


def latest_call_brief(session: Session, client_id: int) -> str | None:
    """This client's most recent approved call_brief, if any."""
    return session.scalar(
        select(OutreachMessage.call_brief)
        .where(
            OutreachMessage.client_id == client_id,
            OutreachMessage.status == "approved",
            OutreachMessage.call_brief.isnot(None),
        )
        .order_by(OutreachMessage.created_at.desc())
        .limit(1)
    )


_PROFILE_CORE_COLUMNS = (
    Clients.client_id,
    Clients.client_code,
    Clients.unit_fund_id,
    Funds.unit_fund_name,
    ClientFeatures.fund_type,
    ClientFeatures.n_funds,
    ClientFeatures.holds_other_funds,
    ClientFeatures.recency_band,
    ClientFeatures.value_band,
    ClientFeatures.cadence_band,
    ClientFeatures.hold_band,
    ClientFeatures.purchase_depth,
    ClientFeatures.trend_band,
    ClientFeatures.exit_reason,
    ClientFeatures.own_rhythm_days,
    ClientFeatures.in_wave,
    ClientFeatures.newly_dormant,
    ClientFeatures.has_depth,
    ClientFeatures.staged_exit,
    ClientFeatures.stale_contact,
    ClientFeatures.history_censored,
    ClientFeatures.purchases_censored,
    Clients.last_activity_date,
    Clients.days_since_last_activity,
    ClientFeatures.observed_volume,
    Clients.n_purchases_returned,
    Clients.total_purchase_amount,
    Clients.computed_at,
    ClientMessageIndicators.message_angle,
    ClientMessageIndicators.priority_tier,
    ClientMessageIndicators.urgency,
    ClientMessageIndicators.prompt_variant,
    ClientMessageIndicators.rule_name,
    ClientMessageIndicators.rule_version,
)


def _profile_core(session: Session, client_id: int) -> Row:
    """Identity, bands, flags, activity, and routing in one row.

    Every join beyond clients is outer: a client can be missing its
    features row (not yet transformed) or its indicators row (not yet
    resolved), and that is a row of nulls here, not a 404 -- only a missing
    clients row is.
    """
    query = (
        select(*_PROFILE_CORE_COLUMNS)
        .select_from(Clients)
        .join(ClientFeatures, ClientFeatures.client_id == Clients.client_id, isouter=True)
        .join(
            ClientMessageIndicators,
            ClientMessageIndicators.client_id == Clients.client_id,
            isouter=True,
        )
        .join(Funds, Funds.unit_fund_id == Clients.unit_fund_id, isouter=True)
        .where(Clients.client_id == client_id)
    )
    row = session.execute(query).one_or_none()
    if row is None:
        raise ClientNotFound(client_id)
    return row


def _client_enrollments(session: Session, client_id: int) -> list[Enrollment]:
    return list(
        session.scalars(
            select(Enrollment)
            .where(Enrollment.client_id == client_id)
            .order_by(Enrollment.enrollment_id)
        ).all()
    )


def _client_touch_log(session: Session, client_id: int) -> list[TouchLog]:
    return list(
        session.scalars(
            select(TouchLog)
            .join(Enrollment, Enrollment.enrollment_id == TouchLog.enrollment_id)
            .where(Enrollment.client_id == client_id)
            .order_by(TouchLog.touch_id)
        ).all()
    )


def _client_outreach_messages(session: Session, client_id: int) -> list[OutreachMessage]:
    return list(
        session.scalars(
            select(OutreachMessage)
            .where(OutreachMessage.client_id == client_id)
            .order_by(OutreachMessage.created_at)
        ).all()
    )


def _client_contact_events(session: Session, client_id: int) -> list[ContactEvent]:
    return list(
        session.scalars(
            select(ContactEvent)
            .where(ContactEvent.client_id == client_id)
            .order_by(ContactEvent.occurred_at)
        ).all()
    )


@dataclass(frozen=True)
class ClientProfile:
    """Every non-PII fact this codebase holds about one client, gathered
    from the tables get_client_profile is allowed to read (see module
    docstring). core is the single-row identity/bands/flags/activity/routing
    projection; the rest are per-client history.
    """

    core: Row
    enrollments: list[Enrollment]
    touch_log: list[TouchLog]
    outreach_messages: list[OutreachMessage]
    contact_events: list[ContactEvent]
    suppression: Suppression | None
    call_brief: str | None


def get_client_profile(session: Session, client_id: int) -> ClientProfile:
    """The fuller client profile: everything in ClientProfile, or raise
    ClientNotFound. No name -- see services/clients.py's module docstring.
    """
    return ClientProfile(
        core=_profile_core(session, client_id),
        enrollments=_client_enrollments(session, client_id),
        touch_log=_client_touch_log(session, client_id),
        outreach_messages=_client_outreach_messages(session, client_id),
        contact_events=_client_contact_events(session, client_id),
        suppression=session.get(Suppression, client_id),
        call_brief=latest_call_brief(session, client_id),
    )


def get_client_name(
    session: Session, client_id: int, *, reviewer_id: str | None = None
) -> str | None:
    """This client's real name, read through the restricted role and
    audited. The one PII read in this module -- see module docstring.

    reviewer_id is the authenticated caller (see app.api.reviewer_auth),
    recorded as the audit row's actor so the read names who actually
    looked, not just that a look happened.

    Raises ClientNotFound when no clients row exists at all, the same
    not-found this module's other reads use. A clients row with nothing in
    pii_vault yet is not that: it returns None, a real and common state for
    a client whose contact channels have not synced yet.
    """
    if session.get(Clients, client_id) is None:
        raise ClientNotFound(client_id)

    with restricted_session() as restricted:
        name = restricted.scalar(
            select(PiiVault.client_name).where(PiiVault.client_id == client_id)
        )
        record_audit(
            restricted,
            entity_type="pii_vault",
            action="read",
            entity_id=str(client_id),
            actor_id=reviewer_id,
            detail={"purpose": "client_profile_name"},
        )
        restricted.commit()
    return name


def segment_distribution(session: Session) -> dict[str, list[tuple] | int]:
    """Client counts grouped by purchase depth, value band, cadence band,
    message angle, and a value-band x recency-band cross-tab, plus scalar
    counts of the data-quality flags a reader needs before treating any of
    the above as a complete count.
    """

    def _counts(column) -> list[tuple[str, int]]:
        return list(
            session.execute(
                select(column, func.count()).group_by(column).order_by(func.count().desc())
            ).all()
        )

    def _flag_count(column) -> int:
        return (
            session.execute(
                select(func.count()).select_from(ClientFeatures).where(column)
            ).scalar_one()
            or 0
        )

    cross_tab = list(
        session.execute(
            select(ClientFeatures.value_band, ClientFeatures.recency_band, func.count())
            .group_by(ClientFeatures.value_band, ClientFeatures.recency_band)
            .order_by(func.count().desc())
        ).all()
    )

    return {
        "by_purchase_depth": _counts(ClientFeatures.purchase_depth),
        "by_value_band": _counts(ClientFeatures.value_band),
        "by_cadence_band": _counts(ClientFeatures.cadence_band),
        "by_message_angle": _counts(ClientMessageIndicators.message_angle),
        "by_value_and_recency": cross_tab,
        "stale_contact_count": _flag_count(ClientFeatures.stale_contact),
        "history_censored_count": _flag_count(ClientFeatures.history_censored),
        "purchases_censored_count": _flag_count(ClientFeatures.purchases_censored),
        "unknown_recency_count": _flag_count(ClientFeatures.recency_band == "Unknown"),
    }


@dataclass(frozen=True)
class ClientBookSummary:
    total_clients: int
    fund_count: int


def client_book_summary(session: Session) -> ClientBookSummary:
    """Book-wide client and fund counts, read straight off clients/funds."""
    total_clients = session.scalar(select(func.count()).select_from(Clients)) or 0
    fund_count = session.scalar(select(func.count()).select_from(Funds)) or 0
    return ClientBookSummary(total_clients=total_clients, fund_count=fund_count)


@dataclass(frozen=True)
class EnrollmentSummary:
    enrolled_count: int
    excluded_count: int


def enrollment_summary(session: Session) -> EnrollmentSummary:
    """Distinct clients currently enrolled vs. excluded, book-wide.

    Summing each campaign's own enrollment count would double-count a
    client enrolled in more than one campaign, so this counts distinct
    client_id directly off enrollment.status instead. enrolled_count is
    "enrolled" or "in_progress" (an active enrollment right now);
    excluded_count is the "excluded" terminal status -- the two status
    values with an obvious direct mapping, not an invented category.
    """
    enrolled = session.scalar(
        select(func.count(func.distinct(Enrollment.client_id))).where(
            Enrollment.status.in_(("enrolled", "in_progress"))
        )
    )
    excluded = session.scalar(
        select(func.count(func.distinct(Enrollment.client_id))).where(
            Enrollment.status == "excluded"
        )
    )
    return EnrollmentSummary(enrolled_count=enrolled or 0, excluded_count=excluded or 0)


@dataclass(frozen=True)
class SuppressionSummary:
    suppressed_count: int
    by_reason: list[tuple[str, int]]


def suppression_summary(session: Session) -> SuppressionSummary:
    """Book-wide suppression count and a reason breakdown.

    client_id is suppression's primary key -- one row per client already,
    so count(*) is already a distinct-client count.
    """
    suppressed_count = session.scalar(select(func.count()).select_from(Suppression)) or 0
    by_reason = list(
        session.execute(
            select(Suppression.reason, func.count())
            .group_by(Suppression.reason)
            .order_by(func.count().desc())
        ).all()
    )
    return SuppressionSummary(suppressed_count=suppressed_count, by_reason=by_reason)
