from __future__ import annotations

import argparse
import functools
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.agents.insight_members import resolve_insight_members  # noqa: E402
from app.agents.proposal_state import transition_proposal  # noqa: E402
from app.agents.signals import recompute_all_signals  # noqa: E402
from app.agents.situation_action_mapping import action_code_for_situation  # noqa: E402
from app.agents.situations import (  # noqa: E402
    SITUATION_CODES,
    SituationDelta,
    recompute_all_situations,
    resolved_signal_codes,
    situation_delta,
)
from app.agents.watchlist import WatchlistConfigMissing, load_thresholds  # noqa: E402
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


@dataclass(frozen=True)
class PilotSituationResult:
    situation_code: str
    delta: SituationDelta
    resolved_reasons: dict[tuple[int, int], tuple[str, ...]]
    insight: AgentInsight | None
    proposal: AgentProposal | None
    reused_existing_proposal: bool
    drafts: tuple[OutreachMessage, ...]
    stopped_reason: str | None


@dataclass(frozen=True)
class PilotRun:
    """What one pilot run found, proposed, and left waiting."""

    as_of: date
    run_id: str
    situations: list[PilotSituationResult]


def _parse_date(value: str) -> date:
    return date.fromisoformat(value)


def _resolved_reasons(
    session: Session, delta: SituationDelta, run_id: str
) -> dict[tuple[int, int], tuple[str, ...]]:
    return {
        key: resolved_signal_codes(session, key[0], key[1], run_id) for key in delta.newly_inactive
    }


def describe_delta(
    delta: SituationDelta, resolved_reasons: dict[tuple[int, int], tuple[str, ...]]
) -> str:
    """The delta from Todo 4, in plain words."""
    reason_counts: dict[str, int] = {}
    for codes in resolved_reasons.values():
        label = " and ".join(codes) if codes else "unknown reasons"
        reason_counts[label] = reason_counts.get(label, 0) + 1
    reasons = ", ".join(f"{count} by {label}" for label, count in sorted(reason_counts.items()))
    lines = [
        f"{len(delta.newly_active)} client funds are new to this situation since last run.",
        f"{len(delta.newly_inactive)} left it" + (f" ({reasons})." if reasons else "."),
        f"{len(delta.persisting)} are unchanged from last run.",
    ]
    return "\n".join(lines)


def _already_proposed_today(session: Session, group_name: str, as_of: date) -> AgentProposal | None:
    return session.scalar(
        select(AgentProposal)
        .where(
            AgentProposal.group_name == group_name,
            func.date(AgentProposal.created_at) == as_of,
        )
        .order_by(AgentProposal.proposal_id.desc())
        .limit(1)
    )


def _write_finding(session: Session, situation_code: str, as_of: date) -> AgentInsight:
    insight = AgentInsight(
        kind="risk",
        title=f"Clients in {situation_code}",
        group_name=situation_code,
        group_definition=None,
        client_count=0,
        money_total_kes=0.0,
        confidence="high",
        confidence_reason="the group was counted straight off the situation table",
        suggestion="take action based on mapped playbook",
        avoid_saying="anything unrelated to this situation",
        why_now="the situation just went active for these client funds",
        state="accepted",
    )
    session.add(insight)
    session.flush()
    resolved = resolve_insight_members(session, insight, as_of)
    insight.client_count = len(resolved.members)
    insight.money_total_kes = sum(member.balance for member in resolved.members)
    session.commit()
    return insight


