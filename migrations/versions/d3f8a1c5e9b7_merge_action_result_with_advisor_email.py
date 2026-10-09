from __future__ import annotations

from collections.abc import Sequence

revision: str = "d3f8a1c5e9b7"
down_revision: str | Sequence[str] | None = ("7c3e750bad4e", "b6e1d4a9c372")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
