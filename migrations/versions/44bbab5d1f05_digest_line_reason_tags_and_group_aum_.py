"""digest line reason tags and group aum total

Revision ID: 44bbab5d1f05
Revises: 64e72b0c8350
Create Date: 2026-08-13 09:20:52.032233

risk_reason_tags is risk_reasons split into short codes (the fired signal
names, sig_ prefix stripped) so a console can render chips instead of
parsing the prose string. group_aum_total is the true aum_at_risk sum
across every eligible client in the line's group, including the ones the
per-group cap left out of digest_line entirely -- group_total already
carries the equivalent eligible-count the same way, duplicated onto every
line in a group.

Both are computed once at digest build time from data the build already
has (digest/build.py), not backfilled here: existing rows get an empty tag
list and a zero total, which is honest for a digest that predates this
column, not a guess at what it would have been.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "44bbab5d1f05"
down_revision: str | Sequence[str] | None = "64e72b0c8350"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "digest_line",
        sa.Column("risk_reason_tags", JSONB(), server_default="[]", nullable=False),
    )
    op.add_column(
        "digest_line",
        sa.Column("group_aum_total", sa.Float(), server_default="0", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("digest_line", "group_aum_total")
    op.drop_column("digest_line", "risk_reason_tags")
