from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from sqlalchemy import select, tuple_
from sqlalchemy.orm import Session

from app.agents.prompt_versioning import CLIENT_BRIEF_NARRATIVE, prompt_text
from app.agents.signals import keys_with_active_signal
from app.audit.log import record_audit, with_on_behalf_of
from app.briefing.narrative import NarrativeResult, draft_narrative
from app.briefing.render import BriefingFacts, render_briefing
from app.briefing.store import read_narrative, save_narrative
from app.config import get_settings
from app.db.models.active_clients import ActiveClientFund
from app.db.models.complaints import ClientComplaint
from app.db.models.models import Funds, PiiVault
from app.db.models.risk import ClientRiskFeatures, RiskConfigVersion
from app.db.models.routing import ClientTier
from app.db.session import restricted_session
from app.privacy.boundary import AuditSink, BoundaryAudit
from app.privacy.fact_block import FUND_DISPLAY_NAMES, RiskFactBlock
from app.privacy.llm_client import LLMClient
from app.risk.signals import (
    ADVISOR_SIGNAL_LABELS,
    ADVISOR_SIGNAL_ORDER,
    GOING_DORMANT,
    NEAR_PRESERVATION_LINE,
    SIGNAL_ORDER,
)
from app.routing.tier_playbook import TIER_PLAYBOOK
from app.services.client_next_actions import next_actions_for
from app.transform.active_features import BALANCE_TIERS
from app.transform.features import MIN_TREND_CHANGE
from app.transform.features import fund_type_from_name as classify_fund_type


class BriefingNotFound(Exception):
    """No client_risk_features or active_client_fund row for this key."""


class NarrativeDisabled(Exception):
    """get_narrative_briefing was called while settings.ai_briefing_enabled is off."""


@dataclass(frozen=True)
class BriefingView:
    client_id: int
    unit_fund_id: int
    client_name: str | None
    text: str
    basis: list[str]
    # "deterministic" for get_briefing; "narrative" or "deterministic_fallback"
    # for get_narrative_briefing -- see briefing.narrative.NarrativeResult.
    mode: str = "deterministic"


def briefing_available_keys(
    session: Session, keys: Sequence[tuple[int, int]]
) -> set[tuple[int, int]]:
    """Which (client_id, unit_fund_id) keys have both a risk row and a fund row.
    A cheap existence check: no name is read and nothing is audited."""
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
    """Days from occurred to reference_date, clipped at zero so a future date is never negative."""
    if occurred is None:
        return None
    return max(0, (reference_date - occurred).days)


def _withdrawal_pct(largest_withdrawal: float | None, balance: float | None) -> float | None:
    """The largest withdrawal as a share of the balance before it. Mirrors active_features."""
    if largest_withdrawal is None or balance is None:
        return None
    balance_before_withdrawal = balance + largest_withdrawal
    if balance_before_withdrawal <= 0:
        return None
    return largest_withdrawal / balance_before_withdrawal


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


def _months_until_empty_threshold(session: Session, config_version: int) -> float:
    thresholds = session.scalar(
        select(RiskConfigVersion.thresholds).where(RiskConfigVersion.version == config_version)
    )
    return float(thresholds["MONTHS_UNTIL_EMPTY"]) if thresholds else float("inf")


def _client_name(client_id: int) -> str | None:
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


