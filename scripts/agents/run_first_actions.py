from __future__ import annotations

import argparse
import functools
import json
import sys
from datetime import date
from pathlib import Path

# Make the app package importable when run as a plain script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

from sqlalchemy import select  # noqa: E402

from app.agents.insight_members import resolve_insight_members  # noqa: E402
from app.agents.proposal_state import transition_proposal  # noqa: E402
from app.agents.write_tools import (  # noqa: E402
    RUN_PROPOSAL,
    WRITE_PROPOSAL,
    draft_into_review_queue,
    make_write_tools,
)
from app.db.models.agent_insight import AgentInsight  # noqa: E402
from app.db.models.agent_proposal import AgentProposal  # noqa: E402
from app.db.models.outreach import OutreachMessage  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.logging_config import configure_logging  # noqa: E402

ACTIONS = ("fee_warning", "welcome_and_top_up")

FEE_WARNING_FILTER = [
    {"field": "months_until_empty", "op": "lt", "value": 6},
    {"field": "balance", "op": "gt", "value": 0},
]


def _parse_date(value: str) -> date:
    return date.fromisoformat(value)


def _write_finding(session, *, action: str, conditions: list[dict], as_of: date) -> AgentInsight:
    """A finding of the shape the agent writes, accepted the way a person does."""
    insight = AgentInsight(
        kind="risk",
        title=f"A trial run of {action}",
        group_name=f"trial_{action}",
        group_definition={"conditions": conditions},
        client_count=0,
        money_total_kes=0.0,
        confidence="high",
        confidence_reason="the group was counted straight off the filter stored with it",
        suggestion="try the first live response against real clients, with sending off",
        avoid_saying="that their account is nearly empty",
        why_now="the response is being tried for the first time",
        state="accepted",
    )
    session.add(insight)
    session.flush()
    resolved = resolve_insight_members(session, insight, as_of)
    insight.client_count = len(resolved.members)
    insight.money_total_kes = sum(member.balance for member in resolved.members)
    session.commit()
    return insight


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Try one live response against real clients from end to end, with "
            "nothing sent. The drafts are left in the review queue."
        )
    )
    parser.add_argument("--action", choices=ACTIONS, default="fee_warning")
    parser.add_argument("--as-of", type=_parse_date, default=None)
    parser.add_argument(
        "--reviewer",
        default="dry_run",
        help="Who is recorded as having approved the proposal.",
    )
    parser.add_argument(
        "--conditions",
        default=None,
        help="The filter that picks the group, as JSON. Narrow it to keep the run small.",
    )
    parser.add_argument(
        "--max-clients",
        type=int,
        default=5,
        help="Stop before proposing anything if the filter picks up more than this.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=3,
        help="How many of the group to draft for.",
    )
    args = parser.parse_args(argv)

    configure_logging()
    as_of = args.as_of or date.today()
    conditions = json.loads(args.conditions) if args.conditions else FEE_WARNING_FILTER

    with SessionLocal() as session:
        insight = _write_finding(session, action=args.action, conditions=conditions, as_of=as_of)
        print(f"finding {insight.insight_id}: {insight.client_count} client funds")
        if insight.client_count > args.max_clients:
            print(
                f"stopping: the filter picks up {insight.client_count} client funds, "
                f"over the {args.max_clients} this run allows. Narrow --conditions."
            )
            return 1

        tools = make_write_tools(
            as_of=as_of,
            draft=functools.partial(draft_into_review_queue, limit=args.limit),
        )
        proposed = tools[WRITE_PROPOSAL](
            session,
            insight_id=insight.insight_id,
            action_code=args.action,
            angle=args.action,
            reason="trying the first live response against real clients",
        )
        session.commit()
        print("write_proposal:", json.dumps(proposed, default=str))
        if "error" in proposed:
            return 1

        proposal = session.get(AgentProposal, proposed["proposal_id"])
        transition_proposal(
            session,
            proposal,
            to_status="approved",
            reason="approved by hand for a trial run",
            decided_by=args.reviewer,
        )
        session.commit()

        started = tools[RUN_PROPOSAL](session, proposal_id=proposal.proposal_id)
        session.commit()
        print("run_proposal:", json.dumps(started, default=str))
        if "error" in started:
            return 1

        drafts = session.scalars(
            select(OutreachMessage).where(
                OutreachMessage.campaign_id == started["campaign_id"],
                OutreachMessage.status == "pending_review",
            )
        ).all()

        print(f"\n{len(drafts)} drafts waiting on a person, nothing sent")
        for draft in drafts:
            print("-" * 60)
            print("subject:", draft.ai_draft_content["subject"])
            print(draft.ai_draft_content["body"])

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
