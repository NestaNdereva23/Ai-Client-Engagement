"""Assemble and audit one client-fund's on-demand briefing.

Gathers the fact block render_briefing needs from client_risk_features and
active_client_fund, attaches the client's name last, and audits both the
name read and the briefing view itself, matching Section 19 of the
implementation plan.

get_briefing itself never calls a model. It will serve a narration that was
already drafted and stored for this client, when one matching today's facts
exists, but it never waits on one being drafted: with nothing stored it
returns the deterministic text, same as it always did.

get_narrative_briefing (AM15) reuses the exact same gathered facts to also
offer an optional model-narrated version, generated through
briefing.narrative's own crossing of the model boundary, with get_briefing's
own deterministic text as its fallback. What it accepts is stored, so the
next look at the same client costs nothing.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from sqlalchemy import select, tuple_
from sqlalchemy.orm import Session

from app.audit.log import record_audit
from app.briefing.narrative import NarrativeResult, draft_narrative
from app.briefing.render import BriefingFacts, render_briefing
from app.briefing.store import read_narrative, save_narrative
from app.config import get_settings
from app.db.models.active_clients import ActiveClientFund
from app.db.models.complaints import ClientComplaint
from app.db.models.models import Funds, PiiVault
from app.db.models.risk import ClientRiskFeatures, RiskConfigVersion
from app.db.session import restricted_session
from app.privacy.boundary import AuditSink, BoundaryAudit
from app.privacy.fact_block import FUND_DISPLAY_NAMES, RiskFactBlock
from app.privacy.llm_client import LLMClient
from app.risk.signals import SIGNAL_LABELS, SIGNAL_ORDER
from app.transform.active_features import BALANCE_TIERS
from app.transform.features import TREND_EPS
from app.transform.features import _fund_type as classify_fund_type


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
    active_features.py::_days_since_deposit uses, for the same reason: a
    date after the reference point is a data quirk, not a real future event.
    """
    if occurred is None:
        return None
    return max(0, (reference_date - occurred).days)


def _withdrawal_pct(largest_withdrawal: float | None, balance: float | None) -> float | None:
    """The largest withdrawal as a share of the balance implied before it
    happened. Mirrors active_features.py::_withdrawal_pct.
    """
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
    """The MONTHS_UNTIL_EMPTY threshold the config version that scored this
    client used, so the briefing's fee caveat is judged against the same
    number the score itself was computed with.
    """
    thresholds = session.scalar(
        select(RiskConfigVersion.thresholds).where(RiskConfigVersion.version == config_version)
    )
    return float(thresholds["MONTHS_UNTIL_EMPTY"]) if thresholds else float("inf")


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


