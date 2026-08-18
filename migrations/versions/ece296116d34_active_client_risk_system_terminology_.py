"""active client risk system terminology rename

Revision ID: ece296116d34
Revises: 97b20ad94a18
Create Date: 2026-08-18 08:55:52.770053

Same rename the terminology guide describes: no logic, threshold, or scored
result changes, only names. Renames the matching DB columns on
active_client_fund, client_risk_features, risk_snapshot, and digest_line,
and rekeys the persisted risk_config_version.thresholds/weights JSONB and
the current-state route/balance_tier/risk_reasons values on
client_risk_features to match. risk_snapshot and digest_line rows already
written keep the wording they were captured with -- both are run-scoped
history, never rewritten after the fact.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "ece296116d34"
down_revision: str | Sequence[str] | None = "97b20ad94a18"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ACTIVE_CLIENT_FUND_RENAMES = [
    ("n_purchases", "n_deposits"),
    ("n_sales", "n_withdrawals"),
    ("last_purchase", "last_deposit_date"),
    ("last_sale", "last_withdrawal_slot_date"),
    ("purchases_censored", "deposit_count_capped"),
    ("redemption_history_blind", "withdrawal_history_hidden"),
    ("rhythm_days", "typical_gap_days"),
    ("avg_ticket", "avg_deposit_amount"),
    ("max_ticket", "max_deposit_amount"),
    ("last_ticket", "last_deposit_amount"),
    ("ticket_trend", "deposit_trend"),
    ("largest_real_sale", "largest_withdrawal"),
    ("last_real_sale_date", "last_withdrawal_date"),
    ("fee_runway_months", "months_until_empty"),
]

# Shared column set: client_risk_features and risk_snapshot were created
# with the same six columns (see db/models/risk.py).
_RISK_FEATURE_RENAMES = [
    ("sig_drawdown", "sig_heavy_withdrawal"),
    ("sig_cadence_break", "sig_broken_pattern"),
    ("sig_fee_erosion", "sig_going_dormant"),
    ("lapse_ratio", "overdue_multiple"),
    ("credible_rhythm", "pattern_is_reliable"),
    ("aum_at_risk", "fund_at_risk"),
]

_DIGEST_LINE_RENAMES = [
    ("aum_at_risk", "fund_at_risk"),
    ("group_aum_total", "group_fund_value_total"),
]

_THRESHOLD_KEY_RENAMES = {
    "DRAWDOWN_HEAVY": "HEAVY_WITHDRAWAL_PCT",
    "LAPSE_MULTIPLE": "OVERDUE_MULTIPLE",
    "DECLINE_SLOPE": "SHRINKING_TREND",
    "DUST_BALANCE": "TINY_BALANCE",
    "MATERIAL_BALANCE": "WORTH_A_CALL_BALANCE",
    "FEE_RUNWAY_MONTHS": "MONTHS_UNTIL_EMPTY",
    "SYSTEM_SALE_MAX": "SYSTEM_FEE_MAX",
}

_WEIGHT_KEY_RENAMES = {
    "sig_drawdown": "sig_heavy_withdrawal",
    "sig_cadence_break": "sig_broken_pattern",
    "sig_fee_erosion": "sig_going_dormant",
}

_ROUTE_RENAMES = {
    "dust_cleanup": "small_balance_review",
    "fa_digest_watch": "fa_watchlist",
    "automated_nurture": "auto_checkin",
}

# Only the three signal labels whose wording actually changed.
_LABEL_RENAMES = {
    "Broke their own cadence": "Broke their own pattern",
    "No contribution in 12m": "No deposit in 12 months",
    "Heavy redemption": "Heavy withdrawal",
}

_risk_config_version = sa.table(
    "risk_config_version",
    sa.column("config_id", sa.BigInteger),
    sa.column("thresholds", JSONB),
    sa.column("weights", JSONB),
)

_client_risk_features = sa.table(
    "client_risk_features",
    sa.column("route", sa.Text),
    sa.column("balance_tier", sa.Text),
)


def _rekey(mapping: dict, renames: dict[str, str]) -> dict:
    return {renames.get(key, key): value for key, value in mapping.items()}


def upgrade() -> None:
    for old, new in _ACTIVE_CLIENT_FUND_RENAMES:
        op.alter_column("active_client_fund", old, new_column_name=new)
    for old, new in _RISK_FEATURE_RENAMES:
        op.alter_column("client_risk_features", old, new_column_name=new)
        op.alter_column("risk_snapshot", old, new_column_name=new)
    for old, new in _DIGEST_LINE_RENAMES:
        op.alter_column("digest_line", old, new_column_name=new)

    connection = op.get_bind()

    for row in connection.execute(
        sa.select(
            _risk_config_version.c.config_id,
            _risk_config_version.c.thresholds,
            _risk_config_version.c.weights,
        )
    ):
        connection.execute(
            _risk_config_version.update()
            .where(_risk_config_version.c.config_id == row.config_id)
            .values(
                thresholds=_rekey(row.thresholds, _THRESHOLD_KEY_RENAMES),
                weights=_rekey(row.weights, _WEIGHT_KEY_RENAMES),
            )
        )

    for old_route, new_route in _ROUTE_RENAMES.items():
        connection.execute(
            _client_risk_features.update()
            .where(_client_risk_features.c.route == old_route)
            .values(route=new_route)
        )
    connection.execute(
        _client_risk_features.update()
        .where(_client_risk_features.c.balance_tier == "Dust")
        .values(balance_tier="Tiny")
    )
    # Best-effort freshness only: the next nightly run overwrites this row's
    # risk_reasons regardless, this just avoids a stale label in between.
    for old_label, new_label in _LABEL_RENAMES.items():
        connection.execute(
            sa.text(
                "UPDATE client_risk_features SET risk_reasons = replace(risk_reasons, :old, :new)"
                " WHERE risk_reasons LIKE :pattern"
            ),
            {"old": old_label, "new": new_label, "pattern": f"%{old_label}%"},
        )


def downgrade() -> None:
    connection = op.get_bind()

    for old_label, new_label in _LABEL_RENAMES.items():
        connection.execute(
            sa.text(
                "UPDATE client_risk_features SET risk_reasons = replace(risk_reasons, :new, :old)"
                " WHERE risk_reasons LIKE :pattern"
            ),
            {"old": old_label, "new": new_label, "pattern": f"%{new_label}%"},
        )
    connection.execute(
        _client_risk_features.update()
        .where(_client_risk_features.c.balance_tier == "Tiny")
        .values(balance_tier="Dust")
    )
    for old_route, new_route in _ROUTE_RENAMES.items():
        connection.execute(
            _client_risk_features.update()
            .where(_client_risk_features.c.route == new_route)
            .values(route=old_route)
        )

    reverse_thresholds = {new: old for old, new in _THRESHOLD_KEY_RENAMES.items()}
    reverse_weights = {new: old for old, new in _WEIGHT_KEY_RENAMES.items()}
    for row in connection.execute(
        sa.select(
            _risk_config_version.c.config_id,
            _risk_config_version.c.thresholds,
            _risk_config_version.c.weights,
        )
    ):
        connection.execute(
            _risk_config_version.update()
            .where(_risk_config_version.c.config_id == row.config_id)
            .values(
                thresholds=_rekey(row.thresholds, reverse_thresholds),
                weights=_rekey(row.weights, reverse_weights),
            )
        )

    for old, new in reversed(_DIGEST_LINE_RENAMES):
        op.alter_column("digest_line", new, new_column_name=old)
    for old, new in reversed(_RISK_FEATURE_RENAMES):
        op.alter_column("risk_snapshot", new, new_column_name=old)
        op.alter_column("client_risk_features", new, new_column_name=old)
    for old, new in reversed(_ACTIVE_CLIENT_FUND_RENAMES):
        op.alter_column("active_client_fund", new, new_column_name=old)
