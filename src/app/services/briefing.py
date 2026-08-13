"""Assemble and audit one client-fund's on-demand briefing.

Gathers the fact block render_briefing needs from client_risk_features and
active_client_fund (never from the LLM boundary -- this never calls a
model at all), attaches the client's name last, and audits both the name
read and the briefing view itself, matching Section 19 of the
implementation plan.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from sqlalchemy import select, tuple_
from sqlalchemy.orm import Session

from app.audit.log import record_audit
from app.briefing.render import BriefingFacts, render_briefing
from app.db.models.active_clients import ActiveClientFund
from app.db.models.complaints import ClientComplaint
from app.db.models.models import Funds, PiiVault
from app.db.models.risk import ClientRiskFeatures, RiskConfigVersion
from app.db.session import restricted_session
from app.risk.signals import SIGNAL_LABELS, SIGNAL_ORDER


class BriefingNotFound(Exception):
    """No client_risk_features or active_client_fund row for this key."""


@dataclass(frozen=True)
class BriefingView:
    client_id: int
    unit_fund_id: int
    client_name: str | None
    text: str
    basis: list[str]


def briefing_available_keys(
    session: Session, keys: Sequence[tuple[int, int]]
) -> set[tuple[int, int]]:
    """Which of these (client_id, unit_fund_id) keys have enough data for
    get_briefing to render right now: a client_risk_features row and an
    active_client_fund row, both present -- the same two reads
    get_briefing itself does, so this can never disagree with what it
    actually finds. A cheap existence check, not a render: no name is read
    and nothing is audited.
    """
    keys = list(keys)
    if not keys:
        return set()
    risk_keys = set(
        session.execute(
            select(ClientRiskFeatures.client_id, ClientRiskFeatures.unit_fund_id).where(
                tuple_(ClientRiskFeatures.client_id, ClientRiskFeatures.unit_fund_id).in_(keys)
            )
        ).all()
    )
    active_keys = set(
        session.execute(
            select(ActiveClientFund.client_id, ActiveClientFund.unit_fund_id).where(
                tuple_(ActiveClientFund.client_id, ActiveClientFund.unit_fund_id).in_(keys)
            )
        ).all()
    )
    return risk_keys & active_keys


def _days_since(occurred: date | None, reference_date: date) -> int | None:
    """Days from occurred to reference_date, clipped at zero. Same clipping
    active_features.py::_days_since_purchase uses, for the same reason: a
    date after the reference point is a data quirk, not a real future event.
    """
    if occurred is None:
        return None
    return max(0, (reference_date - occurred).days)


def _drawdown_depth(largest_real_sale: float | None, balance: float | None) -> float | None:
    """The largest real sale as a share of the balance implied before it
    happened. Mirrors active_features.py::_drawdown_ratio.
    """
    if largest_real_sale is None or balance is None:
        return None
    implied_prior_balance = balance + largest_real_sale
    if implied_prior_balance <= 0:
        return None
    return largest_real_sale / implied_prior_balance


def _holds_both_funds(session: Session, client_id: int, unit_fund_id: int) -> bool:
    other = session.scalar(
        select(ActiveClientFund.unit_fund_id)
        .where(
            ActiveClientFund.client_id == client_id,
            ActiveClientFund.unit_fund_id != unit_fund_id,
        )
        .limit(1)
    )
    return other is not None


def _has_open_complaint(session: Session, client_id: int) -> bool:
    return (
        session.scalar(
            select(ClientComplaint.id)
            .where(ClientComplaint.client_id == client_id, ClientComplaint.status == "open")
            .limit(1)
        )
        is not None
    )


def _fund_name(session: Session, unit_fund_id: int) -> str:
    name = session.scalar(select(Funds.unit_fund_name).where(Funds.unit_fund_id == unit_fund_id))
    return name if name is not None else f"Fund {unit_fund_id}"


def _fee_runway_threshold(session: Session, config_version: int) -> float:
    """The FEE_RUNWAY_MONTHS threshold the config version that scored this
    client used, so the briefing's fee caveat is judged against the same
    number the score itself was computed with.
    """
    thresholds = session.scalar(
        select(RiskConfigVersion.thresholds).where(RiskConfigVersion.version == config_version)
    )
    return float(thresholds["FEE_RUNWAY_MONTHS"]) if thresholds else float("inf")


def _client_name(client_id: int) -> str | None:
    """The client's real name, read once under the restricted role and
    audited -- the same pattern eligibility.py::_vault_signals uses.
    """
    with restricted_session() as restricted:
        name = restricted.scalar(
            select(PiiVault.client_name).where(PiiVault.client_id == client_id)
        )
        record_audit(
            restricted,
            entity_type="pii_vault",
            action="read",
            entity_id=str(client_id),
            detail={"purpose": "risk_briefing"},
        )
        restricted.commit()
    return name


def get_briefing(
    session: Session,
    client_id: int,
    unit_fund_id: int,
    *,
    viewing_fa_id: str,
    reviewer_id: str | None = None,
    reference_date: date | None = None,
) -> BriefingView:
    """Render one client-fund's briefing and audit the view.

    Raises BriefingNotFound when either the score or the behavioural row is
    missing -- there is not enough to render a page that means anything.
    """
    risk = session.get(ClientRiskFeatures, (client_id, unit_fund_id))
    active = session.get(ActiveClientFund, (client_id, unit_fund_id))
    if risk is None or active is None:
        raise BriefingNotFound(f"{client_id}/{unit_fund_id}")

    ref = reference_date if reference_date is not None else date.today()
    signals = {name: getattr(risk, name) for name in SIGNAL_ORDER}

    facts = BriefingFacts(
        client_code=active.client_code,
        fund_name=_fund_name(session, unit_fund_id),
        risk_score=risk.risk_score,
        risk_band=risk.risk_band,
        route=risk.route,
        balance=active.balance if active.balance is not None else 0.0,
        balance_tier=risk.balance_tier or "Unknown",
        days_since_purchase=_days_since(active.last_purchase, ref),
        last_ticket=active.last_ticket,
        own_rhythm_days=active.rhythm_days,
        overdue_multiple=risk.lapse_ratio,
        typical_ticket=active.avg_ticket,
        largest_ticket=active.max_ticket,
        ticket_trend=active.ticket_trend,
        largest_real_redemption=active.largest_real_sale,
        drawdown_depth=_drawdown_depth(active.largest_real_sale, active.balance),
        days_since_real_redemption=_days_since(active.last_real_sale_date, ref),
        signals=signals,
        purchases_censored=active.purchases_censored,
        redemption_history_blind=active.redemption_history_blind,
        holds_both_funds=_holds_both_funds(session, client_id, unit_fund_id),
        fee_runway_months=active.fee_runway_months,
        fee_runway_threshold=_fee_runway_threshold(session, risk.config_version),
        has_open_complaint=_has_open_complaint(session, client_id),
    )
    text = render_briefing(facts)
    name = _client_name(client_id)
    # The same fired signals the "WHY THIS CLIENT SURFACED" section of
    # `text` lists, in the same order -- the discrete facts AM11 actually
    # weighed, not a paraphrase of them.
    basis = [SIGNAL_LABELS[sig] for sig in SIGNAL_ORDER if signals.get(sig)]

    record_audit(
        session,
        entity_type="risk_briefing",
        action="view",
        entity_id=str(client_id),
        actor_id=viewing_fa_id,
        detail={"unit_fund_id": unit_fund_id, "route": risk.route, "reviewer_id": reviewer_id},
    )
    session.commit()

    return BriefingView(
        client_id=client_id, unit_fund_id=unit_fund_id, client_name=name, text=text, basis=basis
    )