def gather_briefing_facts(
    session: Session, client_id: int, unit_fund_id: int, reference_date: date
) -> BriefingFacts | None:
    """Everything render_briefing (and, for AM15, RiskFactBlock) need for
    one client-fund relationship, or None when there is not enough data to
    render anything.

    The one place both get_briefing and get_narrative_briefing gather their
    facts, so the deterministic text and the narrative's own grounding can
    never disagree about what is true for this client -- they are always
    built from the exact same BriefingFacts instance.
    """
    risk = session.get(ClientRiskFeatures, (client_id, unit_fund_id))
    active = session.get(ActiveClientFund, (client_id, unit_fund_id))
    if risk is None or active is None:
        return None

    signals = {name: getattr(risk, name) for name in SIGNAL_ORDER}

    return BriefingFacts(
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
    """The same fired signals the "WHY THIS CLIENT SURFACED" section of a
    rendered briefing lists, in the same order -- the discrete facts AM11
    actually weighed, not a paraphrase of them.
    """
    return [SIGNAL_LABELS[sig] for sig in SIGNAL_ORDER if facts.signals.get(sig)]


def get_briefing(
    session: Session,
    client_id: int,
    unit_fund_id: int,
    *,
    viewing_fa_id: str,
    reference_date: date | None = None,
) -> BriefingView:
    """Render one client-fund's briefing and audit the view.

    Raises BriefingNotFound when either the score or the behavioural row is
    missing -- there is not enough to render a page that means anything.

    Serves a narration instead of the deterministic text when one was
    already drafted for this client and still matches today's facts, which
    for the clients the digest surfaces is the normal case. Never drafts
    one: this read stays instant, and mode says which text came back.
    """
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
        actor_id=viewing_fa_id,
        detail={"unit_fund_id": unit_fund_id, "route": facts.route, "mode": mode},
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


def _deposit_trend_band(deposit_trend: float | None) -> str:
    """rising/flat/falling/unknown: the same three-way split render_briefing
    already makes inline with TREND_EPS, in prose, just given a name so it
    can be a RiskFactBlock field instead. Mirrors transform/features.py's
    own _trend_band, which bands the dormant book's ticket_trend the same
    way against the same constant.
    """
    if deposit_trend is None:
        return "unknown"
    if abs(deposit_trend) < TREND_EPS:
        return "flat"
    return "falling" if deposit_trend < 0 else "rising"


def to_risk_fact_block(facts: BriefingFacts) -> RiskFactBlock:
    """The band-only projection of facts a briefing narrative may see.

    Every field here is already a closed band or a boolean somewhere on
    facts; deposit_trend_band is the one new band, and it is a rename of a
    comparison render_briefing already makes (see _deposit_trend_band).
    render_briefing's "balance covers only N months of fees" caveat is the
    same comparison sig_going_dormant already made at scoring time (both
    read months_until_empty against the same config version's own
    MONTHS_UNTIL_EMPTY), so that signal alone covers it here -- no separate
    fee-runway flag. balance_tier falls back to "Unknown" for display in
    facts itself (render_briefing always wants text to print); that
    sentinel is not a real band member, so it maps to no fact here rather
    than failing RiskFactBlock's closed vocabulary. fund_name classifies
    facts.fund_name (the free-text name render_briefing prints) through
    transform.features's own money-market/high-yield name classifier --
    the same rule Phase 1 already uses to bucket a fund by name, reused
    rather than re-derived -- then maps the result to the same reviewed
    display name ModelFactBlock uses.
    """
    return RiskFactBlock(
        risk_band=facts.risk_band,
        route=facts.route,
        balance_tier=facts.balance_tier if facts.balance_tier in BALANCE_TIERS else None,
        recency_band=facts.recency_band,
        value_tier=facts.value_tier,
        deposit_trend_band=_deposit_trend_band(facts.deposit_trend),
        fund_name=FUND_DISPLAY_NAMES.get(classify_fund_type(facts.fund_name)),
        sig_heavy_withdrawal=facts.signals.get("sig_heavy_withdrawal"),
        sig_dormant=facts.signals.get("sig_dormant"),
        sig_broken_pattern=facts.signals.get("sig_broken_pattern"),
        sig_shrinking=facts.signals.get("sig_shrinking"),
        sig_going_dormant=facts.signals.get("sig_going_dormant"),
        sig_never_repeated=facts.signals.get("sig_never_repeated"),
        deposit_count_capped=facts.deposit_count_capped,
        withdrawal_history_hidden=facts.withdrawal_history_hidden,
        holds_both_funds=facts.holds_both_funds,
        has_open_complaint=facts.has_open_complaint,
    )


def briefing_boundary_audit_sink(session: Session) -> AuditSink:
    """Adapt run_model_boundary's BoundaryAudit into one audit_log row per
    crossing, the same pattern agents.graph's own callers wire up for
    Phase 1 generation.
    """

    def sink(record: BoundaryAudit) -> None:
        record_audit(
            session,
            entity_type="risk_briefing",
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
    viewing_fa_id: str,
    llm_client: LLMClient,
    reference_date: date | None = None,
) -> BriefingView:
    """The optional, model-narrated version of get_briefing's same page (AM15).

    Raises NarrativeDisabled when settings.ai_briefing_enabled is off --
    checked here, not only in the router, so a second caller can't
    accidentally skip the gate. Raises BriefingNotFound on the same missing
    data get_briefing itself raises on. Never raises on a boundary leak or
    an ungrounded narrative: draft_narrative falls back to the deterministic
    text instead, and the response's mode field says which one was actually
    returned.
    """
    if not get_settings().ai_briefing_enabled:
        raise NarrativeDisabled("AI-narrated briefings are not enabled")

    ref = reference_date if reference_date is not None else date.today()
    facts = gather_briefing_facts(session, client_id, unit_fund_id, ref)
    if facts is None:
        raise BriefingNotFound(f"{client_id}/{unit_fund_id}")

    fallback_text = render_briefing(facts)
    risk_fact_block = to_risk_fact_block(facts)

    stored = read_narrative(session, client_id, unit_fund_id, risk_fact_block)
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
            )

    name = _client_name(client_id)

    record_audit(
        session,
        entity_type="risk_briefing",
        action="view",
        entity_id=str(client_id),
        actor_id=viewing_fa_id,
        detail={"unit_fund_id": unit_fund_id, "route": facts.route, "mode": result.mode},
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
