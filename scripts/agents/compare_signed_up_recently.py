from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

from sqlalchemy import select  # noqa: E402

from app.agents.propose import GROUP_ACTIONS, group_skip_reasons, load_action_or_raise  # noqa: E402
from app.agents.signals import recompute_new_client_signals  # noqa: E402
from app.agents.situations import recompute_new_client_single_deposit  # noqa: E402
from app.agents.watchlist import (  # noqa: E402
    SIGNED_UP_RECENTLY,
    GroupMember,
    WatchGroup,
    load_thresholds,
    signed_up_recently,
)
from app.db.models.active_clients import ActiveClientFund  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402


def legacy_signed_up_recently(session, thresholds, as_of: date) -> WatchGroup:
    """The query signed_up_recently() ran before Todo 2, kept here only to
    prove the situation table agrees with it.
    """
    earliest = as_of - timedelta(days=thresholds.new_client_days)
    statement = select(
        ActiveClientFund.client_id, ActiveClientFund.unit_fund_id, ActiveClientFund.balance
    ).where(
        ActiveClientFund.n_deposits == 1,
        ActiveClientFund.first_deposit_date.is_not(None),
        ActiveClientFund.first_deposit_date >= earliest,
    )
    members = tuple(
        GroupMember(
            client_id=row.client_id,
            unit_fund_id=row.unit_fund_id,
            balance=float(row.balance or 0.0),
        )
        for row in session.execute(statement).all()
    )
    return WatchGroup(name=SIGNED_UP_RECENTLY, members=members)


def member_keys(group: WatchGroup) -> set[tuple[int, int]]:
    return {(member.client_id, member.unit_fund_id) for member in group.members}


def main() -> None:
    as_of = date.today()

    with SessionLocal() as session:
        thresholds = load_thresholds(session, as_of)
        run = recompute_new_client_signals(session, as_of)
        recompute_new_client_single_deposit(session, as_of, run.run_id)

    with SessionLocal() as session:
        legacy = legacy_signed_up_recently(session, thresholds, as_of)
        situation = signed_up_recently(session, thresholds, as_of)

        legacy_keys = member_keys(legacy)
        situation_keys = member_keys(situation)

        print(f"as of {as_of}, signal run {run.run_id}")
        print(f"legacy query members: {len(legacy_keys)}")
        print(f"situation table members: {len(situation_keys)}")

        only_legacy = legacy_keys - situation_keys
        only_situation = situation_keys - legacy_keys
        print(f"in legacy but not situation: {len(only_legacy)}")
        print(f"in situation but not legacy: {len(only_situation)}")
        for key in sorted(only_legacy)[:10]:
            print(f"  only legacy: {key}")
        for key in sorted(only_situation)[:10]:
            print(f"  only situation: {key}")

        mapped_code = GROUP_ACTIONS.get(SIGNED_UP_RECENTLY)
        print(f"GROUP_ACTIONS['{SIGNED_UP_RECENTLY}'] = {mapped_code!r}")
        action = load_action_or_raise(session, mapped_code, as_of)

        legacy_reasons = group_skip_reasons(session, legacy.members, action, as_of, None)
        situation_reasons = group_skip_reasons(session, situation.members, action, as_of, None)

        common_keys = legacy_keys & situation_keys
        mismatched_reasons = [
            key for key in common_keys if legacy_reasons.get(key) != situation_reasons.get(key)
        ]
        print(f"gate agreement checked for {len(common_keys)} shared members")
        print(f"gate reason mismatches: {len(mismatched_reasons)}")
        for key in mismatched_reasons[:10]:
            print(
                f"  {key}: legacy={legacy_reasons.get(key)!r} "
                f"situation={situation_reasons.get(key)!r}"
            )

        if not only_legacy and not only_situation and not mismatched_reasons:
            print("RESULT: the situation table agrees with the group's own query, exactly.")
        else:
            print("RESULT: mismatch found, see above.")


if __name__ == "__main__":
    main()
