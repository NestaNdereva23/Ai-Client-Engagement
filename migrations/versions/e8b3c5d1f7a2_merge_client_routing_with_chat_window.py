from __future__ import annotations

from collections.abc import Sequence

revision: str = "e8b3c5d1f7a2"
down_revision: str | Sequence[str] | None = ("c5d8a3f1e7b9", "d7f3a9c21b58")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