def gather_briefing_facts(
    session: Session, client_id: int, unit_fund_id: int, reference_date: date
) -> BriefingFacts | None:
    risk = session.get(ClientRiskFeatures, (client_id, unit_fund_id))
    active = session.get(ActiveClientFund, (client_id, unit_fund_id))
    if risk is None or active is None:
        return None

    signals = {name: getattr(risk, name) for name in SIGNAL_ORDER if name != GOING_DORMANT}
    signals[NEAR_PRESERVATION_LINE] = bool(
        keys_with_active_signal(session, NEAR_PRESERVATION_LINE, [(client_id, unit_fund_id)])
    )

    client_tier = session.get(ClientTier, client_id)
    playbook = TIER_PLAYBOOK.get(client_tier.tier) if client_tier else None
    next_action = next_actions_for(session, [(client_id, unit_fund_id)])[
        (client_id, unit_fund_id)
    ].action

    return BriefingFacts(
        tier_label=playbook.label if playbook else None,
        tier_goal=playbook.goal if playbook else None,
        tier_action=playbook.fa_action if playbook else None,
        next_action=f"{next_action.title}: {next_action.why}" if next_action else None,
        client_code=active.client_code,
        fund_name=_fund_name(session, unit_fund_id),
        risk_score=risk.risk_score,
        risk_band=risk.risk_band,
        route=risk.route,
        balance=active.balance if active.balance is not None else 0.0,
        balance_tier=risk.balance_tier or "Unknown",
        days_since_deposit=_days_since(active.last_deposit_date, reference_date),
        last_deposit_amount=active.last_deposit_amount,
        typical_gap_days=active.typical_gap_days,
        overdue_multiple=risk.overdue_multiple,
        typical_deposit_amount=active.avg_deposit_amount,
        largest_deposit_amount=active.max_deposit_amount,
        deposit_trend=active.deposit_trend,
        largest_withdrawal=active.largest_withdrawal,
        withdrawal_pct=_withdrawal_pct(active.largest_withdrawal, active.balance),
        days_since_withdrawal=_days_since(active.last_withdrawal_date, reference_date),
        signals=signals,
        deposit_count_capped=active.deposit_count_capped,
        withdrawal_history_hidden=active.withdrawal_history_hidden,
        holds_both_funds=_holds_both_funds(session, client_id, unit_fund_id),
        months_until_empty=active.months_until_empty,
        months_until_empty_threshold=_months_until_empty_threshold(session, risk.config_version),
        has_open_complaint=_has_open_complaint(session, client_id),
        recency_band=risk.recency_band,
        value_tier=risk.value_tier,
    )


def _basis(facts: BriefingFacts) -> list[str]:
    return [ADVISOR_SIGNAL_LABELS[sig] for sig in ADVISOR_SIGNAL_ORDER if facts.signals.get(sig)]


def get_briefing(
    session: Session,
    client_id: int,
    unit_fund_id: int,
    *,
    viewer_id: str,
    on_behalf_of: str | None = None,
    reference_date: date | None = None,
) -> BriefingView:
    ref = reference_date if reference_date is not None else date.today()
    facts = gather_briefing_facts(session, client_id, unit_fund_id, ref)
    if facts is None:
        raise BriefingNotFound(f"{client_id}/{unit_fund_id}")

    text = render_briefing(facts)
    mode = "deterministic"
    if get_settings().ai_briefing_enabled:
        stored = read_narrative(session, client_id, unit_fund_id, to_risk_fact_block(facts))
        if stored is not None:
            text = stored
            mode = "narrative"
    name = _client_name(client_id)

    record_audit(
        session,
        entity_type="risk_briefing",
        action="view",
        entity_id=str(client_id),
        actor_id=viewer_id,
        detail=with_on_behalf_of(
            {"unit_fund_id": unit_fund_id, "route": facts.route, "mode": mode}, on_behalf_of
        ),
    )
    session.commit()

    return BriefingView(
        client_id=client_id,
        unit_fund_id=unit_fund_id,
        client_name=name,
        text=text,
        basis=_basis(facts),
        mode=mode,
    )


def deposit_trend_band_of(deposit_trend: float | None) -> str:
    if deposit_trend is None:
        return "unknown"
    if abs(deposit_trend) < MIN_TREND_CHANGE:
        return "flat"
    return "falling" if deposit_trend < 0 else "rising"


