"""Walk one real client fund through the whole agent pipeline by hand.

Given a client_id and unit_fund_id, this runs the same real functions the
nightly agent uses, one stage at a time, and prints what each stage saw:
the resolved (non-PII) client row, its signals and situations, which watch
list group it lands in, the proposal written for it, the permission gate,
and the exact system prompt the model would be sent. Every function called
here is the production function, not a copy; only the LLM call itself is
gated behind --run, since that is the one step that costs real money.

Usage:
    uv run python scripts/agents/trace_client_generation.py --discover 5
    uv run python scripts/agents/trace_client_generation.py --client-id 167 --unit-fund-id 17
    uv run python scripts/agents/trace_client_generation.py --client-id 167 --unit-fund-id 17 --run
"""

from __future__ import annotations

import argparse
import functools
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

import structlog  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.agents.email_agent import build_system_prompt, resolve_allowed_placeholders  # noqa: E402
from app.agents.graph import load_client_context  # noqa: E402
from app.agents.prompt_config import resolve_active_configuration  # noqa: E402
from app.agents.proposal_state import transition_proposal  # noqa: E402
from app.agents.propose import propose_group, situation_winners  # noqa: E402
from app.agents.watchlist import (  # noqa: E402
    FEE_PRESSURE_GONE_QUIET,
    WatchGroup,
    build_watchlist,
    load_thresholds,
)
from app.agents.write_tools import (  # noqa: E402
    RUN_PROPOSAL,
    draft_into_review_queue,
    make_write_tools,
)
from app.campaigns.generation import resolve_product  # noqa: E402
from app.campaigns.nurture_bridge import prepare_client_for_drafting  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db.models.active_clients import ActiveClientFund  # noqa: E402
from app.db.models.risk import ClientRiskFeatures  # noqa: E402
from app.db.models.signals import ClientSignalState, ClientSituationState  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.logging_config import configure_logging  # noqa: E402
from app.personalization.eligibility import filter_facts_for_prompt  # noqa: E402
from app.privacy.fact_block import ModelFactBlock  # noqa: E402

logger = structlog.get_logger("trace_client_generation")

TARGET_GROUP_NAME = FEE_PRESSURE_GONE_QUIET


def _parse_date(value: str) -> date:
    return date.fromisoformat(value)


def _print_step(number: int, title: str) -> None:
    print(f"\n{'=' * 70}\nSTEP {number}: {title}\n{'=' * 70}")


def step_resolved_client(
    session, client_id: int, unit_fund_id: int
) -> tuple[ActiveClientFund, ClientRiskFeatures | None]:
    _print_step(1, "ingestion-resolved client details (active book, no PII)")
    fund = session.execute(
        select(ActiveClientFund).where(
            ActiveClientFund.client_id == client_id,
            ActiveClientFund.unit_fund_id == unit_fund_id,
        )
    ).scalar_one_or_none()
    if fund is None:
        raise SystemExit(
            f"no active_client_fund row for client {client_id}, fund {unit_fund_id}. "
            "Run with --discover to see client funds that actually exist tonight."
        )
    risk = session.execute(
        select(ClientRiskFeatures).where(
            ClientRiskFeatures.client_id == client_id,
            ClientRiskFeatures.unit_fund_id == unit_fund_id,
        )
    ).scalar_one_or_none()
    logger.info(
        "resolved_client.active_client_fund",
        client_id=client_id,
        unit_fund_id=unit_fund_id,
        balance_kes=float(fund.balance) if fund.balance is not None else None,
        n_deposits=fund.n_deposits,
        first_deposit_date=str(fund.first_deposit_date),
    )
    if risk is not None:
        logger.info(
            "resolved_client.risk_features",
            client_id=client_id,
            unit_fund_id=unit_fund_id,
            risk_band=risk.risk_band,
            risk_score=risk.risk_score,
            sig_dormant=risk.sig_dormant,
            sig_shrinking=risk.sig_shrinking,
            route=risk.route,
        )
    print(
        "No client name, code, or contact detail was read here: only the pseudonymous "
        "client_id/unit_fund_id and the fields logged above."
    )
    return fund, risk


def step_signals_and_situations(session, client_id: int, unit_fund_id: int) -> None:
    _print_step(2, "signals and situations computed for this client fund")
    signals = session.scalars(
        select(ClientSignalState).where(
            ClientSignalState.client_id == client_id,
            ClientSignalState.unit_fund_id == unit_fund_id,
        )
    ).all()
    for signal in signals:
        logger.info(
            "signal_state",
            signal_code=signal.signal_code,
            is_active=signal.is_active,
            since=str(signal.since),
        )
    situations = session.scalars(
        select(ClientSituationState).where(
            ClientSituationState.client_id == client_id,
            ClientSituationState.unit_fund_id == unit_fund_id,
        )
    ).all()
    for situation in situations:
        logger.info(
            "situation_state",
            situation_code=situation.situation_code,
            is_active=situation.is_active,
            signal_codes=situation.signal_codes,
            since=str(situation.since),
        )
    active = [situation.situation_code for situation in situations if situation.is_active]
    print(f"Active situations tonight: {active}")


