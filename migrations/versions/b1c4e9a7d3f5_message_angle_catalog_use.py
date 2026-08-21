"""message_angle_catalog.use

Revision ID: b1c4e9a7d3f5
Revises: 3358df7209ce
Create Date: 2026-08-21 09:00:00.000000

Every earlier angle version told the model who it was talking to and what
it could claim, but not how retrieved product or market facts should be
used. Emails leaned on stale account history instead of a current
proposition, and some opened by asking the client to confirm contact
details. use closes that gap: a plain sentence, alongside who, claim, ask,
and never, saying whether and how RAG content belongs in this angle's
email. Nullable because version 1 predates the concept and is never
rewritten to add it.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b1c4e9a7d3f5"
down_revision: str | Sequence[str] | None = "3358df7209ce"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("message_angle_catalog", sa.Column("use", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("message_angle_catalog", "use")
