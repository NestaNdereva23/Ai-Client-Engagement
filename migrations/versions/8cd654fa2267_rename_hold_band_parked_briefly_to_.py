"""rename hold_band 'Parked briefly' to 'Under 2m'

Revision ID: 8cd654fa2267
Revises: 11c3e0f1020d
Create Date: 2026-08-11 22:45:00.000000

"Parked briefly" reads oddly next to "Under 6m" in the same family, and
everybody in this book deposited funds, so "deposited" is not the
distinguishing word. "Under 2m" reads correctly next to "Under 6m" and needs
no explanation. This is a data migration, not a schema change: the column
already exists, only the values in it move.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "8cd654fa2267"
down_revision: str | Sequence[str] | None = "11c3e0f1020d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

client_features = sa.table("client_features", sa.column("hold_band", sa.Text))

_OLD = "Parked briefly"
_NEW = "Under 2m"


def upgrade() -> None:
    op.execute(
        client_features.update().where(client_features.c.hold_band == _OLD).values(hold_band=_NEW)
    )


def downgrade() -> None:
    op.execute(
        client_features.update().where(client_features.c.hold_band == _NEW).values(hold_band=_OLD)
    )
