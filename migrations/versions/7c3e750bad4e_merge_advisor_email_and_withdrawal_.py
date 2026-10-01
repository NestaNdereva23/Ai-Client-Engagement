"""merge advisor email and withdrawal cluster tiers

Revision ID: 7c3e750bad4e
Revises: cb7edb7d728d, 8a329564c6d6
Create Date: 2026-10-01 11:09:14.705540

"""

from collections.abc import Sequence

revision: str = "7c3e750bad4e"
down_revision: str | Sequence[str] | None = ("cb7edb7d728d", "8a329564c6d6")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
