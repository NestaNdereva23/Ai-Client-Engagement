"""Command line entry point to inspect one agent run after the fact.

Prints the run's plan and summary, every tool call it made (in order, with
input and output), and every proposal it wrote. Useful for a run started by
scripts/agents/run_nightly_agent.py earlier, or by the nightly scheduled
trigger, when you want to look at it again without re-reading log output.

Usage:
    uv run python scripts/agents/show_agent_run.py --run-id 42
    uv run python scripts/agents/show_agent_run.py --latest
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Make the app package importable when run as a plain script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

from sqlalchemy import select  # noqa: E402

from app.db.models.agent_proposal import AgentProposal, AgentProposalClient  # noqa: E402
from app.db.models.agent_run import AgentRun, AgentToolCall  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402


def _truncate(value: object, limit: int = 400) -> str:
    text = json.dumps(value, sort_keys=True, default=str)
    return text if len(text) <= limit else text[:limit] + "... (truncated)"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Show what one agent run did.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--run-id", type=int, help="Show this run.")
    group.add_argument("--latest", action="store_true", help="Show the most recent run.")
    args = parser.parse_args(argv)

    session = SessionLocal()
    try:
        if args.latest:
            run = session.scalar(select(AgentRun).order_by(AgentRun.run_id.desc()).limit(1))
            if run is None:
                print("no agent runs yet")
                return 1
        else:
            run = session.get(AgentRun, args.run_id)
            if run is None:
                print(f"no agent run with run_id={args.run_id}")
                return 1

        print(f"run_id={run.run_id} state={run.state} trigger={run.trigger}")
        print(f"started_at={run.started_at} finished_at={run.finished_at}")
        if run.state == "failed":
            print(f"failure_reason: {run.failure_reason}")
        print(f"\nplan:\n{run.plan_text}")
        print(f"\nsummary:\n{run.summary}")
        if run.cost_kes is not None:
            print(f"\ncost: {run.cost_kes:.2f} KES")

        tool_calls = session.scalars(
            select(AgentToolCall)
            .where(AgentToolCall.run_id == run.run_id)
            .order_by(AgentToolCall.ordinal)
        ).all()
        print(f"\ntool calls ({len(tool_calls)}):")
        for call in tool_calls:
            print(f"  [{call.ordinal}] {call.tool_name}({_truncate(call.tool_input, 200)})")
            print(f"       -> {_truncate(call.tool_output)}")

        proposals = session.scalars(
            select(AgentProposal).where(AgentProposal.run_id == run.run_id)
        ).all()
        print(f"\nproposals ({len(proposals)}):")
        for proposal in proposals:
            print(
                f"  proposal_id={proposal.proposal_id} group={proposal.group_name!r} "
                f"action={proposal.action_code!r} status={proposal.status!r} "
                f"clients={proposal.client_count}"
            )
            print(f"    reason: {proposal.reason}")
            members = session.scalars(
                select(AgentProposalClient).where(
                    AgentProposalClient.proposal_id == proposal.proposal_id
                )
            ).all()
            included = [m for m in members if m.included]
            excluded = [m for m in members if not m.included]
            print(f"    included={len(included)} excluded={len(excluded)}")
            if proposal.skip_reason_counts:
                print("    left out by reason:")
                for reason, count in sorted(
                    proposal.skip_reason_counts.items(), key=lambda pair: -pair[1]
                ):
                    print(f"      {reason}: {count}")
            for member in excluded[:5]:
                print(f"      excluded client_id={member.client_id}: {member.skip_reason}")
            if len(excluded) > 5:
                print(f"      ... and {len(excluded) - 5} more")
    finally:
        session.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
