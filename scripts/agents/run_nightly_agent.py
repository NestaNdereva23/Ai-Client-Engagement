from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

# Make the app package importable when run as a plain script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

from sqlalchemy import select  # noqa: E402

from app.agents.agent_loop import run_nightly_agent  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db.models.agent_proposal import AgentProposal  # noqa: E402
from app.db.models.agent_run import AgentToolCall  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.llmops.tracing import get_tracer  # noqa: E402
from app.logging_config import configure_logging  # noqa: E402
from app.privacy.llm_client import get_agent_llm_client, resolve_agent_model_config  # noqa: E402


def _parse_date(value: str) -> date:
    return date.fromisoformat(value)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run one pass of the nightly agent loop and print what it did."
    )
    parser.add_argument(
        "--as-of",
        type=_parse_date,
        default=None,
        help="Score the run as if it were this date (YYYY-MM-DD). Defaults to today.",
    )
    parser.add_argument(
        "--trigger",
        choices=("manual", "nightly", "chat"),
        default="manual",
        help="How the run is recorded as having started. Use 'manual' for a test run.",
    )
    parser.add_argument(
        "--provider",
        default=None,
        help="Override the configured agent model provider for this run only "
        "(anthropic, ollama, or llamacpp).",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Override the configured agent model name for this run only.",
    )
    parser.add_argument(
        "--cooldown-days",
        type=int,
        default=None,
        help="Override AGENT_CONTACT_COOLDOWN_DAYS for this run only.",
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging(settings.log_level)

    overrides = {}
    if args.provider is not None:
        overrides["agent_llm_provider"] = args.provider
    if args.model is not None:
        overrides["agent_llm_model"] = args.model
    if overrides:
        settings = settings.model_copy(update=overrides)

    provider, model, _temperature, _max_tokens = resolve_agent_model_config(settings)
    as_of = args.as_of or date.today()

    print(f"input: as_of={as_of.isoformat()} trigger={args.trigger!r}")
    print(f"model: provider={provider!r} model={model!r}")
    print("--- live trace (one line per step or tool call) ---")

    llm_client = get_agent_llm_client(settings)
    tracer = get_tracer(settings)

    session = SessionLocal()
    try:
        run = run_nightly_agent(
            session,
            trigger=args.trigger,
            llm_client=llm_client,
            as_of=as_of,
            cooldown_days=args.cooldown_days,
            tracer=tracer,
        )

        tool_calls = session.scalars(
            select(AgentToolCall)
            .where(AgentToolCall.run_id == run.run_id)
            .order_by(AgentToolCall.ordinal)
        ).all()
        proposals = session.scalars(
            select(AgentProposal).where(AgentProposal.run_id == run.run_id)
        ).all()
    finally:
        tracer.shutdown()
        session.close()

    print("\n--- result ---")
    print(f"run_id={run.run_id} state={run.state}")
    if run.state == "failed":
        print(f"failure_reason: {run.failure_reason}")
        return 1

    print(f"plan: {run.plan_text}")
    print(f"summary: {run.summary}")
    if run.cost_kes is not None:
        print(f"cost: {run.cost_kes:.2f} KES")
    print(f"tool calls made: {len(tool_calls)}")

    print(f"\nproposals written ({len(proposals)}):")
    for proposal in proposals:
        print(
            f"  proposal_id={proposal.proposal_id} group={proposal.group_name!r} "
            f"action={proposal.action_code!r} clients={proposal.client_count} "
            f"status={proposal.status!r}"
        )
        print(f"    reason: {proposal.reason}")

    print(
        "\nNothing was sent. Review these proposals the same way the review "
        "queue would, then approve or reject before anything reaches a client."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
