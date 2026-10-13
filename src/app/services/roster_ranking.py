from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import and_, case, func, or_, select, tuple_
from sqlalchemy.sql.elements import ColumnElement
from sqlalchemy.sql.selectable import Lateral

from app.db.models.active_clients import (
    HANDLED_INTERACTION_TYPES,
    ActiveClientFund,
    ActiveClientInteraction,
)
from app.db.models.risk import ClientRiskFeatures
from app.pagination import decode_sort_cursor, encode_sort_cursor
from app.risk.scoring import RISK_BAND_ORDER

SORT_CLIENT_ID = "client_id"
SORT_RISK_SCORE = "risk_score"
SORT_FUND_AT_RISK = "fund_at_risk"
RANKED_SORTS = (SORT_RISK_SCORE, SORT_FUND_AT_RISK)

UNSCORED_SORT_VALUE = -1
SORT_CURSOR_SIZE = 5


@dataclass(frozen=True)
class RosterRanking:
    latest_handled_interaction: Lateral
    already_actioned: ColumnElement
    primary_key: ColumnElement
    secondary_key: ColumnElement

    def select_columns(self) -> list[ColumnElement]:
        return [
            self.already_actioned.label("sort_already_actioned"),
            self.primary_key.label("sort_primary_key"),
            self.secondary_key.label("sort_secondary_key"),
        ]

    def order_by_clauses(self) -> list[ColumnElement]:
        return [
            self.already_actioned.asc(),
            self.primary_key.desc(),
            self.secondary_key.desc(),
            ActiveClientFund.client_id.asc(),
            ActiveClientFund.unit_fund_id.asc(),
        ]

    def rows_after(self, cursor: str) -> ColumnElement:
        already_actioned, primary_key, secondary_key, client_id, unit_fund_id = decode_sort_cursor(
            cursor, SORT_CURSOR_SIZE
        )
        same_actioned = self.already_actioned == already_actioned
        same_primary = self.primary_key == primary_key
        return or_(
            self.already_actioned > already_actioned,
            and_(same_actioned, self.primary_key < primary_key),
            and_(same_actioned, same_primary, self.secondary_key < secondary_key),
            and_(
                same_actioned,
                same_primary,
                self.secondary_key == secondary_key,
                tuple_(ActiveClientFund.client_id, ActiveClientFund.unit_fund_id)
                > (client_id, unit_fund_id),
            ),
        )

    def cursor_after(self, row) -> str:
        return encode_sort_cursor(
            [
                row.sort_already_actioned,
                row.sort_primary_key,
                row.sort_secondary_key,
                row.client_id,
                row.unit_fund_id,
            ]
        )


def _band_rank(band_column) -> ColumnElement:
    return case({band: rank for rank, band in enumerate(RISK_BAND_ORDER)}, value=band_column)


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
    return RosterRanking(latest, already_actioned, fund_at_risk, score)
