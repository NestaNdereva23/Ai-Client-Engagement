"""Read-only mappings for database views.

Views are created and owned by migrations. They live on their own metadata, off
Base.metadata, so create_all and autogenerate never treat them as tables. This
is the single code-side allow-list for the model-facing projection.
"""

from __future__ import annotations

from sqlalchemy import BigInteger, Column, MetaData, Table, Text

view_metadata = MetaData()

# The only fields the model facing path may read: tiers and buckets, keyed by
# client_id for server-side re-attachment. No name, code, exact amount, or date.
llm_client_context = Table(
    "llm_client_context",
    view_metadata,
    Column("client_id", BigInteger, primary_key=True),
    Column("archetype", Text),
    Column("recency_bucket", Text),
    Column("value_tier_label", Text),
    Column("rhythm_band", Text),
)
