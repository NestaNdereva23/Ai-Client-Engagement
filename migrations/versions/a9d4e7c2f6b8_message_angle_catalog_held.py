"""message_angle_catalog.held, and hold see_what_changed pending the business

Revision ID: a9d4e7c2f6b8
Revises: d2f7c9a1e8b3
Create Date: 2026-08-04 09:00:00.000000

A held angle still resolves, generates, and reviews normally; only the send
itself is stopped, checked at the same point a suppression or a bounce is
checked. This is metadata about an angle's operational status, not part of
what the model is told, so it is backfilled directly rather than shipped as
a new catalogue version: no message's brief changes underneath it.

see_what_changed is the largest single angle (1,258 clients) and cannot
responsibly send until the business confirms its position on the exit
window it addresses. Lifting the hold is a later, separate catalogue change.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a9d4e7c2f6b8"
down_revision: str | Sequence[str] | None = "d2f7c9a1e8b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "message_angle_catalog",
        sa.Column("held", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.execute("UPDATE message_angle_catalog SET held = true WHERE angle = 'see_what_changed'")


def downgrade() -> None:
    op.drop_column("message_angle_catalog", "held")
