"""Console reads and writes for the active-client (risk/digest) population.

Everything here is keyed by (client_id, unit_fund_id), the active book's own
key -- distinct from the dormant client_fund population app.services.clients
covers. The active-client population has no campaign, enrollment, or
outreach_message path in this codebase yet: the interaction log below is
manual FA bookkeeping, never a send trigger. No query here reads pii_vault;
a name is never re-attached on any of these reads.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.log import record_audit
from app.db.models.active_clients import ActiveClientFund, ActiveClientInteraction
from app.db.models.complaints import ClientComplaint
from app.db.models.models import Funds
from app.db.models.risk import ClientRiskFeatures, RiskSnapshot


class ActiveClientNotFound(Exception):
    """No active_client_fund row for this client-fund key."""


def _fund_name(session: Session, unit_fund_id: int) -> str:
    name = session.scalar(select(Funds.unit_fund_name).where(Funds.unit_fund_id == unit_fund_id))
    return name if name is not None else f"Fund {unit_fund_id}"


def record_interaction(
    session: Session,
    client_id: int,
    unit_fund_id: int,
    *,
    type: str,
    note: str | None,
    reviewer_id: str,
) -> ActiveClientInteraction:
    """Log one FA action against a client-fund, or raise ActiveClientNotFound.

    reviewer_id is the caller X-Reviewer-Key resolved to (see
    app.api.reviewer_auth), never a self-reported field on the request
    body. Audited the same as every other write path in this codebase.
    """
    if session.get(ActiveClientFund, (client_id, unit_fund_id)) is None:
        raise ActiveClientNotFound(f"{client_id}/{unit_fund_id}")

    row = ActiveClientInteraction(
        client_id=client_id,
        unit_fund_id=unit_fund_id,
        type=type,
        note=note,
        reviewer_id=reviewer_id,
    )
    session.add(row)
    session.flush()
    record_audit(
        session,
        entity_type="active_client_interaction",
        action=type,
        entity_id=f"{client_id}/{unit_fund_id}",
        actor_id=reviewer_id,
        detail={"note": note} if note else None,
    )
    session.commit()
    return row


def list_interactions(
    session: Session,
    client_id: int,
    unit_fund_id: int,
    *,
    since: datetime | None = None,
) -> list[ActiveClientInteraction]:
    """This client-fund's logged interactions, most recent first."""
    query = select(ActiveClientInteraction).where(
        ActiveClientInteraction.client_id == client_id,
        ActiveClientInteraction.unit_fund_id == unit_fund_id,
    )
    if since is not None:
        query = query.where(ActiveClientInteraction.created_at >= since)
    query = query.order_by(ActiveClientInteraction.created_at.desc())
    return list(session.scalars(query).all())


@dataclass(frozen=True)
class ActiveClientProfile:
    """Every non-PII fact this codebase holds about one active-client-fund
    relationship, gathered from the tables get_active_client_profile is
    allowed to read (see module docstring).
    """

    active: ActiveClientFund
    risk: ClientRiskFeatures | None
    fund_name: str
    risk_history: list[RiskSnapshot]
    complaints: list[ClientComplaint]
    interactions: list[ActiveClientInteraction]


def get_active_client_profile(
    session: Session, client_id: int, unit_fund_id: int
) -> ActiveClientProfile:
    """The fuller active-client profile: identity, current bands, risk
    history, and complaint/interaction history. Raises ActiveClientNotFound
    when there is no active_client_fund row at all; a client_risk_features
    row missing (no nightly run has scored it yet) is not that -- bands
    come back null instead.
    """
    active = session.get(ActiveClientFund, (client_id, unit_fund_id))
    if active is None:
        raise ActiveClientNotFound(f"{client_id}/{unit_fund_id}")

    risk = session.get(ClientRiskFeatures, (client_id, unit_fund_id))
    risk_history = list(
        session.scalars(
            select(RiskSnapshot)
            .where(RiskSnapshot.client_id == client_id, RiskSnapshot.unit_fund_id == unit_fund_id)
            .order_by(RiskSnapshot.snapshot_id.desc())
        )
    )
    complaints = list(
        session.scalars(
            select(ClientComplaint)
            .where(ClientComplaint.client_id == client_id)
            .order_by(ClientComplaint.opened_at.desc())
        )
    )

    return ActiveClientProfile(
        active=active,
        risk=risk,
        fund_name=_fund_name(session, unit_fund_id),
        risk_history=risk_history,
        complaints=complaints,
        interactions=list_interactions(session, client_id, unit_fund_id),
    )
