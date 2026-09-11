"""Command line report of who the proposal gates leave out, and why.

Answers "which check emptied this group" as a table instead of a guess. Two
modes:

- Live (default): builds tonight's watch list and runs the same gates
  propose_watchlist would, using the rule table's mapped action for each
  group. Nothing is written; this is a dry run over the current data.
- --run-id: reads back what a stored run actually proposed, using the
  per reason counts saved on each agent_proposal row.

Usage:
    uv run python scripts/agents/report_gate_exclusions.py
    uv run python scripts/agents/report_gate_exclusions.py --as-of 2026-09-08
    uv run python scripts/agents/report_gate_exclusions.py --group fees_will_empty
    uv run python scripts/agents/report_gate_exclusions.py --run-id 18
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

# Make the app package importable when run as a plain script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

from sqlalchemy import func, select  # noqa: E402

from app.agents.propose import (  # noqa: E402
    GROUP_ACTIONS,
    ProposalActionMissing,
    group_skip_reasons,
    load_action_or_raise,
    skip_reason_counts,
)
from app.agents.watchlist import build_watchlist, load_thresholds  # noqa: E402
from app.db.models.agent_proposal import AgentProposal, AgentProposalClient  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402


def _parse_date(value: str) -> date:
    return date.fromisoformat(value)


def _print_table(rows: list[tuple[str, int, int, dict[str, int]]]) -> None:
    """rows are (group_name, fund_total, included, {reason: count}), one row
    per group. Client funds throughout, not distinct clients, since that is
    what the gates and skip_reason_counts are keyed on.
    """
    total_all = sum(total for _, total, _, _ in rows)
    excluded_all = sum(total - included for _, total, included, _ in rows)
    print(f"{'group':<28} {'funds':>7} {'included':>9} {'excluded':>9}  by reason")
    for name, total, included, reasons in rows:
        excluded = total - included
        reason_text = ", ".join(f"{r}={c}" for r, c in sorted(reasons.items(), key=lambda p: -p[1]))
        print(f"{name:<28} {total:>7} {included:>9} {excluded:>9}  {reason_text}")
    print(f"\n{total_all} client funds across every group, {excluded_all} left out by a check.")


def _live_report(as_of: date, only_group: str | None) -> int:
    with SessionLocal() as session:
        thresholds = load_thresholds(session, as_of)
        rows = []
        for group in build_watchlist(session, as_of, thresholds):
            if only_group is not None and group.name != only_group:
                continue
            if not group.members:
                rows.append((group.name, 0, 0, {}))
                continue
            action_code = GROUP_ACTIONS.get(group.name)
            if action_code is None:
                raise ProposalActionMissing(
                    f"the group '{group.name}' has no action in the rule table"
                )
            action = load_action_or_raise(session, action_code, as_of)
            reasons = group_skip_reasons(session, group.members, action, as_of, None)
            included = sum(1 for reason in reasons.values() if reason is None)
            # Client funds, not distinct clients: a client with two funds in
            # the same group is one client_count but two entries here, and
            # skip_reasons is keyed per fund, so the two must match to add up.
            rows.append((group.name, len(group.members), included, skip_reason_counts(reasons)))

    if not rows:
        print(f"no group named {only_group!r} tonight" if only_group else "no groups tonight")
        return 1
    _print_table(rows)
    return 0


def _run_report(run_id: int, only_group: str | None) -> int:
    with SessionLocal() as session:
        proposals = session.scalars(
            select(AgentProposal).where(AgentProposal.run_id == run_id)
        ).all()
        if not proposals:
            print(f"no proposals recorded for run_id={run_id}")
            return 1
        rows = []
        for p in proposals:
            if only_group is not None and p.group_name != only_group:
                continue
            # Count client funds directly rather than trust client_count
            # (distinct clients) to line up with the reason tally (per
            # fund): a client with two funds in the group makes the two
            # counts differ by design.
            fund_total = session.scalar(
                select(func.count())
                .select_from(AgentProposalClient)
                .where(AgentProposalClient.proposal_id == p.proposal_id)
            )
            reasons = p.skip_reason_counts
            if reasons is None:
                # This proposal predates the stored tally. The same answer
                # is still sitting in agent_proposal_client, just ungrouped.
                reasons = dict(
                    session.execute(
                        select(AgentProposalClient.skip_reason, func.count())
                        .where(
                            AgentProposalClient.proposal_id == p.proposal_id,
                            AgentProposalClient.included.is_(False),
                        )
                        .group_by(AgentProposalClient.skip_reason)
                    ).all()
                )
            included = fund_total - sum(reasons.values())
            rows.append((p.group_name, fund_total, included, reasons))
    if not rows:
        print(f"run_id={run_id} has no proposal for group {only_group!r}")
        return 1
    _print_table(rows)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Report how many clients the proposal gates leave out, and why."
    )
    parser.add_argument(
        "--as-of",
        type=_parse_date,
        default=None,
        help="Score the live report as if it were this date (YYYY-MM-DD). Defaults to today. "
        "Ignored with --run-id.",
    )
    parser.add_argument(
        "--group", default=None, help="Only report this group. Defaults to every group."
    )
    parser.add_argument(
        "--run-id",
        type=int,
        default=None,
        help="Read back a stored run's proposals instead of computing live.",
    )
    args = parser.parse_args(argv)

    if args.run_id is not None:
        return _run_report(args.run_id, args.group)
    return _live_report(args.as_of or date.today(), args.group)


if __name__ == "__main__":
    raise SystemExit(main())
