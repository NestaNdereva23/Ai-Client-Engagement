from __future__ import annotations

import argparse
import functools
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

from app.agents.email_agent import build_system_prompt  # noqa: E402
from app.agents.graph import (  # noqa: E402
    build_generation_graph,
    load_client_context,
    new_generation_state,
)
from app.agents.guardrails import DEFAULT_GUARDRAIL_CHECKS  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.llmops.telemetry import persist_generation_telemetry  # noqa: E402
from app.llmops.tracing import get_tracer  # noqa: E402
from app.llmops.versions import persist_generation_run  # noqa: E402
from app.logging_config import configure_logging  # noqa: E402
from app.privacy.llm_client import get_llm_client  # noqa: E402


def _print_step(node: str, update: dict[str, Any]) -> None:
    print(f"\n--- {node} ---")
    for key, value in update.items():
        print(f"{key}: {value!r}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the real harness for one client, streaming every step."
    )
    parser.add_argument("--client-id", type=int, required=True)
    parser.add_argument("--product", default="money market")
    parser.add_argument("--max-attempts", type=int, default=2)
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging(settings.log_level)
    print(f"provider={settings.llm_provider} model={settings.llm_model}")

    tracer = get_tracer(settings)
    print(f"langfuse={'enabled' if settings.langfuse_enabled else 'disabled'}")

    session = SessionLocal()
    trace_url = None
    try:
        graph = build_generation_graph(
            context_loader=functools.partial(load_client_context, session),
            llm_client=get_llm_client(settings),
            guardrail_checks=DEFAULT_GUARDRAIL_CHECKS,
            prompt_builder=build_system_prompt,
            max_attempts=args.max_attempts,
            tracer=tracer,
        )

        state = new_generation_state(client_id=args.client_id, product=args.product)
        final_state: dict[str, Any] = dict(state)

        for step in graph.stream(state, stream_mode="updates"):
            for node, update in step.items():
                _print_step(node, update)
                final_state.update(update)

        run = persist_generation_run(session, final_state, settings)
        persist_generation_telemetry(session, run, final_state, tracer=tracer)
        session.commit()
        trace_url = tracer.get_trace_url(final_state["trace_id"])
    finally:
        tracer.flush()
        tracer.shutdown()
        session.close()

    print("\n=== final result ===")
    print(f"status:           {final_state.get('status')}")
    print(f"attempts:         {final_state.get('attempts')}")
    print(f"failed_guardrail: {final_state.get('failed_guardrail')}")
    print(f"reason:           {final_state.get('reason')}")
    print(f"run_id:           {final_state.get('run_id')}")
    if trace_url:
        print(f"trace:            {trace_url}")
    return 0 if final_state.get("status") == "accepted" else 1


if __name__ == "__main__":
    raise SystemExit(main())
