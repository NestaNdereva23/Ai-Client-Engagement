from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from psycopg.types.json import Jsonb
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.agents.signals import (
    ADVISOR_CALL_OVERDUE,
    FIRST_DEPOSIT_RECENT,
    NEAR_PRESERVATION_LINE,
    NET_FLOW_NEGATIVE,
    NO_RECENT_PURCHASE,
    PAST_TYPICAL_GAP,
    PURCHASED_THIS_PERIOD,
    REAL_WITHDRAWER,
    RISK_BAND_ROSE,
    SEGMENT_MOVED_UP,
    SIDE_CHANGED_DOWNWARD,
    SINGLE_DEPOSIT,
    SINGLE_FUND_HELD,
)
from app.db.bulk import copy_insert, copy_upsert
from app.db.models.risk import ClientRiskFeatures
from app.db.models.routing import ClientTier
from app.db.models.signals import (
    ClientSignalSnapshot,
    ClientSignalState,
    ClientSituationSnapshot,
    ClientSituationState,
    SituationRunCount,
)

GRADUAL_DEPOSITOR_DUE = "gradual_depositor_due"
GRADUAL_DEPOSITOR_WITHDRAWING = "gradual_depositor_withdrawing"
FREQUENT_DEPOSITOR_ONE_FUND = "frequent_depositor_one_fund"
FREQUENT_DEPOSITOR_WITHDRAWING = "frequent_depositor_withdrawing"
APPROACHING_PRESERVATION_LINE = "approaching_preservation_line"
CROSSED_BELOW_LINE = "crossed_below_line"
DEPOSIT_SIZE_FALLING = "deposit_size_falling"
FIRST_DEPOSIT_NO_REPEAT = "first_deposit_no_repeat"
FA_CALL_OVERDUE = "fa_call_overdue"
RISK_ROSE_NO_CALL = "risk_rose_no_call"
MOVED_TO_ACTIVE_ACTIVE = "moved_to_active_active"

SIG_SHRINKING = "sig_shrinking"

SITUATION_CODES = (
    GRADUAL_DEPOSITOR_DUE,
    GRADUAL_DEPOSITOR_WITHDRAWING,
    FREQUENT_DEPOSITOR_ONE_FUND,
    FREQUENT_DEPOSITOR_WITHDRAWING,
    APPROACHING_PRESERVATION_LINE,
    CROSSED_BELOW_LINE,
    DEPOSIT_SIZE_FALLING,
    FIRST_DEPOSIT_NO_REPEAT,
    FA_CALL_OVERDUE,
    RISK_ROSE_NO_CALL,
    MOVED_TO_ACTIVE_ACTIVE,
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
            ClientRiskFeatures.sig_shrinking,
        )
    ).all()
    return {(row.client_id, row.unit_fund_id): {SIG_SHRINKING: row.sig_shrinking} for row in rows}


def _only_active_book(
    session: Session, facts: dict[tuple[int, int], dict[str, bool]]
) -> dict[tuple[int, int], dict[str, bool]]:
    active_book = set(
        session.scalars(select(ClientTier.client_id).where(ClientTier.segment.is_not(None)))
    )
    return {key: (values if key[0] in active_book else {}) for key, values in facts.items()}


# since only moves when a situation flips; an unchanged situation keeps its date.
_SITUATION_STATE_SET_SQL = (
    "is_active = EXCLUDED.is_active, "
    "signal_codes = EXCLUDED.signal_codes, "
    "run_id = EXCLUDED.run_id, "
    "updated_at = now(), "
    "since = CASE WHEN client_situation_state.is_active <> EXCLUDED.is_active "
    "THEN EXCLUDED.since ELSE client_situation_state.since END"
)


