"""separate db roles for the pii boundary

Revision ID: b5e7c1d9f2a3
Revises: 032df854c4cb
Create Date: 2026-07-24 09:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b5e7c1d9f2a3"
down_revision: str | Sequence[str] | None = "032df854c4cb"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Two group roles carry the boundary. restricted may touch pii_vault; safe is the
# model-facing path and gets no grant on it. Switched into with SET ROLE.
RESTRICTED = "ace_restricted"
SAFE = "ace_safe"

# Tables the safe role may read today. The vault and the raw model tables holding
# codes or exact figures are deliberately left out (deny by default).
SAFE_READABLE = ("client_features",)


def upgrade() -> None:
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{RESTRICTED}') THEN
                CREATE ROLE {RESTRICTED} NOLOGIN;
            END IF;
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{SAFE}') THEN
                CREATE ROLE {SAFE} NOLOGIN;
            END IF;
        END
        $$;
        """
    )

    # The connecting login role must be able to switch into either boundary role.
    op.execute(f"GRANT {RESTRICTED} TO CURRENT_USER")
    op.execute(f"GRANT {SAFE} TO CURRENT_USER")

    op.execute(f"GRANT USAGE ON SCHEMA public TO {RESTRICTED}")
    op.execute(f"GRANT USAGE ON SCHEMA public TO {SAFE}")

    # Vault: restricted only, never the world and never safe.
    op.execute("REVOKE ALL ON pii_vault FROM PUBLIC")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON pii_vault TO {RESTRICTED}")
    op.execute(f"REVOKE ALL ON pii_vault FROM {SAFE}")

    # Model-facing reads for the safe path.
    for table in SAFE_READABLE:
        op.execute(f"GRANT SELECT ON {table} TO {SAFE}")


def downgrade() -> None:
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{SAFE}') THEN
                EXECUTE 'DROP OWNED BY {SAFE}';
                EXECUTE 'DROP ROLE {SAFE}';
            END IF;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{RESTRICTED}') THEN
                EXECUTE 'DROP OWNED BY {RESTRICTED}';
                EXECUTE 'DROP ROLE {RESTRICTED}';
            END IF;
        END
        $$;
        """
    )
