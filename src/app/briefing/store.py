"""Read and write the stored narration for one client-fund relationship.

The whole point of storing a narration is to take drafting off the path of a
click that should be instant. The whole risk of storing one is serving text
that describes a client as they were last week. facts_fingerprint settles
that: a stored narration is only ever returned when the facts it was written
from still hash to the same value, so it goes stale the moment the client
changes rather than on a timer.

No audit rows are written here. Reading a stored narration is not a model
crossing, and services/briefing.py already audits the view itself.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence

from sqlalchemy import func, select, tuple_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.db.models.briefing import BriefingNarrative
from app.privacy.fact_block import RISK_FACT_BLOCK_VERSION, RiskFactBlock


def facts_fingerprint(facts: RiskFactBlock) -> str:
    """A stable hash of everything a narration was allowed to draw on.

    The block is dumped with sorted keys so two equal blocks always hash
    alike, and the schema version is folded in so a change to what the
    fields mean retires every stored narration written under the old
    meaning.
    """
    payload = json.dumps(
        {"version": RISK_FACT_BLOCK_VERSION, "facts": facts.to_dict()},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def read_narrative(
    session: Session, client_id: int, unit_fund_id: int, facts: RiskFactBlock
) -> str | None:
    """The stored narration for this client-fund, if it still matches these
    facts. None when there is none, or when the one there is went stale.
    """
    row = session.get(BriefingNarrative, (client_id, unit_fund_id))
    if row is None:
        return None
    if row.facts_hash != facts_fingerprint(facts):
        return None
    return row.narrative_text


def save_narrative(
    session: Session,
    client_id: int,
    unit_fund_id: int,
    facts: RiskFactBlock,
    *,
    text: str,
    model: str,
) -> None:
    """Store the narration for this client-fund, replacing any earlier one.

    Only ever called with text a narration attempt actually accepted. A
    fallback is not stored: it is the deterministic text, which the read
    path can always rebuild for free.
    """
    statement = insert(BriefingNarrative).values(
        client_id=client_id,
        unit_fund_id=unit_fund_id,
        narrative_text=text,
        facts_hash=facts_fingerprint(facts),
        fact_block_version=RISK_FACT_BLOCK_VERSION,
        model=model,
    )
    session.execute(
        statement.on_conflict_do_update(
            index_elements=["client_id", "unit_fund_id"],
            set_={
                "narrative_text": statement.excluded.narrative_text,
                "facts_hash": statement.excluded.facts_hash,
                "fact_block_version": statement.excluded.fact_block_version,
                "model": statement.excluded.model,
                "generated_at": func.now(),
            },
        )
    )


def stale_or_missing_keys(
    session: Session, fingerprints: dict[tuple[int, int], str]
) -> list[tuple[int, int]]:
    """Which of these client-fund keys have no usable narration right now.

    One read for the whole batch, so the nightly warm-up can skip the
    clients whose facts did not move overnight without a query per client.
    """
    if not fingerprints:
        return []
    stored = {
        (row.client_id, row.unit_fund_id): row.facts_hash
        for row in session.execute(
            select(
                BriefingNarrative.client_id,
                BriefingNarrative.unit_fund_id,
                BriefingNarrative.facts_hash,
            ).where(BriefingNarrative.client_id.in_({key[0] for key in fingerprints}))
        ).all()
    }
    return [key for key, fingerprint in fingerprints.items() if stored.get(key) != fingerprint]


def read_narratives(
    session: Session, keys: Sequence[tuple[int, int]]
) -> dict[tuple[int, int], str]:
    """The stored narration for each of these client-funds, in one read.

    Unlike read_narrative this does not check the facts it was written from.
    Its caller is the morning email, which is built minutes after the
    nightly warm-up rewrote every narration on the digest, so there is
    nothing newer to be stale against. Anything reading a narration outside
    that window should use read_narrative and its freshness check.
    """
    keys = list(keys)
    if not keys:
        return {}
    rows = session.execute(
        select(
            BriefingNarrative.client_id,
            BriefingNarrative.unit_fund_id,
            BriefingNarrative.narrative_text,
        ).where(tuple_(BriefingNarrative.client_id, BriefingNarrative.unit_fund_id).in_(keys))
    ).all()
    return {(row.client_id, row.unit_fund_id): row.narrative_text for row in rows}