def _recompute_situation(
    session: Session,
    situation_code: str,
    as_of: date,
    run_id: str,
    facts: dict[tuple[int, int], dict[str, bool]],
    required_codes: tuple[str, ...],
    excluded_codes: tuple[str, ...] = (),
) -> None:
    snapshots: list[dict] = []
    states: list[dict] = []
    for (client_id, unit_fund_id), values in facts.items():
        active_codes = [code for code in required_codes if values.get(code)]
        is_active = len(active_codes) == len(required_codes) and not any(
            values.get(code) for code in excluded_codes
        )
        codes_json = Jsonb(active_codes)
        snapshots.append(
            {
                "run_id": run_id,
                "client_id": client_id,
                "unit_fund_id": unit_fund_id,
                "situation_code": situation_code,
                "is_active": is_active,
                "signal_codes": codes_json,
            }
        )
        states.append(
            {
                "client_id": client_id,
                "unit_fund_id": unit_fund_id,
                "situation_code": situation_code,
                "is_active": is_active,
                "signal_codes": codes_json,
                "since": as_of,
                "run_id": run_id,
            }
        )

    copy_insert(session, ClientSituationSnapshot.__tablename__, snapshots)
    copy_upsert(
        session,
        ClientSituationState.__tablename__,
        states,
        ["client_id", "unit_fund_id", "situation_code"],
        _SITUATION_STATE_SET_SQL,
    )
    session.commit()


def recompute_gradual_depositor_due(session: Session, as_of: date, run_id: str) -> None:
    facts = _signal_state_map(session, (NO_RECENT_PURCHASE, PAST_TYPICAL_GAP, REAL_WITHDRAWER))
    _recompute_situation(
        session,
        GRADUAL_DEPOSITOR_DUE,
        as_of,
        run_id,
        facts,
        (NO_RECENT_PURCHASE, PAST_TYPICAL_GAP),
        excluded_codes=(REAL_WITHDRAWER,),
    )


def recompute_gradual_depositor_withdrawing(session: Session, as_of: date, run_id: str) -> None:
    facts = _signal_state_map(session, (NO_RECENT_PURCHASE, REAL_WITHDRAWER))
    required_codes = (NO_RECENT_PURCHASE, REAL_WITHDRAWER)
    _recompute_situation(
        session, GRADUAL_DEPOSITOR_WITHDRAWING, as_of, run_id, facts, required_codes
    )


def recompute_frequent_depositor_one_fund(session: Session, as_of: date, run_id: str) -> None:
    facts = _signal_state_map(session, (PURCHASED_THIS_PERIOD, SINGLE_FUND_HELD, REAL_WITHDRAWER))
    _recompute_situation(
        session,
        FREQUENT_DEPOSITOR_ONE_FUND,
        as_of,
        run_id,
        facts,
        (PURCHASED_THIS_PERIOD, SINGLE_FUND_HELD),
        excluded_codes=(REAL_WITHDRAWER,),
    )


def recompute_frequent_depositor_withdrawing(session: Session, as_of: date, run_id: str) -> None:
    facts = _signal_state_map(session, (PURCHASED_THIS_PERIOD, REAL_WITHDRAWER, NET_FLOW_NEGATIVE))
    required_codes = (PURCHASED_THIS_PERIOD, REAL_WITHDRAWER, NET_FLOW_NEGATIVE)
    _recompute_situation(
        session, FREQUENT_DEPOSITOR_WITHDRAWING, as_of, run_id, facts, required_codes
    )


def recompute_approaching_preservation_line(session: Session, as_of: date, run_id: str) -> None:
    facts = _only_active_book(session, _signal_state_map(session, (NEAR_PRESERVATION_LINE,)))
    required_codes = (NEAR_PRESERVATION_LINE,)
    _recompute_situation(
        session, APPROACHING_PRESERVATION_LINE, as_of, run_id, facts, required_codes
    )


def recompute_crossed_below_line(session: Session, as_of: date, run_id: str) -> None:
    facts = _signal_state_map(session, (SIDE_CHANGED_DOWNWARD,))
    _recompute_situation(
        session, CROSSED_BELOW_LINE, as_of, run_id, facts, (SIDE_CHANGED_DOWNWARD,)
    )


