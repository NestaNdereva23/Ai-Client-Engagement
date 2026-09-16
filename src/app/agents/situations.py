from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agents.signals import (
    CALL_FOLLOW_UP_OVERDUE,
    FEE_PRESSURE_CLOSE,
    FIRST_DEPOSIT_RECENT,
    ROUTE_ESCALATED_THIS_RUN,
    SINGLE_DEPOSIT,
    SINGLE_FUND_HELD,
    SMALL_BALANCE,
)
from app.db.models.risk import ClientRiskFeatures
from app.db.models.signals import (
    ClientSignalSnapshot,
    ClientSignalState,
    ClientSituationSnapshot,
    ClientSituationState,
)

NEW_CLIENT_SINGLE_DEPOSIT = "new_client_single_deposit"
SYSTEM_FEE_PRESSURE = "system_fee_pressure"
SMALL_BALANCE_INACTIVE = "small_balance_inactive"
CONTRIBUTION_DECLINE = "contribution_decline"
SINGLE_FUND_HEALTHY = "single_fund_healthy"
FOLLOW_UP_OVERDUE = "follow_up_overdue"
RISK_ACTION_GAP = "risk_action_gap"
FEE_PRESSURE_GONE_QUIET = "fee_pressure_gone_quiet"
FEE_PRESSURE_ACTIVE_CONTRIBUTOR = "fee_pressure_active_contributor"

SIG_DORMANT = "sig_dormant"
SIG_SHRINKING = "sig_shrinking"
HEALTHY_RISK_BAND = "healthy_risk_band"
HEALTHY_BANDS = ("None", "Low")

SITUATION_CODES = (
    NEW_CLIENT_SINGLE_DEPOSIT,
    SYSTEM_FEE_PRESSURE,
    SMALL_BALANCE_INACTIVE,
    CONTRIBUTION_DECLINE,
    SINGLE_FUND_HEALTHY,
    FOLLOW_UP_OVERDUE,
    RISK_ACTION_GAP,
    FEE_PRESSURE_GONE_QUIET,
    FEE_PRESSURE_ACTIVE_CONTRIBUTOR,
)


def _signal_state_map(
    session: Session, signal_codes: tuple[str, ...]
) -> dict[tuple[int, int], dict[str, bool]]:
    rows = session.execute(
        select(
            ClientSignalState.client_id,
            ClientSignalState.unit_fund_id,
            ClientSignalState.signal_code,
            ClientSignalState.is_active,
        ).where(ClientSignalState.signal_code.in_(signal_codes))
    ).all()

    funds: dict[tuple[int, int], dict[str, bool]] = {}
    for row in rows:
        funds.setdefault((row.client_id, row.unit_fund_id), {})[row.signal_code] = row.is_active
    return funds


def _risk_signal_map(session: Session) -> dict[tuple[int, int], dict[str, bool]]:
    rows = session.execute(
        select(
            ClientRiskFeatures.client_id,
            ClientRiskFeatures.unit_fund_id,
            ClientRiskFeatures.sig_dormant,
            ClientRiskFeatures.sig_shrinking,
            ClientRiskFeatures.risk_band,
        )
    ).all()

    funds: dict[tuple[int, int], dict[str, bool]] = {}
    for row in rows:
        funds[(row.client_id, row.unit_fund_id)] = {
            SIG_DORMANT: row.sig_dormant,
            SIG_SHRINKING: row.sig_shrinking,
            HEALTHY_RISK_BAND: row.risk_band in HEALTHY_BANDS,
        }
    return funds


def _combine(
    *sources: dict[tuple[int, int], dict[str, bool]],
) -> dict[tuple[int, int], dict[str, bool]]:
    combined: dict[tuple[int, int], dict[str, bool]] = {}
    for source in sources:
        for key, values in source.items():
            combined.setdefault(key, {}).update(values)
    return combined


def _existing_situation_state(
    session: Session, situation_code: str, keys: list[tuple[int, int]]
) -> dict[tuple[int, int], ClientSituationState]:
    if not keys:
        return {}
    rows = session.scalars(
        select(ClientSituationState).where(
            ClientSituationState.situation_code == situation_code,
            ClientSituationState.client_id.in_({key[0] for key in keys}),
        )
    )
    return {(row.client_id, row.unit_fund_id): row for row in rows}