def to_risk_fact_block(facts: BriefingFacts) -> RiskFactBlock:
    return RiskFactBlock(
        risk_band=facts.risk_band,
        route=facts.route,
        balance_tier=facts.balance_tier if facts.balance_tier in BALANCE_TIERS else None,
        recency_band=facts.recency_band,
        value_tier=facts.value_tier,
        deposit_trend_band=deposit_trend_band_of(facts.deposit_trend),
        fund_name=FUND_DISPLAY_NAMES.get(classify_fund_type(facts.fund_name)),
        sig_heavy_withdrawal=facts.signals.get("sig_heavy_withdrawal"),
        sig_dormant=facts.signals.get("sig_dormant"),
        sig_broken_pattern=facts.signals.get("sig_broken_pattern"),
        sig_shrinking=facts.signals.get("sig_shrinking"),
        near_preservation_line=facts.signals.get(NEAR_PRESERVATION_LINE),
        sig_never_repeated=facts.signals.get("sig_never_repeated"),
        deposit_count_capped=facts.deposit_count_capped,
        withdrawal_history_hidden=facts.withdrawal_history_hidden,
        holds_both_funds=facts.holds_both_funds,
        has_open_complaint=facts.has_open_complaint,
    )


def briefing_boundary_audit_sink(session: Session, entity_type: str = "risk_briefing") -> AuditSink:
    """Adapt run_model_boundary's BoundaryAudit into one audit_log row per crossing."""

    def sink(record: BoundaryAudit) -> None:
        record_audit(
            session,
            entity_type=entity_type,
            action="narrate",
            entity_id=record.entity_id,
            run_id=record.run_id,
            trace_id=record.trace_id,
            detail={
                "fields": record.fields,
                "inbound": record.inbound,
                "outbound": record.outbound,
                "reason": record.reason,
            },
        )

    return sink


def get_narrative_briefing(
    session: Session,
    client_id: int,
    unit_fund_id: int,
    *,
    viewer_id: str,
    on_behalf_of: str | None = None,
    llm_client: LLMClient,
    reference_date: date | None = None,
) -> BriefingView:
    """The optional model-narrated version of get_briefing's page.
    Raises NarrativeDisabled when AI briefing is off; a leak falls back to plain text."""
    if not get_settings().ai_briefing_enabled:
        raise NarrativeDisabled("AI-narrated briefings are not enabled")

    ref = reference_date if reference_date is not None else date.today()
    facts = gather_briefing_facts(session, client_id, unit_fund_id, ref)
    if facts is None:
        raise BriefingNotFound(f"{client_id}/{unit_fund_id}")

    fallback_text = render_briefing(facts)
    risk_fact_block = to_risk_fact_block(facts)

    instructions = prompt_text(session, CLIENT_BRIEF_NARRATIVE, ref)
    stored = read_narrative(session, client_id, unit_fund_id, risk_fact_block, instructions)
    if stored is not None:
        # Already drafted for these exact facts, by the nightly warm-up or by
        # an earlier look. Nothing crosses the model boundary, so there is no
        # crossing to audit; the view audit below still records the read.
        result = NarrativeResult(text=stored, mode="narrative")
    else:
        result = draft_narrative(
            risk_fact_block,
            llm_client,
            fallback_text=fallback_text,
            instructions=instructions,
            entity_id=str(client_id),
            audit=briefing_boundary_audit_sink(session),
        )
        if result.mode == "narrative":
            save_narrative(
                session,
                client_id,
                unit_fund_id,
                risk_fact_block,
                text=result.text,
                model=getattr(llm_client, "model", "unknown"),
                instructions=instructions,
            )

    name = _client_name(client_id)

    record_audit(
        session,
        entity_type="risk_briefing",
        action="view",
        entity_id=str(client_id),
        actor_id=viewer_id,
        detail=with_on_behalf_of(
            {"unit_fund_id": unit_fund_id, "route": facts.route, "mode": result.mode},
            on_behalf_of,
        ),
    )
    session.commit()

    return BriefingView(
        client_id=client_id,
        unit_fund_id=unit_fund_id,
        client_name=name,
        text=result.text,
        basis=_basis(facts),
        mode=result.mode,
    )
