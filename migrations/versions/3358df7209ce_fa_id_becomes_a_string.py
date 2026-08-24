"""fa_id becomes a string

The FA roster's identifier was an arbitrary integer (ACE_FA_ROSTER's
"fa_id:name:email:daily_capacity"), disconnected from the console app that
now calls this API by a real login username. fa_id becomes that username
directly wherever it's stored, so a digest/assignment lookup can key
straight off it. fa_assignment.fa_id, digest_line.covering_for_fa_id, and
digest_email_send.fa_id all carried the old integer roster ids; those rows
are cleared rather than cast, since a value like 1 has no meaningful string
form to convert to -- the next nightly run regenerates all three from the
re-seeded ACE_FA_ROSTER.

Revision ID: 3358df7209ce
Revises: b966435f9794
Create Date: 2026-08-20 17:05:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "3358df7209ce"
down_revision: str | Sequence[str] | None = "b966435f9794"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute(sa.text("DELETE FROM fa_assignment"))
    op.execute(sa.text("DELETE FROM digest_email_send"))
    op.execute(sa.text("UPDATE digest_line SET covering_for_fa_id = NULL"))

    op.alter_column(
        "fa_assignment",
        "fa_id",
        existing_type=sa.BigInteger(),
        type_=sa.Text(),
        nullable=True,
    )
    op.alter_column(
        "digest_email_send",
        "fa_id",
        existing_type=sa.BigInteger(),
        type_=sa.Text(),
        nullable=False,
    )
    op.alter_column(
        "digest_line",
        "covering_for_fa_id",
        existing_type=sa.BigInteger(),
        type_=sa.Text(),
        nullable=True,
    )


def downgrade() -> None:
    """Downgrade schema.

    Postgres has no implicit text to bigint cast, so each column needs an
    explicit USING clause or the ALTER is rejected outright, empty table or
    not. The rows are cleared just above, so the cast never has a real value
    to convert and cannot fail on one.
    """
    op.execute(sa.text("DELETE FROM fa_assignment"))
    op.execute(sa.text("DELETE FROM digest_email_send"))
    op.execute(sa.text("UPDATE digest_line SET covering_for_fa_id = NULL"))

    op.alter_column(
        "fa_assignment",
        "fa_id",
        existing_type=sa.Text(),
        type_=sa.BigInteger(),
        nullable=True,
        postgresql_using="fa_id::bigint",
    )
    op.alter_column(
        "digest_email_send",
        "fa_id",
        existing_type=sa.Text(),
        type_=sa.BigInteger(),
        nullable=False,
        postgresql_using="fa_id::bigint",
    )
    op.alter_column(
        "digest_line",
        "covering_for_fa_id",
        existing_type=sa.Text(),
        type_=sa.BigInteger(),
        nullable=True,
        postgresql_using="covering_for_fa_id::bigint",
    )
