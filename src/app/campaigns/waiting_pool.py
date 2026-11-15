from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from typing import Any

from sqlalchemy import BigInteger, Select, all_, any_, cast, func, select
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Session

from app.audit.log import record_audit
from app.campaigns.state_machine import TERMINAL_STATUSES
from app.config import get_settings
from app.db.models.campaigns import ContactEvent, Enrollment
from app.db.models.models import ClientFeatures, PiiVault
from app.db.models.routing import ClientSide
from app.db.models.rules import ClientMessageIndicators
from app.db.models.suppression import Suppression
from app.db.session import restricted_session
from app.routing.sides import SIDE_INACTIVE

STOPPING_EVENT_TYPES = ("bounce", "complaint", "optout")


def _opted_out_client_ids() -> list[int]:
    with restricted_session() as vault:
        ids = list(vault.scalars(select(PiiVault.client_id).where(PiiVault.opt_out_flag.is_(True))))
        record_audit(
            vault,
            entity_type="pii_vault",
            action="read_batch",
            detail={"count": len(ids), "purpose": "waiting_pool"},
        )
        vault.commit()
    return ids


def _waiting_query(*columns: Any) -> Select:
    query = (
        select(*columns)
        .select_from(ClientSide)
        .join(ClientMessageIndicators, ClientMessageIndicators.client_id == ClientSide.client_id)
        .join(ClientFeatures, ClientFeatures.client_id == ClientSide.client_id, isouter=True)
        .where(
            ClientSide.side == SIDE_INACTIVE,
            ClientSide.client_id.not_in(select(Suppression.client_id)),
            ClientSide.client_id.not_in(
                select(ContactEvent.client_id).where(ContactEvent.type.in_(STOPPING_EVENT_TYPES))
            ),
            ClientSide.client_id.not_in(
                select(Enrollment.client_id).where(
                    Enrollment.status.not_in(sorted(TERMINAL_STATUSES))
                )
            ),
        )
    )
    opted_out = _opted_out_client_ids()
    if opted_out:
        query = query.where(ClientSide.client_id != all_(cast(opted_out, ARRAY(BigInteger))))
    return query


def waiting_client_ids(session: Session, candidates: Sequence[int] | None = None) -> list[int]:
    query = _waiting_query(ClientSide.client_id)
    if candidates is not None:
        query = query.where(ClientSide.client_id == any_(cast(list(candidates), ARRAY(BigInteger))))
    return list(session.scalars(query))


def _shown(counts: Counter, minimum: int) -> dict[str, int]:
    return {key: n for key, n in counts.most_common() if n >= minimum}


def waiting_pool_summary(session: Session) -> dict[str, Any]:
    value_band = func.coalesce(ClientFeatures.value_band, "unknown")
    rows = session.execute(
        _waiting_query(
            ClientMessageIndicators.message_angle,
            ClientMessageIndicators.priority_tier,
            value_band,
            func.count(),
        ).group_by(
            ClientMessageIndicators.message_angle, ClientMessageIndicators.priority_tier, value_band
        )
    ).all()

    by_angle, by_tier, by_band = Counter(), Counter(), Counter()
    by_tier_and_band: Counter = Counter()
    for angle, tier, band, n in rows:
        by_angle[angle] += n
        by_tier[tier] += n
        by_band[band] += n
        by_tier_and_band[(tier, band)] += n

    minimum = get_settings().campaign_agent_min_group_size
    cells = [
        {"tier": tier, "value_band": band, "count": n}
        for (tier, band), n in by_tier_and_band.most_common()
        if n >= minimum
    ]
    return {
        "total": sum(by_tier.values()),
        "by_tier": _shown(by_tier, minimum),
        "by_angle": _shown(by_angle, minimum),
        "by_value_band": _shown(by_band, minimum),
        "by_tier_and_value_band": cells,
        "groups_too_small_to_show": len(by_tier_and_band) - len(cells),
    }
