"""release the see_what_changed hold

Revision ID: b11ce06f7b30
Revises: 7e2a94c6f108
Create Date: 2026-09-14 00:00:00.000000

The business question this hold was waiting on is resolved, so
see_what_changed goes back to sending like every other angle. The angle's
brief is unchanged; only its operational hold flag flips back.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b11ce06f7b30"
down_revision: str | Sequence[str] | None = "7e2a94c6f108"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        sa.text("UPDATE message_angle_catalog SET held = false WHERE angle = 'see_what_changed'")
    )


def downgrade() -> None:
    op.execute(
        sa.text("UPDATE message_angle_catalog SET held = true WHERE angle = 'see_what_changed'")
    )