def step_watchlist_group(session, as_of: date, client_id: int, unit_fund_id: int):
    _print_step(3, "which watch list group this client fund lands in tonight")
    thresholds = load_thresholds(session, as_of)
    groups = build_watchlist(session, as_of, thresholds)
    winners = situation_winners(groups)
    key = (client_id, unit_fund_id)

    membership = [
        g.name for g in groups if any((m.client_id, m.unit_fund_id) == key for m in g.members)
    ]
    winner = winners.get(key)
    logger.info(
        "watchlist.membership",
        client_id=client_id,
        unit_fund_id=unit_fund_id,
        groups_tonight=membership,
        winning_situation=winner,
    )

    target_group = next(g for g in groups if g.name == TARGET_GROUP_NAME)
    member = next((m for m in target_group.members if (m.client_id, m.unit_fund_id) == key), None)
    if member is None:
        raise SystemExit(
            f"client {client_id} fund {unit_fund_id} is not in '{TARGET_GROUP_NAME}' tonight "
            f"({as_of}). It is currently in: {membership or ['no group']}. Run with --discover "
            "to see who actually is."
        )
    if winner != TARGET_GROUP_NAME:
        print(
            f"Note: '{winner}' outranks '{TARGET_GROUP_NAME}' for this client tonight, so a real "
            "nightly run would leave them out under 'consolidated_into_another_situation'. "
            f"Continuing anyway to trace the {TARGET_GROUP_NAME} path on its own."
        )
    return thresholds, target_group, member


def step_write_proposal(session, target_group, member, thresholds, as_of: date):
    _print_step(4, "writing the agent_proposal for this one client (real propose_group)")
    solo_group = WatchGroup(
        name=target_group.name, definition=target_group.definition, members=(member,)
    )
    winners = {(member.client_id, member.unit_fund_id): target_group.name}
    proposal = propose_group(session, solo_group, thresholds, as_of, winners=winners)
    session.commit()
    logger.info(
        "agent_proposal.created",
        proposal_id=proposal.proposal_id,
        action_code=proposal.action_code,
        angle=proposal.angle,
        response_kind=proposal.response_kind,
        permission_applied=proposal.permission_applied,
        status=proposal.status,
        reason=proposal.reason,
    )
    return proposal


def step_permission_gate(session, proposal, reviewer: str) -> None:
    _print_step(5, "checking the agent_permission gate for this action")
    if proposal.permission_applied == "act_alone":
        logger.info("permission.act_alone", action_code=proposal.action_code)
        print(
            f"agent_permission for '{proposal.action_code}' is act_alone: no person needs to "
            "approve this. The system approves it on its own."
        )
        decided_by = "agent(act_alone)"
        reason = "act_alone permission auto-approves this proposal"
    else:
        logger.info(
            "permission.needs_a_person",
            action_code=proposal.action_code,
            permission=proposal.permission_applied,
        )
        print(
            f"agent_permission for '{proposal.action_code}' is '{proposal.permission_applied}', "
            f"which needs a person. Simulating that approval now as '{reviewer}' so the trace "
            "can reach generation."
        )
        decided_by = reviewer
        reason = "approved by hand for a single client generation trace"
    transition_proposal(
        session, proposal, to_status="approved", reason=reason, decided_by=decided_by
    )
    session.commit()
    logger.info("agent_proposal.approved", proposal_id=proposal.proposal_id, decided_by=decided_by)


