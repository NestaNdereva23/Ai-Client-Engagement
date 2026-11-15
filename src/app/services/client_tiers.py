from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models.routing import ClientSide, ClientTier
from app.routing.sides import SIDE_ACTIVE, SIDE_INACTIVE
from app.routing.tiers import (
    ACTIVE_TIERS_IN_DISPLAY_ORDER,
    BELOW_CAPITAL_PRESERVATION_THRESHOLD,
    NO_TRANSACTIONS_IN_REVIEW_PERIOD,
)


@dataclass(frozen=True)
class TierSummary:
    active_clients: int
    inactive_clients: int
    active_clients_by_tier: dict[str, int]
    active_clients_with_no_transactions: int
    inactive_clients_below_threshold: int
    active_clients_not_yet_tiered: int
    computed_at: datetime | None


def summarize_client_tiers(session: Session) -> TierSummary:
    grouped_rows = session.execute(
        select(ClientSide.side, ClientTier.tier, func.count(), func.max(ClientTier.computed_at))
        .select_from(ClientSide)
        .join(ClientTier, ClientTier.client_id == ClientSide.client_id, isouter=True)
        .group_by(ClientSide.side, ClientTier.tier)
    ).all()

    clients_by_side_and_tier = {(side, tier): clients for side, tier, clients, _ in grouped_rows}
    computed_times = [computed_at for *_, computed_at in grouped_rows if computed_at is not None]

    active_clients = sum(
        clients for (side, _), clients in clients_by_side_and_tier.items() if side == SIDE_ACTIVE
    )
    inactive_clients = sum(
        clients for (side, _), clients in clients_by_side_and_tier.items() if side == SIDE_INACTIVE
    )
    active_clients_by_tier = {
        tier: clients_by_side_and_tier.get((SIDE_ACTIVE, tier), 0)
        for tier in ACTIVE_TIERS_IN_DISPLAY_ORDER
    }
    active_clients_with_no_transactions = clients_by_side_and_tier.get(
        (SIDE_ACTIVE, NO_TRANSACTIONS_IN_REVIEW_PERIOD), 0
    )
    active_clients_with_a_tier_or_held_out = (
        sum(active_clients_by_tier.values()) + active_clients_with_no_transactions
    )

    return TierSummary(
        active_clients=active_clients,
        inactive_clients=inactive_clients,
        active_clients_by_tier=active_clients_by_tier,
        active_clients_with_no_transactions=active_clients_with_no_transactions,
        inactive_clients_below_threshold=clients_by_side_and_tier.get(
            (SIDE_INACTIVE, BELOW_CAPITAL_PRESERVATION_THRESHOLD), 0
        ),
        active_clients_not_yet_tiered=active_clients - active_clients_with_a_tier_or_held_out,
        computed_at=max(computed_times) if computed_times else None,
    )
