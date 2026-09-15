from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agents.signals import FIRST_DEPOSIT_RECENT, SINGLE_DEPOSIT
from app.db.models.signals import (
    ClientSignalSnapshot,
    ClientSignalState,
    ClientSituationSnapshot,
    ClientSituationState,
)

NEW_CLIENT_SINGLE_DEPOSIT = "new_client_single_deposit"


def _signal_states(session: Session) -> dict[tuple[int, int], dict[str, bool]]:
    rows = session.execute(
        select(
            ClientSignalState.client_id,
            ClientSignalState.unit_fund_id,
            ClientSignalState.signal_code,
            ClientSignalState.is_active,
        ).where(ClientSignalState.signal_code.in_((SINGLE_DEPOSIT, FIRST_DEPOSIT_RECENT)))
    ).all()

    funds: dict[tuple[int, int], dict[str, bool]] = {}
    for row in rows:
        funds.setdefault((row.client_id, row.unit_fund_id), {})[row.signal_code] = row.is_active
    return funds


def _existing_state(
    session: Session, keys: list[tuple[int, int]]
) -> dict[tuple[int, int], ClientSituationState]:
    if not keys:
        return {}
    rows = session.scalars(
        select(ClientSituationState).where(
            ClientSituationState.situation_code == NEW_CLIENT_SINGLE_DEPOSIT,
            ClientSituationState.client_id.in_({key[0] for key in keys}),
        )
    )
    return {(row.client_id, row.unit_fund_id): row for row in rows}


def recompute_new_client_single_deposit(session: Session, as_of: date, run_id: str) -> None:
    funds = _signal_states(session)
    existing = _existing_state(session, list(funds.keys()))

    for (client_id, unit_fund_id), signals in funds.items():
        active_codes = [
            code for code in (SINGLE_DEPOSIT, FIRST_DEPOSIT_RECENT) if signals.get(code)
        ]
        is_active = len(active_codes) == 2

        session.add(
            ClientSituationSnapshot(
                run_id=run_id,
                client_id=client_id,
                unit_fund_id=unit_fund_id,
                situation_code=NEW_CLIENT_SINGLE_DEPOSIT,
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
                    situation_code=NEW_CLIENT_SINGLE_DEPOSIT,
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
    return tuple(
        code
        for code in (SINGLE_DEPOSIT, FIRST_DEPOSIT_RECENT)
        if previous.get(code, False) and not current.get(code, False)
    )
