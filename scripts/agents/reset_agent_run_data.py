from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

from sqlalchemy import func, select, true, update  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db.models.agent_insight import (  # noqa: E402
    AgentInsight,
    AgentInsightClient,
    AgentInsightFact,
)
from app.db.models.agent_proposal import AgentProposal, AgentProposalClient  # noqa: E402
from app.db.models.agent_run import AgentRun, AgentToolCall  # noqa: E402
from app.db.models.signals import (  # noqa: E402
    ClientSignalSnapshot,
    ClientSignalState,
    ClientSituationSnapshot,
    ClientSituationState,
    SignalRun,
)
from app.db.session import SessionLocal  # noqa: E402
from app.logging_config import configure_logging  # noqa: E402


def _in_or_all(column, ids: list | None):
    """column.in_(ids), or an unconditional true() when ids is None (no scoping)."""
    return true() if ids is None else column.in_(ids)


def _build_steps(run_ids: list[int] | None):
    """One (label, model, where_clause) per table, in child-before-parent order.

    Leaves ingestion and every campaign table alone; those are their own
    reset tools' job. The situation tables clear alongside the signal
    tables, not separately: they key off the same signal_run.run_id, so
    leaving them behind orphans that row and the delete below fails on its
    own foreign key.

    The signal and situation tables key off their own signal run id, a
    string unrelated to agent_run.run_id, so --run-id can't scope them.
    They always clear in full, regardless of scope.
    """
    insight_ids = select(AgentInsight.insight_id).where(_in_or_all(AgentInsight.run_id, run_ids))
    proposal_ids = select(AgentProposal.proposal_id).where(
        _in_or_all(AgentProposal.run_id, run_ids)
    )

    return [
        ("agent_insight_fact", AgentInsightFact, AgentInsightFact.insight_id.in_(insight_ids)),
        (
            "agent_insight_client",
            AgentInsightClient,
            AgentInsightClient.insight_id.in_(insight_ids),
        ),
        (
            "agent_proposal_client",
            AgentProposalClient,
            AgentProposalClient.proposal_id.in_(proposal_ids),
        ),
        ("agent_proposal", AgentProposal, _in_or_all(AgentProposal.run_id, run_ids)),
        ("agent_insight", AgentInsight, _in_or_all(AgentInsight.run_id, run_ids)),
        ("agent_tool_call", AgentToolCall, _in_or_all(AgentToolCall.run_id, run_ids)),
        ("agent_run", AgentRun, _in_or_all(AgentRun.run_id, run_ids)),
        ("client_signal_snapshot", ClientSignalSnapshot, true()),
        ("client_signal_state", ClientSignalState, true()),
        ("client_situation_snapshot", ClientSituationSnapshot, true()),
        ("client_situation_state", ClientSituationState, true()),
        ("signal_run", SignalRun, true()),
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Delete agent run, insight, proposal, and signal data for a clean agent re-test."
        )
    )
    parser.add_argument(
        "--run-id",
        type=int,
        action="append",
        dest="run_ids",
        help=(
            "Only clear this agent run (repeatable). Omit to clear every run. "
            "The signal tables always clear in full, since they have no agent run id to scope by."
        ),
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Actually delete. Without this flag, only counts are printed.",
    )
    args = parser.parse_args(argv)

    configure_logging(get_settings().log_level)

    with SessionLocal() as session:
        run_ids = args.run_ids
        if run_ids is not None:
            found = set(
                session.execute(
                    select(AgentRun.run_id).where(AgentRun.run_id.in_(run_ids))
                ).scalars()
            )
            missing = set(run_ids) - found
            if missing:
                parser.error(f"no agent run with id(s): {sorted(missing)}")

        print(f"scope: {'all agent runs' if run_ids is None else run_ids}")
        print(f"mode:  {'DELETE' if args.yes else 'dry run (pass --yes to actually delete)'}")
        print()

        if args.yes:
            _clear_insight_links(session, run_ids)

        steps = _build_steps(run_ids)
        total = 0
        for label, model, where_clause in steps:
            count = session.execute(
                select(func.count()).select_from(model).where(where_clause)
            ).scalar_one()
            total += count
            print(f"  {label:<32} {count}")
            if args.yes and count:
                session.query(model).filter(where_clause).delete(synchronize_session=False)

        if args.yes:
            session.commit()
            print(f"\ndeleted {total} row(s) total")
        else:
            print(f"\nwould delete {total} row(s) total (dry run, nothing changed)")

    return 0


def _clear_insight_links(session: Session, run_ids: list[int] | None) -> None:
    """Null agent_run.insight_id before agent_insight rows are deleted.

    agent_run.insight_id and agent_insight.run_id point at each other, so one
    side has to be cleared first or the delete below fails on its own foreign
    key.
    """
    session.execute(
        update(AgentRun).where(_in_or_all(AgentRun.run_id, run_ids)).values(insight_id=None)
    )


if __name__ == "__main__":
    raise SystemExit(main())