def _recompute_situation(
    session: Session,
    situation_code: str,
    as_of: date,
    run_id: str,
    facts: dict[tuple[int, int], dict[str, bool]],
    required_codes: tuple[str, ...],
    excluded_codes: tuple[str, ...] = (),
) -> None:
    existing = _existing_situation_state(session, situation_code, list(facts.keys()))

    for (client_id, unit_fund_id), values in facts.items():
        active_codes = [code for code in required_codes if values.get(code)]
        is_active = len(active_codes) == len(required_codes) and not any(
            values.get(code) for code in excluded_codes
        )

        session.add(
            ClientSituationSnapshot(
                run_id=run_id,
                client_id=client_id,
                unit_fund_id=unit_fund_id,
                situation_code=situation_code,
                is_active=is_active,
                signal_codes=active_codes,
            )
        )

        prior = existing.get((client_id, unit_fund_id))
        if prior is None:
            session.add(
                ClientSituationState(
                    client_id=client_id,
                    unit_fund_id=unit_fund_id,
                    situation_code=situation_code,
                    is_active=is_active,
                    signal_codes=active_codes,
                    since=as_of,
                    run_id=run_id,
                )
            )
        else:
            if prior.is_active != is_active:
                prior.since = as_of
            prior.is_active = is_active
            prior.signal_codes = active_codes
            prior.run_id = run_id

    session.commit()


def recompute_new_client_single_deposit(session: Session, as_of: date, run_id: str) -> None:
    facts = _signal_state_map(session, (SINGLE_DEPOSIT, FIRST_DEPOSIT_RECENT))
    required_codes = (SINGLE_DEPOSIT, FIRST_DEPOSIT_RECENT)
    _recompute_situation(session, NEW_CLIENT_SINGLE_DEPOSIT, as_of, run_id, facts, required_codes)


def recompute_system_fee_pressure(session: Session, as_of: date, run_id: str) -> None:
    facts = _signal_state_map(session, (FEE_PRESSURE_CLOSE,))
    _recompute_situation(session, SYSTEM_FEE_PRESSURE, as_of, run_id, facts, (FEE_PRESSURE_CLOSE,))


def recompute_fee_pressure_gone_quiet(session: Session, as_of: date, run_id: str) -> None:
    facts = _combine(_signal_state_map(session, (FEE_PRESSURE_CLOSE,)), _risk_signal_map(session))
    _recompute_situation(
        session,
        FEE_PRESSURE_GONE_QUIET,
        as_of,
        run_id,
        facts,
        (FEE_PRESSURE_CLOSE, SIG_DORMANT),
    )


def recompute_fee_pressure_active_contributor(session: Session, as_of: date, run_id: str) -> None:
    facts = _combine(_signal_state_map(session, (FEE_PRESSURE_CLOSE,)), _risk_signal_map(session))
    _recompute_situation(
        session,
        FEE_PRESSURE_ACTIVE_CONTRIBUTOR,
        as_of,
        run_id,
        facts,
        (FEE_PRESSURE_CLOSE,),
        excluded_codes=(SIG_DORMANT,),
    )


def recompute_small_balance_inactive(session: Session, as_of: date, run_id: str) -> None:
    facts = _combine(_signal_state_map(session, (SMALL_BALANCE,)), _risk_signal_map(session))
    _recompute_situation(
        session, SMALL_BALANCE_INACTIVE, as_of, run_id, facts, (SMALL_BALANCE, SIG_DORMANT)
    )


def recompute_contribution_decline(session: Session, as_of: date, run_id: str) -> None:
    facts = _risk_signal_map(session)
    _recompute_situation(session, CONTRIBUTION_DECLINE, as_of, run_id, facts, (SIG_SHRINKING,))


def recompute_single_fund_healthy(session: Session, as_of: date, run_id: str) -> None:
    facts = _combine(_signal_state_map(session, (SINGLE_FUND_HELD,)), _risk_signal_map(session))
    _recompute_situation(
        session,
        SINGLE_FUND_HEALTHY,
        as_of,
        run_id,
        facts,
        (SINGLE_FUND_HELD, HEALTHY_RISK_BAND),
    )


def recompute_follow_up_overdue(session: Session, as_of: date, run_id: str) -> None:
    facts = _signal_state_map(session, (CALL_FOLLOW_UP_OVERDUE,))
    required_codes = (CALL_FOLLOW_UP_OVERDUE,)
    _recompute_situation(session, FOLLOW_UP_OVERDUE, as_of, run_id, facts, required_codes)


def recompute_risk_action_gap(session: Session, as_of: date, run_id: str) -> None:
    facts = _signal_state_map(session, (ROUTE_ESCALATED_THIS_RUN,))
    _recompute_situation(
        session, RISK_ACTION_GAP, as_of, run_id, facts, (ROUTE_ESCALATED_THIS_RUN,)
    )


def recompute_all_situations(session: Session, as_of: date, run_id: str) -> None:
    recompute_new_client_single_deposit(session, as_of, run_id)
    recompute_system_fee_pressure(session, as_of, run_id)
    recompute_fee_pressure_gone_quiet(session, as_of, run_id)
    recompute_fee_pressure_active_contributor(session, as_of, run_id)
    recompute_small_balance_inactive(session, as_of, run_id)
    recompute_contribution_decline(session, as_of, run_id)
    recompute_single_fund_healthy(session, as_of, run_id)
    recompute_follow_up_overdue(session, as_of, run_id)
    recompute_risk_action_gap(session, as_of, run_id)