def recompute_deposit_size_falling(session: Session, as_of: date, run_id: str) -> None:
    facts = _only_active_book(session, _risk_signal_map(session))
    _recompute_situation(session, DEPOSIT_SIZE_FALLING, as_of, run_id, facts, (SIG_SHRINKING,))


def recompute_first_deposit_no_repeat(session: Session, as_of: date, run_id: str) -> None:
    facts = _only_active_book(
        session, _signal_state_map(session, (SINGLE_DEPOSIT, FIRST_DEPOSIT_RECENT))
    )
    required_codes = (SINGLE_DEPOSIT, FIRST_DEPOSIT_RECENT)
    _recompute_situation(session, FIRST_DEPOSIT_NO_REPEAT, as_of, run_id, facts, required_codes)


def recompute_fa_call_overdue(session: Session, as_of: date, run_id: str) -> None:
    facts = _only_active_book(session, _signal_state_map(session, (ADVISOR_CALL_OVERDUE,)))
    _recompute_situation(session, FA_CALL_OVERDUE, as_of, run_id, facts, (ADVISOR_CALL_OVERDUE,))


def recompute_risk_rose_no_call(session: Session, as_of: date, run_id: str) -> None:
    facts = _only_active_book(session, _signal_state_map(session, (RISK_BAND_ROSE,)))
    _recompute_situation(session, RISK_ROSE_NO_CALL, as_of, run_id, facts, (RISK_BAND_ROSE,))


def recompute_moved_to_active_active(session: Session, as_of: date, run_id: str) -> None:
    facts = _only_active_book(session, _signal_state_map(session, (SEGMENT_MOVED_UP,)))
    _recompute_situation(session, MOVED_TO_ACTIVE_ACTIVE, as_of, run_id, facts, (SEGMENT_MOVED_UP,))


def record_situation_counts(session: Session, run_id: str) -> None:
    counts = session.execute(
        select(
            ClientSituationSnapshot.situation_code,
            func.count().filter(ClientSituationSnapshot.is_active.is_(True)),
        )
        .where(ClientSituationSnapshot.run_id == run_id)
        .group_by(ClientSituationSnapshot.situation_code)
    ).all()
    session.execute(delete(SituationRunCount).where(SituationRunCount.run_id == run_id))
    session.add_all(
        SituationRunCount(run_id=run_id, situation_code=code, active_count=active)
        for code, active in counts
    )
    session.commit()


def recompute_all_situations(session: Session, as_of: date, run_id: str) -> None:
    recompute_gradual_depositor_due(session, as_of, run_id)
    recompute_gradual_depositor_withdrawing(session, as_of, run_id)
    recompute_frequent_depositor_one_fund(session, as_of, run_id)
    recompute_frequent_depositor_withdrawing(session, as_of, run_id)
    recompute_approaching_preservation_line(session, as_of, run_id)
    recompute_crossed_below_line(session, as_of, run_id)
    recompute_deposit_size_falling(session, as_of, run_id)
    recompute_first_deposit_no_repeat(session, as_of, run_id)
    recompute_fa_call_overdue(session, as_of, run_id)
    recompute_risk_rose_no_call(session, as_of, run_id)
    recompute_moved_to_active_active(session, as_of, run_id)
    record_situation_counts(session, run_id)


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
    A fund with no earlier snapshot counts as having been inactive."""
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
    Read off client_signal_snapshot for this run and the one before it."""
    current = _signal_states_for_run(session, run_id, client_id, unit_fund_id)
    previous = _signal_states_before_run(session, run_id, client_id, unit_fund_id)
    from app.agents.signals import SIGNAL_CODES

    return tuple(
        code for code in SIGNAL_CODES if previous.get(code, False) and not current.get(code, False)
    )
