"""digest cap per group on risk config version

Revision ID: 82a27dc9d0e9
Revises: a1a2550348fe
Create Date: 2026-08-12 15:54:32.339805

Adds a rendering knob, not a scoring one: how many lines the morning digest
shows per FA (or fund) group before falling back to an "and N more" line
(the notebook's own cap is 12). Backfilled onto the already-shipped v1 config
row rather than left to a new version, since it changes nothing about how a
score or a route is computed -- the "never mutated after creation" rule on
this table is about the weights/thresholds a shipped score can be explained
against, not about every column on the row.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "82a27dc9d0e9"
down_revision: str | Sequence[str] | None = "a1a2550348fe"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_V1_DIGEST_CAP = 12


def upgrade() -> None:
    op.add_column(
        "risk_config_version",
        sa.Column(
            "digest_cap_per_group", sa.Integer(), server_default=str(_V1_DIGEST_CAP), nullable=False
        ),
    )
    op.alter_column("risk_config_version", "digest_cap_per_group", server_default=None)


def downgrade() -> None:
    op.drop_column("risk_config_version", "digest_cap_per_group")