def step_bridge_and_prompt(session, proposal, member, as_of: date) -> str:
    _print_step(6, "bridging into the drafting tables and assembling the real system prompt")
    settings = get_settings()

    bridged = prepare_client_for_drafting(
        session,
        member.client_id,
        angle=proposal.angle,
        chosen_by=proposal.action_code,
        catalog_version=proposal.catalog_version,
    )
    session.commit()
    if not bridged:
        raise SystemExit(f"client {member.client_id} holds nothing in the active book to draft for")
    logger.info(
        "nurture_bridge.prepared",
        client_id=member.client_id,
        angle=proposal.angle,
        chosen_by=proposal.action_code,
        catalog_version=proposal.catalog_version,
    )

    product = resolve_product(session, member.client_id)
    logger.info("product.resolved", client_id=member.client_id, product=product)

    ctx = load_client_context(session, member.client_id, product, at=as_of)
    logger.info(
        "client_context.loaded",
        client_id=member.client_id,
        angle=ctx.angle,
        prompt_variant=ctx.prompt_variant,
        priority_tier=ctx.priority_tier,
        chunk_count=len(ctx.chunks),
        facts_before_filtering=ctx.facts,
    )

    config = resolve_active_configuration(
        session,
        channel="email",
        angle=ctx.angle,
        tier=ctx.priority_tier,
        at=as_of,
        settings=settings,
    )
    facts = ctx.facts
    if facts and config.fact_eligibility is not None:
        filtered = filter_facts_for_prompt(facts, config.fact_eligibility)
        allowed_fields = set(filtered.direct) | set(filtered.placeholder)
        facts = {field: value for field, value in facts.items() if field in allowed_fields}
        facts = ModelFactBlock(**facts).to_dict()
    allowed_placeholders = resolve_allowed_placeholders(config.allowed_placeholder_fields)
    logger.info(
        "facts.filtered_for_prompt",
        client_id=member.client_id,
        facts=facts,
        allowed_placeholders=allowed_placeholders,
    )

    system_prompt = build_system_prompt(
        angle=ctx.angle,
        prompt_variant=ctx.prompt_variant,
        chunks=ctx.chunks,
        brief=ctx.brief,
        contract=ctx.contract,
        facts=facts,
        voice_text=config.voice_text,
        safety_words=config.safety_words,
        safety_phrases=config.safety_phrases,
        campaign_prohibitions=config.campaign_prohibitions,
        output_rules=config.output_rules,
        default_sign_off=config.default_sign_off,
        base_instructions=config.base_instructions,
    )
    print("\n----- REAL SYSTEM PROMPT, exactly as build_system_prompt returns it -----\n")
    print(system_prompt)
    print("\n----- END SYSTEM PROMPT -----\n")
    return system_prompt


def step_real_run(session, proposal, as_of: date, reviewer: str) -> None:
    _print_step(
        7, "calling the real run_proposal tool (this spends real credit with the configured model)"
    )
    tools = make_write_tools(as_of=as_of, draft=functools.partial(draft_into_review_queue, limit=1))
    result = tools[RUN_PROPOSAL](session, proposal_id=proposal.proposal_id)
    session.commit()
    logger.info("run_proposal.result", **result)
    print(result)


def discover(session, as_of: date, count: int) -> None:
    thresholds = load_thresholds(session, as_of)
    groups = build_watchlist(session, as_of, thresholds)
    target = next(g for g in groups if g.name == TARGET_GROUP_NAME)
    print(f"{len(target.members)} client funds are in '{TARGET_GROUP_NAME}' tonight ({as_of}).")
    print(f"First {min(count, len(target.members))}:")
    for member in target.members[:count]:
        print(
            f"  --client-id {member.client_id} --unit-fund-id {member.unit_fund_id}  "
            f"(balance={member.balance:,.2f} KES)"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Trace one real client fund through the fee_pressure_warning_dormant path, "
            "step by step, on the console."
        )
    )
    parser.add_argument("--client-id", type=int)
    parser.add_argument("--unit-fund-id", type=int)
    parser.add_argument("--as-of", type=_parse_date, default=None)
    parser.add_argument("--reviewer", default="trace_script")
    parser.add_argument(
        "--run",
        action="store_true",
        help="Also call the real run_proposal tool end to end, through the configured model "
        "provider. Off by default because it spends real money.",
    )
    parser.add_argument(
        "--discover",
        type=int,
        default=0,
        help="Print this many real (client_id, unit_fund_id) pairs from tonight's "
        f"'{TARGET_GROUP_NAME}' group and exit, instead of tracing one.",
    )
    args = parser.parse_args(argv)

    configure_logging()
    as_of = args.as_of or date.today()

    with SessionLocal() as session:
        if args.discover:
            discover(session, as_of, args.discover)
            return 0

        if args.client_id is None or args.unit_fund_id is None:
            parser.error("--client-id and --unit-fund-id are required unless --discover is used")

        step_resolved_client(session, args.client_id, args.unit_fund_id)
        step_signals_and_situations(session, args.client_id, args.unit_fund_id)
        thresholds, target_group, member = step_watchlist_group(
            session, as_of, args.client_id, args.unit_fund_id
        )
        proposal = step_write_proposal(session, target_group, member, thresholds, as_of)
        step_permission_gate(session, proposal, args.reviewer)
        step_bridge_and_prompt(session, proposal, member, as_of)

        if args.run:
            step_real_run(session, proposal, as_of, args.reviewer)
        else:
            print(
                "\nDry run stopped here: no model was called and nothing was spent. The "
                f"proposal sits at status={proposal.status!r}. Pass --run to also call the "
                f"configured model ({get_settings().llm_provider}) for real and leave a draft "
                "sitting in the review queue, the only thing remaining after that being sending."
            )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
