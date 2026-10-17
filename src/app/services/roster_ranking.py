from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import and_, case, func, literal_column, or_, select
from sqlalchemy.sql.elements import ColumnElement
from sqlalchemy.sql.selectable import Lateral

from app.briefing.daily_brief import NO_GROUP_RANK, SITUATION_GROUP_RANK
from app.db.models.active_clients import (
    HANDLED_INTERACTION_TYPES,
    ActiveClientFund,
    ActiveClientInteraction,
)
from app.db.models.risk import ClientRiskFeatures
from app.db.models.signals import ClientSituationState
from app.pagination import decode_sort_cursor, encode_sort_cursor
from app.risk.scoring import RISK_BAND_ORDER

SORT_CLIENT_ID = "client_id"
SORT_RISK_SCORE = "risk_score"
SORT_FUND_AT_RISK = "fund_at_risk"
SORT_BRIEF = "brief"
RANKED_SORTS = (SORT_RISK_SCORE, SORT_FUND_AT_RISK, SORT_BRIEF)

UNSCORED_SORT_VALUE = -1


@dataclass(frozen=True)
class RosterRanking:
    latest_handled_interaction: Lateral
    already_actioned: ColumnElement
    primary_key: ColumnElement
    secondary_key: ColumnElement
    group_rank: ColumnElement | None = None

    def _sort_keys(self) -> list[tuple[str, ColumnElement, bool]]:
        keys = [
            ("sort_already_actioned", self.already_actioned, True),
            ("sort_primary_key", self.primary_key, False),
            ("sort_secondary_key", self.secondary_key, False),
        ]
        if self.group_rank is not None:
            keys.insert(0, ("sort_group_rank", self.group_rank, True))
        return keys

    def _ordering(self) -> list[tuple[ColumnElement, bool]]:
        return [
            *((expression, ascending) for _, expression, ascending in self._sort_keys()),
            (ActiveClientFund.client_id, True),
            (ActiveClientFund.unit_fund_id, True),
        ]

    def select_columns(self) -> list[ColumnElement]:
        return [expression.label(label) for label, expression, _ in self._sort_keys()]

    def order_by_clauses(self) -> list[ColumnElement]:
        return [
            expression.asc() if ascending else expression.desc()
            for expression, ascending in self._ordering()
        ]

    def rows_after(self, cursor: str) -> ColumnElement:
        ordering = self._ordering()
        values = decode_sort_cursor(cursor, len(ordering))
        clauses = []
        for position, ((expression, ascending), value) in enumerate(
            zip(ordering, values, strict=True)
        ):
            same_so_far = [
                earlier == earlier_value
                for (earlier, _), earlier_value in zip(
                    ordering[:position], values[:position], strict=True
                )
            ]
            after = expression > value if ascending else expression < value
            clauses.append(and_(*same_so_far, after))
        return or_(*clauses)

    def cursor_after(self, row) -> str:
        return encode_sort_cursor(
            [
                *(getattr(row, label) for label, _, _ in self._sort_keys()),
                row.client_id,
                row.unit_fund_id,
            ]
        )


def _band_rank(band_column) -> ColumnElement:
    return case({band: rank for rank, band in enumerate(RISK_BAND_ORDER)}, value=band_column)


def brief_group_rank() -> ColumnElement:
    situation_rank = case(
        SITUATION_GROUP_RANK, value=ClientSituationState.situation_code, else_=NO_GROUP_RANK
    )
    best = (
        select(func.min(situation_rank))
        .where(
            ClientSituationState.client_id == ActiveClientFund.client_id,
            ClientSituationState.unit_fund_id == ActiveClientFund.unit_fund_id,
            ClientSituationState.is_active.is_(True),
        )
        .scalar_subquery()
    )
    return func.coalesce(best, NO_GROUP_RANK)


def _latest_handled_interaction():
    return (
        select(
            ActiveClientInteraction.id.label("interaction_id"),
            ActiveClientInteraction.risk_band_at_interaction.label("band_at_interaction"),
        )
        .where(
            ActiveClientInteraction.client_id == ActiveClientFund.client_id,
            ActiveClientInteraction.unit_fund_id == ActiveClientFund.unit_fund_id,
            ActiveClientInteraction.type.in_(HANDLED_INTERACTION_TYPES),
        )
        .order_by(ActiveClientInteraction.id.desc())
        .limit(1)
        .lateral("latest_handled")
    )


def build_roster_ranking(sort: str) -> RosterRanking:
    latest = _latest_handled_interaction()
    already_actioned = case(
        (latest.c.interaction_id.is_(None), 0),
        (latest.c.band_at_interaction.is_(None), 1),
        (_band_rank(ClientRiskFeatures.risk_band) <= _band_rank(latest.c.band_at_interaction), 1),
        else_=0,
    )
    score = func.coalesce(ClientRiskFeatures.risk_score, UNSCORED_SORT_VALUE)
    fund_at_risk = func.coalesce(ClientRiskFeatures.fund_at_risk, float(UNSCORED_SORT_VALUE))
    if sort == SORT_RISK_SCORE:
        return RosterRanking(latest, already_actioned, score, fund_at_risk)
    if sort == SORT_BRIEF:
        recent_window = func.current_date() - literal_column("interval '30 days'")
        heavy_withdrawal = case(
            (
                and_(
                    ClientRiskFeatures.sig_heavy_withdrawal.is_(True),
                    ActiveClientFund.last_withdrawal_date >= recent_window,
                ),
                2,
            ),
            (ClientRiskFeatures.sig_heavy_withdrawal.is_(True), 1),
            else_=0,
        )
        return RosterRanking(
            latest, already_actioned, heavy_withdrawal, fund_at_risk, brief_group_rank()
        )
    return RosterRanking(latest, already_actioned, fund_at_risk, score)
