"""Read-only mappings for database views.

Views are created and owned by migrations. They live on their own metadata, off
Base.metadata, so create_all and autogenerate never treat them as tables. This
is the single code-side allow-list for the model-facing projection.
"""

from __future__ import annotations

from sqlalchemy import BigInteger, Boolean, Column, Float, Integer, MetaData, Table, Text

view_metadata = MetaData()

# Tiers and buckets only, keyed by client_id for server-side re-attachment.
# No name, code, exact amount, or date. Low sensitivity: this is what the
# original four-column allow-list widened into once the twelve bands existed.
llm_client_context = Table(
    "llm_client_context",
    view_metadata,
    Column("client_id", BigInteger, primary_key=True),
    Column("recency_band", Text),
    Column("value_band", Text),
    Column("cadence_band", Text),
    Column("hold_band", Text),
    Column("purchase_depth", Text),
    Column("trend_band", Text),
    Column("exit_reason", Text),
    Column("fund_type", Text),
    Column("in_wave", Boolean),
    Column("has_depth", Boolean),
    Column("staged_exit", Boolean),
    Column("stale_contact", Boolean),
    Column("newly_dormant", Boolean),
)

# Real figures, still unrounded: rounding is ModelFactBlock's job at
# construction, not this view's. Kept separate from llm_client_context so the
# two sensitivities are independently grantable and independently revocable.
llm_client_numeric_facts = Table(
    "llm_client_numeric_facts",
    view_metadata,
    Column("client_id", BigInteger, primary_key=True),
    Column("years_since_exit", Float),
    Column("typical_contribution_kes", Float),
    Column("largest_contribution_kes", Float),
    Column("invested_every_n_days", Integer),
    Column("days_held_after_last_topup", Integer),
    Column("month_they_left", Text),
)
