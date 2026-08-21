"""Pre-draft the narrations for the clients a digest actually surfaces.

Drafting takes seconds on a locally hosted model, which is fine overnight
and far too slow under a click. The nightly risk run ends by building the
digest, and the digest is exactly the short list of clients an FA will open
in the morning, so those are the ones worth drafting ahead of time. Every
other client is still narrated on request.

Best effort by design. A client whose narration fails, leaks, or comes back
ungrounded is skipped and left to the on-demand path, and the whole warm-up
failing never fails the risk run that had already completed. Nothing here
reads a name: a narration is drafted from bands alone, exactly as it is on
the request path.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.briefing.narrative import draft_narrative
from app.briefing.render import render_briefing
from app.briefing.store import facts_fingerprint, save_narrative, stale_or_missing_keys
from app.db.models.digest import DigestLine
from app.privacy.llm_client import LLMClient
from app.services.briefing import (
    briefing_boundary_audit_sink,
    gather_briefing_facts,
    to_risk_fact_block,
)

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class NarrativeWarmResult:
    """What one warm-up pass did, logged and returned to the caller."""

    digest_run_id: int
    considered: int
    already_fresh: int
    drafted: int
    skipped: int


def _digest_keys(session: Session, digest_run_id: int, limit: int) -> list[tuple[int, int]]:
    """The client-funds on this digest, best-ranked first, capped at limit.

    Ranked so that a cap smaller than the digest still warms the clients
    nearest the top of each FA's list rather than an arbitrary slice.
    """
    rows = session.execute(
        select(DigestLine.client_id, DigestLine.unit_fund_id)
        .where(DigestLine.digest_run_id == digest_run_id)
        .order_by(DigestLine.rank, DigestLine.client_id)
    ).all()
    seen: list[tuple[int, int]] = []
    for row in rows:
        key = (row.client_id, row.unit_fund_id)
        if key not in seen:
            seen.append(key)
        if len(seen) >= limit:
            break
    return seen


def warm_digest_narratives(
    session: Session,
    digest_run_id: int,
    llm_client: LLMClient,
    *,
    limit: int,
    reference_date: date | None = None,
) -> NarrativeWarmResult:
    """Draft and store a narration for each client on this digest.

    Skips any client whose stored narration already matches today's facts,
    so a second run over an unchanged digest costs nothing. Commits once at
    the end; a client that fails is logged and left out, never raised.
    """
    ref = reference_date if reference_date is not None else date.today()
    keys = _digest_keys(session, digest_run_id, limit)

    fact_blocks = {}
    for client_id, unit_fund_id in keys:
        facts = gather_briefing_facts(session, client_id, unit_fund_id, ref)
        if facts is not None:
            fact_blocks[(client_id, unit_fund_id)] = facts

    fingerprints = {
        key: facts_fingerprint(to_risk_fact_block(facts)) for key, facts in fact_blocks.items()
    }
    to_draft = stale_or_missing_keys(session, fingerprints)

    drafted = 0
    skipped = 0
    for client_id, unit_fund_id in to_draft:
        facts = fact_blocks[(client_id, unit_fund_id)]
        risk_fact_block = to_risk_fact_block(facts)
        try:
            result = draft_narrative(
                risk_fact_block,
                llm_client,
                fallback_text=render_briefing(facts),
                entity_id=str(client_id),
                audit=briefing_boundary_audit_sink(session),
            )
        except Exception:
            # draft_narrative already swallows every failure it expects, so
            # anything reaching here is unexpected. One bad client must not
            # cost the rest of the digest its warm-up.
            logger.exception(
                "narrative_warm.failed", client_id=client_id, unit_fund_id=unit_fund_id
            )
            skipped += 1
            continue
        if result.mode != "narrative":
            skipped += 1
            continue
        save_narrative(
            session,
            client_id,
            unit_fund_id,
            risk_fact_block,
            text=result.text,
            model=getattr(llm_client, "model", "unknown"),
        )
        drafted += 1

    session.commit()

    result_summary = NarrativeWarmResult(
        digest_run_id=digest_run_id,
        considered=len(fact_blocks),
        already_fresh=len(fact_blocks) - len(to_draft),
        drafted=drafted,
        skipped=skipped,
    )
    logger.info("narrative_warm.completed", **result_summary.__dict__)
    return result_summary
