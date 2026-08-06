"""Command line entry point to generate a touch for every due enrollment in
one campaign, through the real generation harness.

Leaves every accepted draft pending_review; nothing here sends anything.

Usage:
    uv run python scripts/run_campaign_cycle.py --campaign-id 1
"""

from __future__ import annotations

import argparse
import functools
import sys
from pathlib import Path

# Make the app package importable when run as a plain script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from app.agents.email_agent import build_system_prompt  # noqa: E402
from app.agents.graph import (  # noqa: E402
    build_generation_graph,
    load_client_context,
    new_generation_state,
)
from app.agents.guardrails import DEFAULT_GUARDRAIL_CHECKS  # noqa: E402
from app.campaigns.scheduler import select_due_enrollments  # noqa: E402
from app.campaigns.touch import run_due_enrollments  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.llmops.telemetry import persist_generation_telemetry  # noqa: E402
from app.llmops.tracing import get_tracer  # noqa: E402
from app.llmops.versions import persist_generation_run  # noqa: E402
from app.logging_config import configure_logging  # noqa: E402
from app.privacy.llm_client import get_llm_client  # noqa: E402
from app.services.review import create_outreach_message  # noqa: E402


def _print_node(node: str, update: dict) -> None:
    print(f"    --- {node} ---")
    for key, value in update.items():
        text = repr(value)
        if len(text) > 300:
            text = text[:300] + "... (truncated)"
        print(f"    {key}: {text}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate a touch for every due enrollment in one campaign."
    )
    parser.add_argument("--campaign-id", type=int, required=True)
    parser.add_argument("--product", default="money market")
    parser.add_argument("--limit", type=int, default=50)
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging(settings.log_level)
    print(f"input: campaign_id={args.campaign_id} product={args.product!r} limit={args.limit}")
    tracer = get_tracer(settings)

    session = SessionLocal()
    try:
        due_preview = select_due_enrollments(
            session, campaign_id=args.campaign_id, limit=args.limit
        )
        print(f"{len(due_preview)} enrollment(s) due (freshest contact first):")
        for enrollment in due_preview:
            print(f"  enrollment_id={enrollment.enrollment_id} client_id={enrollment.client_id}")

        graph = build_generation_graph(
            context_loader=functools.partial(load_client_context, session),
            llm_client=get_llm_client(settings),
            guardrail_checks=DEFAULT_GUARDRAIL_CHECKS,
            prompt_builder=build_system_prompt,
            tracer=tracer,
        )

        def generate(gen_session, enrollment, step_no):
            print(
                f"\n=== enrollment {enrollment.enrollment_id} "
                f"(client_id={enrollment.client_id}, step {step_no}) ==="
            )
            state = new_generation_state(client_id=enrollment.client_id, product=args.product)
            final_state = dict(state)
            for step in graph.stream(state, stream_mode="updates"):
                for node, update in step.items():
                    _print_node(node, update)
                    final_state.update(update)

            print(
                f"    status={final_state.get('status')!r} attempts={final_state.get('attempts')} "
                f"failed_guardrail={final_state.get('failed_guardrail')!r} "
                f"reason={final_state.get('reason')!r}"
            )

            run = persist_generation_run(gen_session, final_state, settings)
            persist_generation_telemetry(gen_session, run, final_state, tracer=tracer)
            # Committed here, before the accepted check, so a rejected run's
            # generation_run/llm_request/token_usage rows survive even though
            # no message follows from it -- the LLM calls still happened and
            # cost real tokens, and are worth keeping for observability.
            gen_session.commit()

            if final_state.get("status") != "accepted":
                # No message is created for a rejected draft: nothing to review
                # yet. Re-running later retries this same step from scratch.
                raise RuntimeError(
                    f"client {enrollment.client_id} draft {final_state.get('status')}: "
                    f"{final_state.get('reason')}"
                )

            message = create_outreach_message(gen_session, run, campaign_id=enrollment.campaign_id)
            gen_session.commit()
            print(f"    subject: {final_state.get('subject')!r}")
            print(f"    body: {final_state.get('body')!r}")
            print(f"    created outreach_message {message.message_id} (pending_review)")
            return message

        outcomes = run_due_enrollments(
            session, campaign_id=args.campaign_id, limit=args.limit, generate=generate
        )
        session.commit()
    finally:
        tracer.shutdown()
        session.close()

    generated = [o for o in outcomes if o.generated]
    skipped = [o for o in outcomes if not o.generated]
    print(
        f"\noutput: {len(outcomes)} due enrollment(s): "
        f"{len(generated)} generated, {len(skipped)} skipped"
    )
    for outcome in skipped:
        print(f"  skipped enrollment {outcome.enrollment_id}: {outcome.reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
