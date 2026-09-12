from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d4e8f2a6c1b7"
down_revision: str | Sequence[str] | None = "6f1a3d9c2b4e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("voice_contract", sa.Column("default_sign_off", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("voice_contract", "default_sign_off")
