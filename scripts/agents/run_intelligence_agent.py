from __future__ import annotations

import argparse
import asyncio
import sys
import time
from datetime import date
from pathlib import Path

# Make the app package importable when run as a plain script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

from sqlalchemy import select  # noqa: E402

from app.agents.intelligence import run_intelligence_agent  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db.models.agent_insight import AgentInsight, AgentInsightFact  # noqa: E402
from app.db.models.agent_run import AgentToolCall  # noqa: E402
from app.db.models.audit import AuditLog  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.llmops.tracing import get_tracer  # noqa: E402
from app.logging_config import configure_logging  # noqa: E402
from app.privacy.llm_client import (  # noqa: E402
    get_agent_llm_client,
    resolve_agent_model_config,
)


def _parse_date(value: str) -> date:
    return date.fromisoformat(value)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the intelligence agent once and print what it found."
    )
    parser.add_argument(
        "--as-of",
        type=_parse_date,
        default=None,
        help="Read the book as if it were this date (YYYY-MM-DD). Defaults to today.",
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
        "--max-turns",
        type=int,
        default=None,
        help="Override how many turns one group's investigation may take.",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=None,
        help="Override how many groups are investigated at the same time.",
    )
    return parser


async def _run(args, llm_client, tracer):
    session = SessionLocal()
    started = time.perf_counter()
    try:
        run = await run_intelligence_agent(
            session,
            trigger=args.trigger,
            llm_client=llm_client,
            as_of=args.as_of or date.today(),
            max_turns=args.max_turns,
            concurrency=args.concurrency,
            tracer=tracer,
        )
        insights = session.scalars(
            select(AgentInsight)
            .where(AgentInsight.run_id == run.run_id)
            .order_by(AgentInsight.insight_id)
        ).all()
        facts = session.scalars(
            select(AgentInsightFact).where(
                AgentInsightFact.insight_id.in_([row.insight_id for row in insights])
            )
        ).all()
        tool_calls = session.scalars(
            select(AgentToolCall).where(AgentToolCall.run_id == run.run_id)
        ).all()
        report = session.scalars(
            select(AuditLog.detail).where(
                AuditLog.run_id == str(run.run_id), AuditLog.action == "report"
            )
        ).first()
    finally:
        session.close()
    return run, insights, facts, tool_calls, report or {}, time.perf_counter() - started


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

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
    try:
        run, insights, facts, tool_calls, report, seconds = asyncio.run(
            _run(args, llm_client, tracer)
        )
    finally:
        tracer.shutdown()

    print("\n--- result ---")
    print(f"run_id={run.run_id} state={run.state} took={seconds:.1f}s")
    if run.state == "failed":
        print(f"failure_reason: {run.failure_reason}")
        return 1

    print(f"summary: {run.summary}")
    if run.cost_kes is not None:
        print(f"cost: {run.cost_kes:.2f} KES")
    print(
        f"model calls: {report.get('model_calls', 0)} "
        f"input tokens: {report.get('input_tokens', 0)} "
        f"output tokens: {report.get('output_tokens', 0)}"
    )
    print(f"tool calls made: {len(tool_calls)}")
    if report.get("trace_url"):
        print(f"trace: {report['trace_url']}")

    facts_by_insight: dict[int, list[AgentInsightFact]] = {}
    for fact in facts:
        facts_by_insight.setdefault(fact.insight_id, []).append(fact)

    print(f"\nfindings written ({len(insights)}):")
    for insight in insights:
        print(
            f"  insight_id={insight.insight_id} kind={insight.kind!r} "
            f"group={insight.group_name!r} clients={insight.client_count} "
            f"confidence={insight.confidence!r}"
        )
        print(f"    title: {insight.title}")
        print(f"    why now: {insight.why_now}")
        print(f"    suggestion: {insight.suggestion}")
        for fact in facts_by_insight.get(insight.insight_id, ()):
            print(f"    fact: {fact.fact_text} = {fact.fact_value} from {fact.source_table}")

    print(
        "\nNothing was sent and nothing was proposed. These are findings for a "
        "person to read, accept or dismiss."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
