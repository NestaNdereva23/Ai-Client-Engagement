"""signal_threshold rows for near_preservation_line and segment_moved_up
Three months ahead for the line, seven days for a move up a segment."""

from collections.abc import Sequence
from datetime import date

import sqlalchemy as sa
from alembic import op

revision: str = "f4a9c2d7b1e8"
down_revision: str | Sequence[str] | None = "e1c6a8f3b5d9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_VALID_FROM = date(2026, 10, 13)
_ROWS = [
    {"signal_code": "near_preservation_line", "threshold_name": "horizon_months", "value": 3},
    {"signal_code": "segment_moved_up", "threshold_name": "window_days", "value": 7},
]

_TABLE = sa.table(
    "signal_threshold",
    sa.column("version", sa.Integer),
    sa.column("signal_code", sa.Text),
    sa.column("threshold_name", sa.Text),
    sa.column("value", sa.Float),
    sa.column("valid_from", sa.Date),
    sa.column("valid_to", sa.Date),
    sa.column("status", sa.Text),
)


def upgrade() -> None:
    op.bulk_insert(
        _TABLE,
        [
            {
                **row,
                "version": 1,
                "valid_from": _VALID_FROM,
                "valid_to": None,
                "status": "published",
            }
            for row in _ROWS
        ],
    )


def downgrade() -> None:
    for row in _ROWS:
        op.execute(
            _TABLE.delete().where(
                _TABLE.c.signal_code == row["signal_code"],
                _TABLE.c.threshold_name == row["threshold_name"],
                _TABLE.c.version == 1,
            )
        )
