"""repair review cohort campaign fk cascade

Revision ID: ff42810be366
Revises: b6d2f8a3c7e5
Create Date: 2026-08-19 23:06:15.616478

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "ff42810be366"
down_revision: str | Sequence[str] | None = "b6d2f8a3c7e5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The review_cohort table's campaign_id FK was created without ON DELETE
# CASCADE in some environments, even though the migration that added it
# (f1a9c3e7b2d4) declares CASCADE. This repairs the drift so deleting a
# campaign also removes its review cohorts, as the model expects.
CONSTRAINT_NAME = "review_cohort_campaign_id_fkey"


def upgrade() -> None:
    op.drop_constraint(CONSTRAINT_NAME, "review_cohort", type_="foreignkey")
    op.create_foreign_key(
        CONSTRAINT_NAME,
        "review_cohort",
        "campaign",
        ["campaign_id"],
        ["campaign_id"],
        ondelete="CASCADE",
    )


def downgrade() -> None:
    op.drop_constraint(CONSTRAINT_NAME, "review_cohort", type_="foreignkey")
    op.create_foreign_key(
        CONSTRAINT_NAME,
        "review_cohort",
        "campaign",
        ["campaign_id"],
        ["campaign_id"],
    )
