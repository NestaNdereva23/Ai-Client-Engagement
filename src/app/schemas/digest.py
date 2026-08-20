"""Request and response shapes for the digest endpoint."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class DigestLineOut(BaseModel):
    """One client-fund's line, already ranked within its group."""

    model_config = ConfigDict(from_attributes=True)

    client_id: int
    unit_fund_id: int
    rank: int
    # Which cap_per_group-sized slice of the group this line falls in. 0 is
    # always visible; a later batch only appears once the one before it has
    # been fully worked -- see GET /digest/{fa_or_fund_key}.
    batch: int
    risk_score: int
    risk_band: str
    risk_reasons: str
    # risk_reasons split into short codes (e.g. "broken_pattern", "dormant"),
    # the same fired signals, for a console to render as chips.
    risk_reason_tags: list[str]
    fund_at_risk: float
    score_delta: int | None
    route: str
    in_call_queue: bool
    complaint_caveat: bool
    # True when an FA manager already acted on this client-fund and their
    # risk band hasn't risen since -- it ranks below every untouched or
    # escalated line in its group, rather than being hidden outright.
    deprioritized: bool
    # Whether AM11's on-demand briefing has enough data to render for this
    # client-fund right now -- a cheap existence check, not the briefing
    # text itself. See GET /briefing/{client_id}/{unit_fund_id}.
    briefing_available: bool


class DigestGroupOut(BaseModel):
    """Today's unlocked digest lines for one FA (or fund) group.

    `lines` grows through the day: batch 0 is always there, and each later
    batch is added once every line in the one before it has been acted on.
    overflow_count is how many eligible clients are still behind an
    unfinished batch right now -- it shrinks as batches clear, it does not
    mean those clients are hidden for good.
    """

    model_config = ConfigDict(from_attributes=True)

    digest_run_id: int
    risk_run_id: str
    generated_at: datetime
    group_key: str
    total_eligible: int
    overflow_count: int
    # The true fund_at_risk sum across every eligible client today, including
    # the overflow_count ones not yet unlocked into `lines`.
    total_fund_at_risk: float
    lines: list[DigestLineOut]
