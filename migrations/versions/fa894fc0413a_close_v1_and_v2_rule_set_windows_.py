"""close v1 and v2 rule set windows, activate v3

Revision ID: fa894fc0413a
Revises: a9d4e7c2f6b8
Create Date: 2026-08-04 22:17:51.097850

The twelve-angle v3 rule set was seeded with a provisional valid_from far in
the future (2026-12-01), before the business had actually committed to a
cutover date. This closes that gap: v1's window ends where v2's actually
took over (2026-08-01), v2's window ends today, and v3 opens today, so the
three windows are contiguous and never overlap. Only the validity windows
change; no rule's match conditions or outputs are touched, and neither
already-shipped version is otherwise mutated.
"""

from collections.abc import Sequence
from datetime import date

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "fa894fc0413a"
down_revision: str | Sequence[str] | None = "a9d4e7c2f6b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_V1_VALID_FROM = date(2026, 1, 1)
_V1_VALID_TO = date(2026, 8, 1)
_V2_VALID_FROM = date(2026, 8, 1)
_V2_VALID_TO = date(2026, 8, 4)
_V3_VALID_FROM_NEW = date(2026, 8, 4)
_V3_VALID_FROM_OLD = date(2026, 12, 1)

business_rules = sa.table(
    "business_rules",
    sa.column("version", sa.Integer),
    sa.column("valid_from", sa.Date),
    sa.column("valid_to", sa.Date),
)


def upgrade() -> None:
    op.execute(
        business_rules.update().where(business_rules.c.version == 1).values(valid_to=_V1_VALID_TO)
    )
    op.execute(
        business_rules.update().where(business_rules.c.version == 2).values(valid_to=_V2_VALID_TO)
    )
    op.execute(
        business_rules.update()
        .where(business_rules.c.version == 3)
        .values(valid_from=_V3_VALID_FROM_NEW)
    )


def downgrade() -> None:
    op.execute(business_rules.update().where(business_rules.c.version == 1).values(valid_to=None))
    op.execute(business_rules.update().where(business_rules.c.version == 2).values(valid_to=None))
    op.execute(
        business_rules.update()
        .where(business_rules.c.version == 3)
        .values(valid_from=_V3_VALID_FROM_OLD)
    )
