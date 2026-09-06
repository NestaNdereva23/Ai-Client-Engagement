from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date

from sqlalchemy import Row, func, select
from sqlalchemy.orm import Session

from app.db.models.campaigns import Enrollment
from app.db.models.models import ClientFeatures, Clients, Funds, IngestionStatus
from app.db.models.rules import ClientMessageIndicators
from app.db.models.suppression import Suppression
from app.pagination import DEFAULT_LIMIT
from app.services.campaigns import REENGAGED_STATUS
from app.services.clients import list_clients
from app.services.rules import list_angle_status

ENROLLED_STATUSES = ("enrolled", "in_progress")
EXCLUDED_STATUS = "excluded"
UNKNOWN_RECENCY_BAND = "Unknown"


def _ranked(totals: Counter) -> list[tuple[str | None, int]]:
    """Buckets ordered by count, largest first, with the key breaking ties so
    two runs over the same data never come back in a different order.
    """
    return sorted(totals.items(), key=lambda item: (-item[1], str(item[0])))


@dataclass(frozen=True)
class SegmentRollup:
    by_purchase_depth: list[tuple[str | None, int]]
    by_value_band: list[tuple[str | None, int]]
    by_cadence_band: list[tuple[str | None, int]]
    by_message_angle: list[tuple[str | None, int]]
    by_value_and_recency: list[tuple[str | None, str | None, int]]
    stale_contact_count: int
    history_censored_count: int
    purchases_censored_count: int
    unknown_recency_count: int


def _segment_rollup(session: Session) -> SegmentRollup:
    grid = session.execute(
        select(
            ClientFeatures.value_band,
            ClientFeatures.recency_band,
            ClientFeatures.purchase_depth,
            ClientFeatures.cadence_band,
            func.count(),
            func.count().filter(ClientFeatures.stale_contact),
            func.count().filter(ClientFeatures.history_censored),
            func.count().filter(ClientFeatures.purchases_censored),
        ).group_by(
            ClientFeatures.value_band,
            ClientFeatures.recency_band,
            ClientFeatures.purchase_depth,
            ClientFeatures.cadence_band,
        )
    ).all()

    by_depth: Counter = Counter()
    by_value: Counter = Counter()
    by_cadence: Counter = Counter()
    cross_tab: Counter = Counter()
    stale = history = purchases = unknown_recency = 0

    for value_band, recency_band, depth, cadence, count, n_stale, n_hist, n_purch in grid:
        by_depth[depth] += count
        by_value[value_band] += count
        by_cadence[cadence] += count
        cross_tab[(value_band, recency_band)] += count
        stale += n_stale
        history += n_hist
        purchases += n_purch
        if recency_band == UNKNOWN_RECENCY_BAND:
            unknown_recency += count

    by_angle = session.execute(
        select(ClientMessageIndicators.message_angle, func.count())
        .group_by(ClientMessageIndicators.message_angle)
        .order_by(func.count().desc())
    ).all()

    return SegmentRollup(
        by_purchase_depth=_ranked(by_depth),
        by_value_band=_ranked(by_value),
        by_cadence_band=_ranked(by_cadence),
        by_message_angle=[(angle, count) for angle, count in by_angle],
        by_value_and_recency=[
            (value_band, recency_band, count)
            for (value_band, recency_band), count in _ranked(cross_tab)
        ],
        stale_contact_count=stale,
        history_censored_count=history,
        purchases_censored_count=purchases,
        unknown_recency_count=unknown_recency,
    )


@dataclass(frozen=True)
class BookCounts:
    total_clients: int
    fund_count: int


def _book_counts(session: Session) -> BookCounts:
    """Client and fund headcounts as two scalar subqueries in one statement."""
    total_clients, fund_count = session.execute(
        select(
            select(func.count()).select_from(Clients).scalar_subquery(),
            select(func.count()).select_from(Funds).scalar_subquery(),
        )
    ).one()
    return BookCounts(total_clients=total_clients or 0, fund_count=fund_count or 0)


@dataclass(frozen=True)
class EnrollmentCounts:
    enrolled_count: int
    excluded_count: int
    primary_count: int
    reengaged_count: int

    @property
    def reengagement_rate(self) -> float:
        return self.reengaged_count / self.primary_count if self.primary_count else 0.0


def _enrollment_counts(session: Session) -> EnrollmentCounts:
    enrolled, excluded, primary, reengaged = session.execute(
        select(
            func.count(func.distinct(Enrollment.client_id)).filter(
                Enrollment.status.in_(ENROLLED_STATUSES)
            ),
            func.count(func.distinct(Enrollment.client_id)).filter(
                Enrollment.status == EXCLUDED_STATUS
            ),
            func.count().filter(Enrollment.is_primary_contact_row.is_(True)),
            func.count().filter(
                Enrollment.is_primary_contact_row.is_(True),
                Enrollment.status == REENGAGED_STATUS,
            ),
        ).select_from(Enrollment)
    ).one()
    return EnrollmentCounts(
        enrolled_count=enrolled or 0,
        excluded_count=excluded or 0,
        primary_count=primary or 0,
        reengaged_count=reengaged or 0,
    )


@dataclass(frozen=True)
class SuppressionCounts:
    suppressed_count: int
    by_reason: list[tuple[str, int]]


def _suppression_counts(session: Session) -> SuppressionCounts:
    by_reason = session.execute(
        select(Suppression.reason, func.count())
        .group_by(Suppression.reason)
        .order_by(func.count().desc())
    ).all()
    return SuppressionCounts(
        suppressed_count=sum(count for _, count in by_reason),
        by_reason=[(reason, count) for reason, count in by_reason],
    )


def _records_rejected(session: Session) -> int | None:
    return session.scalar(
        select(IngestionStatus.records_rejected)
        .order_by(IngestionStatus.started_at.desc())
        .limit(1)
    )


@dataclass(frozen=True)
class ClientsOverview:
    book: BookCounts
    segments: SegmentRollup
    enrollment: EnrollmentCounts
    suppression: SuppressionCounts
    angles: list[tuple[str, int, date, date | None, bool]]
    records_rejected: int | None
    roster: list[Row]
    roster_next_cursor: str | None


def clients_overview(session: Session, *, roster_limit: int = DEFAULT_LIMIT) -> ClientsOverview:
    roster, next_cursor = list_clients(session, limit=roster_limit)
    return ClientsOverview(
        book=_book_counts(session),
        segments=_segment_rollup(session),
        enrollment=_enrollment_counts(session),
        suppression=_suppression_counts(session),
        angles=list_angle_status(session, active_on=date.today()),
        records_rejected=_records_rejected(session),
        roster=roster,
        roster_next_cursor=next_cursor,
    )
