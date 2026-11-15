from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f3b8d5a1c926"
down_revision: str | Sequence[str] | None = "a7c2e9f4b138"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ACTIVE_KINDS = "'nightly', 'intelligence', 'action', 'chat'"
_ALL_KINDS = f"{_ACTIVE_KINDS}, 'campaign', 'campaign_suggestions'"
_CAMPAIGN_KINDS = "('campaign', 'campaign_suggestions')"


def _set_kind_check(kinds: str) -> None:
    op.drop_constraint("ck_agent_run_agent_kind", "agent_run", type_="check")
    op.create_check_constraint("ck_agent_run_agent_kind", "agent_run", f"agent_kind IN ({kinds})")


def _restart_sequence(table: str, column: str) -> None:
    op.execute(
        f"SELECT setval(pg_get_serial_sequence('{table}', '{column}'), "
        f"COALESCE((SELECT max({column}) FROM {table}), 1), "
        f"EXISTS (SELECT 1 FROM {table}))"
    )


def upgrade() -> None:
    op.create_table(
        "campaign_agent_run",
        sa.Column("run_id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("campaign_id", sa.BigInteger(), nullable=True),
        sa.Column("trigger", sa.Text(), server_default="manual", nullable=False),
        sa.Column("state", sa.Text(), server_default="running", nullable=False),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("cost_kes", sa.Float(), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("plan_report", postgresql.JSONB(), nullable=True),
        sa.PrimaryKeyConstraint("run_id"),
        sa.ForeignKeyConstraint(
            ["campaign_id"],
            ["campaign.campaign_id"],
            name="fk_campaign_agent_run_campaign_id",
            ondelete="SET NULL",
        ),
        sa.CheckConstraint(
            "state IN ('running', 'completed', 'failed')", name="ck_campaign_agent_run_state"
        ),
        sa.CheckConstraint("kind IN ('plan', 'suggest')", name="ck_campaign_agent_run_kind"),
        sa.CheckConstraint(
            "cost_kes IS NULL OR cost_kes >= 0", name="ck_campaign_agent_run_cost_not_negative"
        ),
    )
    op.create_index(
        op.f("ix_campaign_agent_run_campaign_id"), "campaign_agent_run", ["campaign_id"]
    )
    op.create_table(
        "campaign_agent_tool_call",
        sa.Column("tool_call_id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.BigInteger(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("tool_name", sa.Text(), nullable=False),
        sa.Column("tool_input", postgresql.JSONB(), nullable=False),
        sa.Column("tool_output", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("tool_call_id"),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["campaign_agent_run.run_id"],
            name="fk_campaign_agent_tool_call_run_id",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("run_id", "ordinal", name="uq_campaign_agent_tool_call_run_ordinal"),
    )
    op.create_index(
        op.f("ix_campaign_agent_tool_call_run_id"), "campaign_agent_tool_call", ["run_id"]
    )

    op.execute(
        f"""
        INSERT INTO campaign_agent_run
            (run_id, kind, campaign_id, trigger, state, started_at, finished_at,
             summary, cost_kes, failure_reason, plan_report)
        SELECT ar.run_id,
               CASE ar.agent_kind WHEN 'campaign' THEN 'plan' ELSE 'suggest' END,
               CASE WHEN ar.agent_kind = 'campaign' THEN COALESCE(
                   (SELECT c.campaign_id FROM campaign c
                    WHERE c.campaign_id = (ar.plan_report->>'campaign_id')::bigint),
                   (SELECT e.campaign_id FROM touch_plan tp
                    JOIN enrollment e ON e.enrollment_id = tp.enrollment_id
                    WHERE tp.run_id = ar.run_id LIMIT 1)
               ) END,
               ar.trigger,
               CASE WHEN ar.state IN ('running', 'completed', 'failed')
                    THEN ar.state ELSE 'failed' END,
               ar.started_at, ar.finished_at, ar.summary, ar.cost_kes, ar.failure_reason,
               ar.plan_report
        FROM agent_run ar
        WHERE ar.agent_kind IN {_CAMPAIGN_KINDS}
        """
    )
    op.execute(
        f"""
        INSERT INTO campaign_agent_tool_call
            (tool_call_id, run_id, ordinal, tool_name, tool_input, tool_output, created_at)
        SELECT tc.tool_call_id, tc.run_id, tc.ordinal, tc.tool_name, tc.tool_input,
               tc.tool_output, tc.created_at
        FROM agent_tool_call tc
        JOIN agent_run ar ON ar.run_id = tc.run_id
        WHERE ar.agent_kind IN {_CAMPAIGN_KINDS}
        """
    )
    op.create_index(
        "uq_campaign_agent_run_one_running",
        "campaign_agent_run",
        ["kind", sa.text("COALESCE(campaign_id, 0)")],
        unique=True,
        postgresql_where=sa.text("state = 'running'"),
    )
    _restart_sequence("campaign_agent_run", "run_id")
    _restart_sequence("campaign_agent_tool_call", "tool_call_id")

    op.drop_constraint("touch_plan_run_id_fkey", "touch_plan", type_="foreignkey")
    op.create_foreign_key(
        "touch_plan_run_id_fkey", "touch_plan", "campaign_agent_run", ["run_id"], ["run_id"]
    )
    op.drop_constraint("fk_campaign_proposed_by_run_id", "campaign", type_="foreignkey")
    op.create_foreign_key(
        "fk_campaign_proposed_by_run_id",
        "campaign",
        "campaign_agent_run",
        ["proposed_by_run_id"],
        ["run_id"],
    )

    op.execute(
        f"""
        DELETE FROM agent_tool_call WHERE run_id IN
            (SELECT run_id FROM agent_run WHERE agent_kind IN {_CAMPAIGN_KINDS})
        """
    )
    op.execute(f"DELETE FROM agent_run WHERE agent_kind IN {_CAMPAIGN_KINDS}")
    _set_kind_check(_ACTIVE_KINDS)
    op.drop_column("agent_run", "plan_report")


def downgrade() -> None:
    op.add_column("agent_run", sa.Column("plan_report", postgresql.JSONB(), nullable=True))
    _set_kind_check(_ALL_KINDS)

    op.execute(
        """
        CREATE TEMP TABLE campaign_run_map AS
        SELECT run_id AS old_id, nextval(pg_get_serial_sequence('agent_run', 'run_id')) AS new_id
        FROM campaign_agent_run
        """
    )
    op.execute(
        """
        INSERT INTO agent_run
            (run_id, started_at, finished_at, state, trigger, agent_kind, summary, cost_kes,
             failure_reason, plan_report)
        SELECT m.new_id, r.started_at, r.finished_at, r.state,
               CASE WHEN r.trigger IN ('nightly', 'manual', 'chat') THEN r.trigger
                    ELSE 'manual' END,
               CASE r.kind WHEN 'plan' THEN 'campaign' ELSE 'campaign_suggestions' END,
               r.summary, r.cost_kes, r.failure_reason, r.plan_report
        FROM campaign_agent_run r
        JOIN campaign_run_map m ON m.old_id = r.run_id
        """
    )
    op.execute(
        """
        INSERT INTO agent_tool_call
            (run_id, ordinal, tool_name, tool_input, tool_output, created_at)
        SELECT m.new_id, tc.ordinal, tc.tool_name, tc.tool_input, tc.tool_output,
               tc.created_at
        FROM campaign_agent_tool_call tc
        JOIN campaign_run_map m ON m.old_id = tc.run_id
        """
    )

    op.drop_constraint("touch_plan_run_id_fkey", "touch_plan", type_="foreignkey")
    op.drop_constraint("fk_campaign_proposed_by_run_id", "campaign", type_="foreignkey")
    op.execute(
        """
        UPDATE touch_plan SET run_id = m.new_id
        FROM campaign_run_map m WHERE touch_plan.run_id = m.old_id
        """
    )
    op.execute(
        """
        UPDATE campaign SET proposed_by_run_id = m.new_id
        FROM campaign_run_map m WHERE campaign.proposed_by_run_id = m.old_id
        """
    )
    op.create_foreign_key(
        "touch_plan_run_id_fkey", "touch_plan", "agent_run", ["run_id"], ["run_id"]
    )
    op.create_foreign_key(
        "fk_campaign_proposed_by_run_id",
        "campaign",
        "agent_run",
        ["proposed_by_run_id"],
        ["run_id"],
    )

    op.execute("DROP TABLE campaign_run_map")
    op.drop_index(op.f("ix_campaign_agent_tool_call_run_id"), table_name="campaign_agent_tool_call")
    op.drop_table("campaign_agent_tool_call")
    op.drop_index(op.f("ix_campaign_agent_run_campaign_id"), table_name="campaign_agent_run")
    op.drop_table("campaign_agent_run")
