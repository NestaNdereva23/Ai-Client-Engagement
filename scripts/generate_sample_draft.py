# Command line entry point to generate one sample draft through the real harness.

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from app.agents.email_channel import EmailAgent  # noqa: E402
from app.agents.graph import ClientContext  # noqa: E402
from app.agents.orchestrator import Orchestrator  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.llmops.tracing import get_tracer  # noqa: E402
from app.privacy.llm_client import get_llm_client  # noqa: E402


@dataclass(frozen=True)
class FakeChunk:
    chunk_id: int
    text: str


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate one sample draft end to end.")
    parser.add_argument("--client-id", type=int, default=1001)
    parser.add_argument("--product", default="money market")
    parser.add_argument("--archetype", default="Frequent (5+, censored)")
    parser.add_argument("--recency-bucket", default="Exited 1 to 2y")
    parser.add_argument("--value-tier", default="High")
    parser.add_argument("--rhythm-band", default="Regular")
    parser.add_argument("--angle", default="winback_habit")
    parser.add_argument("--prompt-variant", default="habit_premium")
    parser.add_argument(
        "--fact",
        action="append",
        default=[],
        dest="facts",
        help="A fact the draft may cite (repeatable). Default: one sample fund-yield fact.",
    )
    parser.add_argument("--max-attempts", type=int, default=2)
    args = parser.parse_args(argv)

    facts = args.facts or ["the fund yielded 11.35% this week"]
    chunks = [FakeChunk(chunk_id=i, text=text) for i, text in enumerate(facts, start=1)]

    raw_context = {
        "client_id": args.client_id,
        "archetype": args.archetype,
        "recency_bucket": args.recency_bucket,
        "value_tier_label": args.value_tier,
        "rhythm_band": args.rhythm_band,
    }

    def context_loader(client_id: int, product: str) -> ClientContext:
        return ClientContext(
            raw_context=raw_context,
            angle=args.angle,
            prompt_variant=args.prompt_variant,
            chunks=chunks,
        )

    settings = get_settings()
    llm_client = get_llm_client(settings)
    print(f"provider={settings.llm_provider} model={settings.llm_model}")

    tracer = get_tracer(settings)
    print(f"langfuse={'enabled' if settings.langfuse_enabled else 'disabled'}\n")

    agent = EmailAgent(
        context_loader=context_loader,
        llm_client=llm_client,
        max_attempts=args.max_attempts,
        tracer=tracer,
    )
    orchestrator = Orchestrator()
    orchestrator.register(agent)

    result = orchestrator.generate("email", client_id=args.client_id, product=args.product)

    print(f"status:           {result['status']}")
    print(f"attempts:         {result['attempts']}")
    print(f"failed_guardrail: {result.get('failed_guardrail')}")
    print(f"reason:           {result.get('reason')}")
    print(f"\nraw draft:\n{result.get('draft')}")
    if result.get("subject") is not None:
        print(f"\nsubject: {result['subject']}")
        print(f"body:    {result['body']}")
    trace_url = tracer.get_trace_url(result["trace_id"])
    if trace_url:
        print(f"\ntrace: {trace_url}")
    tracer.shutdown()

    return 0 if result["status"] == "accepted" else 1


if __name__ == "__main__":
    raise SystemExit(main())
