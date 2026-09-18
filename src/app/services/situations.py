from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.signals import FIRST_DEPOSIT_RECENT
from app.agents.situations import NEW_CLIENT_SINGLE_DEPOSIT, resolved_signal_codes, situation_delta
from app.db.models.signals import ClientSituationSnapshot, SignalRun

KNOWN_SITUATION_CODES = (NEW_CLIENT_SINGLE_DEPOSIT,)

_EMPTY_DELTA = {
    "newly_active": 0,
    "newly_resolved": 0,
    "resolved_by_second_deposit": 0,
    "resolved_by_window_elapsed": 0,
    "resolved_by_both": 0,
    "persisting": 0,
}


class SituationNotFound(LookupError):
    pass


def _latest_run(session: Session, situation_code: str) -> tuple[str, datetime] | None:
    return session.execute(
        select(ClientSituationSnapshot.run_id, SignalRun.started_at)
        .join(SignalRun, SignalRun.run_id == ClientSituationSnapshot.run_id)
        .where(ClientSituationSnapshot.situation_code == situation_code)
        .order_by(SignalRun.started_at.desc())
        .limit(1)
    ).first()


def _resolution_counts(
    session: Session, run_id: str, newly_inactive: tuple[tuple[int, int], ...]
) -> dict[str, int]:
    counts = {
        "resolved_by_second_deposit": 0,
        "resolved_by_window_elapsed": 0,
        "resolved_by_both": 0,
    }
    for client_id, unit_fund_id in newly_inactive:
        codes = resolved_signal_codes(session, client_id, unit_fund_id, run_id)
        if codes == (FIRST_DEPOSIT_RECENT,):
            counts["resolved_by_window_elapsed"] += 1
        elif len(codes) == 1:
            counts["resolved_by_second_deposit"] += 1
        else:
            counts["resolved_by_both"] += 1
    return counts


def situation_delta_summary(session: Session, situation_code: str) -> dict:
    if situation_code not in KNOWN_SITUATION_CODES:
        raise SituationNotFound(situation_code)

    latest = _latest_run(session, situation_code)
    if latest is None:
        return {"situation_code": situation_code, "run_id": None, "run_at": None, **_EMPTY_DELTA}

    run_id, run_at = latest
    delta = situation_delta(session, situation_code, run_id)
    resolution_counts = _resolution_counts(session, run_id, delta.newly_inactive)

    return {
        "situation_code": situation_code,
        "run_id": run_id,
        "run_at": run_at,
        "newly_active": len(delta.newly_active),
        "newly_resolved": len(delta.newly_inactive),
        "persisting": len(delta.persisting),
        **resolution_counts,
    }