def process_situation(
    session: Session,
    situation_code: str,
    as_of: date,
    run_id: str,
    reviewer: str,
    max_clients: int,
    limit: int,
) -> PilotSituationResult:
    delta = situation_delta(session, situation_code, run_id)
    resolved_reasons = _resolved_reasons(session, delta, run_id)

    def _stopped(reason: str, insight: AgentInsight | None = None) -> PilotSituationResult:
        return PilotSituationResult(
            situation_code=situation_code,
            delta=delta,
            resolved_reasons=resolved_reasons,
            insight=insight,
            proposal=None,
            reused_existing_proposal=False,
            drafts=(),
            stopped_reason=reason,
        )

    if not delta.newly_active:
        return _stopped("no newly active clients for this situation")

    action_code = action_code_for_situation(session, situation_code, as_of)
    if not action_code:
        return _stopped(f"no action mapped for {situation_code}")

    existing = _already_proposed_today(session, situation_code, as_of)
    if existing is not None:
        return PilotSituationResult(
            situation_code=situation_code,
            delta=delta,
            resolved_reasons=resolved_reasons,
            insight=None,
            proposal=existing,
            reused_existing_proposal=True,
            drafts=(),
            stopped_reason=None,
        )

    insight = _write_finding(session, situation_code, as_of)
    if insight.client_count > max_clients:
        return _stopped(
            f"the group picks up {insight.client_count} client funds, "
            f"over the {max_clients} this run allows",
            insight,
        )

    tools = make_write_tools(
        as_of=as_of, draft=functools.partial(draft_into_review_queue, limit=limit)
    )
    proposed = tools[WRITE_PROPOSAL](
        session,
        insight_id=insight.insight_id,
        action_code=action_code,
        angle=action_code,
        reason=f"the situation table says these clients are newly active for {situation_code}",
    )
    session.commit()
    if "error" in proposed:
        return _stopped(proposed["error"], insight)

    proposal = session.get(AgentProposal, proposed["proposal_id"])
    transition_proposal(
        session,
        proposal,
        to_status="approved",
        reason="approved by hand for a signal pilot run",
        decided_by=reviewer,
    )
    session.commit()

    started = tools[RUN_PROPOSAL](session, proposal_id=proposal.proposal_id)
    session.commit()
    if "error" in started:
        return _stopped(started["error"], insight)

    drafts = tuple(
        session.scalars(
            select(OutreachMessage).where(
                OutreachMessage.campaign_id == started["campaign_id"],
                OutreachMessage.status == "pending_review",
            )
        ).all()
    )

    return PilotSituationResult(
        situation_code=situation_code,
        delta=delta,
        resolved_reasons=resolved_reasons,
        insight=insight,
        proposal=proposal,
        reused_existing_proposal=False,
        drafts=drafts,
        stopped_reason=None,
    )


def run_pilot(
    session: Session,
    as_of: date,
    *,
    reviewer: str = "dry_run",
    max_clients: int = 1000,
    limit: int = 100,
) -> PilotRun:
    try:
        thresholds = load_thresholds(session, as_of)
    except WatchlistConfigMissing:
        print(f"Warning: no risk config version is in force on {as_of}. Using defaults.")
        from app.agents.watchlist import WatchlistThresholds

        thresholds = WatchlistThresholds(30, 6.0, 50.0, 7)

    run = recompute_all_signals(
        session,
        as_of,
        months_until_empty_threshold=thresholds.months_until_empty,
        small_balance_threshold=thresholds.small_balance,
        awaiting_call_days=thresholds.awaiting_call_days,
    )
    recompute_all_situations(session, as_of, run.run_id)

    situation_results = []
    for situation_code in SITUATION_CODES:
        result = process_situation(
            session, situation_code, as_of, run.run_id, reviewer, max_clients, limit
        )
        situation_results.append(result)

    return PilotRun(
        as_of=as_of,
        run_id=run.run_id,
        situations=situation_results,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run all situations end to end for one day, "
            "with nothing sent. The drafts are left in the review queue."
        )
    )
    parser.add_argument("--as-of", type=_parse_date, default=None)
    parser.add_argument(
        "--reviewer", default="dry_run", help="Who is recorded as having approved the proposal."
    )
    parser.add_argument(
        "--max-clients",
        type=int,
        default=5,
        help="Stop before proposing anything if a situation picks up more than this.",
    )
    parser.add_argument("--limit", type=int, default=3, help="How many to draft for per situation.")
    args = parser.parse_args(argv)

    configure_logging()
    as_of = args.as_of or date.today()

    with SessionLocal() as session:
        result = run_pilot(
            session,
            as_of,
            reviewer=args.reviewer,
            max_clients=args.max_clients,
            limit=args.limit,
        )

        print(f"as of {result.as_of}, signal run {result.run_id}")

        for sit_res in result.situations:
            print(f"\n{'=' * 60}")
            print(f"Situation: {sit_res.situation_code}")
            print(describe_delta(sit_res.delta, sit_res.resolved_reasons))

            if sit_res.reused_existing_proposal:
                print(
                    f"proposal {sit_res.proposal.proposal_id} already exists for "
                    f"{sit_res.situation_code} today, not re-proposing"
                )
                continue

            if sit_res.stopped_reason is not None:
                print(f"stopping: {sit_res.stopped_reason}")
                continue

            if sit_res.insight:
                print(
                    f"finding {sit_res.insight.insight_id}: "
                    f"{sit_res.insight.client_count} client funds"
                )

            if sit_res.proposal:
                print(f"proposal {sit_res.proposal.proposal_id} approved and run")
                print(f"{len(sit_res.drafts)} drafts waiting on a person, nothing sent")
                for draft in sit_res.drafts:
                    print("-" * 40)
                    print("subject:", draft.ai_draft_content["subject"])
                    print(draft.ai_draft_content["body"])

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
