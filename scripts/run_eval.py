from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from sqlalchemy import exists, select  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db.models.llmops import Evaluation, GenerationRun, ToolCall  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.llmops.judge import judge_draft  # noqa: E402
from app.llmops.versions import persist_evaluation  # noqa: E402
from app.logging_config import configure_logging  # noqa: E402
from app.privacy.llm_client import get_judge_llm_client, resolve_judge_model_config  # noqa: E402


@dataclass(frozen=True)
class StoredChunk:
    chunk_id: int
    text: str


def _unjudged_runs(session, limit: int | None):
    query = (
        select(GenerationRun)
        .where(GenerationRun.ai_draft_content.is_not(None))
        .where(~exists().where(Evaluation.run_id == GenerationRun.run_id))
        .order_by(GenerationRun.created_at)
    )
    if limit:
        query = query.limit(limit)
    return session.scalars(query).all()


def _retrieved_chunks(session, run_id: str) -> list[StoredChunk]:
    tool_call = session.scalar(
        select(ToolCall)
        .where(ToolCall.run_id == run_id)
        .where(ToolCall.tool_name == "rag_retrieval")
    )
    if tool_call is None:
        return []
    chunks = tool_call.tool_output.get("chunks", [])
    return [StoredChunk(chunk_id=c["chunk_id"], text=c["text"]) for c in chunks]


def _draft_text(ai_draft_content: dict) -> str:
    return f"Subject: {ai_draft_content['subject']}\n\n{ai_draft_content['body']}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Judge every unjudged draft offline: tone, compliance, grounding, "
        "personalization, written to evaluations."
    )
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging(settings.log_level)
    llm_client = get_judge_llm_client(settings)
    provider, model, _, _ = resolve_judge_model_config(settings)
    print(f"judge provider={provider} model={model}")

    session = SessionLocal()
    try:
        runs = _unjudged_runs(session, args.limit)
        print(f"{len(runs)} run(s) to judge")

        for run in runs:
            chunks = _retrieved_chunks(session, run.run_id)
            draft = _draft_text(run.ai_draft_content)
            scores = judge_draft(llm_client, draft=draft, chunks=chunks)
            persist_evaluation(session, run, scores, settings)
            session.commit()
            print(
                f"{run.run_id}: tone={scores.tone} compliance={scores.compliance} "
                f"grounding={scores.grounding} personalization={scores.personalization}"
            )
    finally:
        session.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