@dataclass(frozen=True)
class SituationDelta:
    """What changed in one run, against the run before it."""

    newly_active: tuple[tuple[int, int], ...]
    newly_inactive: tuple[tuple[int, int], ...]
    persisting: tuple[tuple[int, int], ...]


def _situation_snapshots_for_run(
    session: Session, situation_code: str, run_id: str
) -> dict[tuple[int, int], bool]:
    rows = session.execute(
        select(
            ClientSituationSnapshot.client_id,
            ClientSituationSnapshot.unit_fund_id,
            ClientSituationSnapshot.is_active,
        ).where(
            ClientSituationSnapshot.situation_code == situation_code,
            ClientSituationSnapshot.run_id == run_id,
        )
    )
    return {(row.client_id, row.unit_fund_id): row.is_active for row in rows}


def _situation_snapshots_before_run(
    session: Session, situation_code: str, run_id: str
) -> dict[tuple[int, int], bool]:
    first_snapshot = (
        select(func.min(ClientSituationSnapshot.snapshot_id))
        .where(
            ClientSituationSnapshot.situation_code == situation_code,
            ClientSituationSnapshot.run_id == run_id,
        )
        .scalar_subquery()
    )
    rows = session.execute(
        select(
            ClientSituationSnapshot.client_id,
            ClientSituationSnapshot.unit_fund_id,
            ClientSituationSnapshot.is_active,
        )
        .where(
            ClientSituationSnapshot.situation_code == situation_code,
            ClientSituationSnapshot.snapshot_id < first_snapshot,
        )
        .distinct(ClientSituationSnapshot.client_id, ClientSituationSnapshot.unit_fund_id)
        .order_by(
            ClientSituationSnapshot.client_id,
            ClientSituationSnapshot.unit_fund_id,
            ClientSituationSnapshot.snapshot_id.desc(),
        )
    )
    return {(row.client_id, row.unit_fund_id): row.is_active for row in rows}


def situation_delta(session: Session, situation_code: str, run_id: str) -> SituationDelta:
    """How this run's situation membership compares to the run before it.

    A client fund with no earlier snapshot counts as having been inactive,
    the same "no history yet" reads as false rather than a fabricated state.
    """
    current = _situation_snapshots_for_run(session, situation_code, run_id)
    previous = _situation_snapshots_before_run(session, situation_code, run_id)

    newly_active = tuple(
        key for key, active in current.items() if active and not previous.get(key, False)
    )
    newly_inactive = tuple(
        key for key, active in current.items() if not active and previous.get(key, False)
    )
    persisting = tuple(
        key for key, active in current.items() if active and previous.get(key, False)
    )
    return SituationDelta(
        newly_active=newly_active, newly_inactive=newly_inactive, persisting=persisting
    )


def _signal_states_for_run(
    session: Session, run_id: str, client_id: int, unit_fund_id: int
) -> dict[str, bool]:
    rows = session.execute(
        select(ClientSignalSnapshot.signal_code, ClientSignalSnapshot.is_active).where(
            ClientSignalSnapshot.run_id == run_id,
            ClientSignalSnapshot.client_id == client_id,
            ClientSignalSnapshot.unit_fund_id == unit_fund_id,
        )
    )
    return {row.signal_code: row.is_active for row in rows}


def _signal_states_before_run(
    session: Session, run_id: str, client_id: int, unit_fund_id: int
) -> dict[str, bool]:
    first_snapshot = (
        select(func.min(ClientSignalSnapshot.snapshot_id))
        .where(
            ClientSignalSnapshot.run_id == run_id,
            ClientSignalSnapshot.client_id == client_id,
            ClientSignalSnapshot.unit_fund_id == unit_fund_id,
        )
        .scalar_subquery()
    )
    rows = session.execute(
        select(ClientSignalSnapshot.signal_code, ClientSignalSnapshot.is_active)
        .where(
            ClientSignalSnapshot.client_id == client_id,
            ClientSignalSnapshot.unit_fund_id == unit_fund_id,
            ClientSignalSnapshot.snapshot_id < first_snapshot,
        )
        .distinct(ClientSignalSnapshot.signal_code)
        .order_by(ClientSignalSnapshot.signal_code, ClientSignalSnapshot.snapshot_id.desc())
    )
    return {row.signal_code: row.is_active for row in rows}


def resolved_signal_codes(
    session: Session, client_id: int, unit_fund_id: int, run_id: str
) -> tuple[str, ...]:
    """Which signal codes turned false in this run for one client fund.

    Read off client_signal_snapshot for this run and the one before it, so
    the reason a situation stopped being active is provable from the signal
    history rather than guessed from the current state alone.
    """
    current = _signal_states_for_run(session, run_id, client_id, unit_fund_id)
    previous = _signal_states_before_run(session, run_id, client_id, unit_fund_id)
    from app.agents.signals import SIGNAL_CODES

    return tuple(
        code for code in SIGNAL_CODES if previous.get(code, False) and not current.get(code, False)
    )
