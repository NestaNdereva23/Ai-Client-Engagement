"""ModelFactBlock: the closed, typed set of real figures the model may see.

Every band is checked against the same vocabulary the derivation produces,
and every free-text field is checked against a reviewed closed list, so
nothing here can carry text the schema did not explicitly anticipate.
Amounts round to two significant figures at construction.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, model_validator

from app.transform.features import (
    CADENCE_BANDS,
    EXIT_REASONS,
    FUND_TYPES,
    HOLD_BANDS,
    PURCHASE_DEPTHS,
    RECENCY_BANDS,
    TREND_BANDS,
    VALUE_BANDS,
)

# The schema a message is stamped with, so it can be explained later even if
# the fields change. Bump on any change to the fields or their meaning.
MODEL_FACT_BLOCK_VERSION = 2

_MONTH_FORMAT = re.compile(r"^\d{4}-\d{2}$")

# The name a message may use for each fund type. A reviewed constant, not
# ingested text, so a wording change upstream can never widen what a message
# may say without a deliberate change here.
FUND_DISPLAY_NAMES = {
    "money_market": "Cytonn Money Market Fund",
    "high_yield": "Cytonn High Yield Fund",
}


def _band(values: Iterable[str]) -> type:
    return Literal[tuple(sorted(values))]


def round_sig_figs(value: float | int | None, sig_figs: int = 2) -> int | float | None:
    """Round toward the nearest value with only sig_figs digits of precision."""
    if value in (None, 0):
        return value
    digits = sig_figs - int(math.floor(math.log10(abs(value)))) - 1
    rounded = round(value, digits)
    return int(rounded) if rounded == int(rounded) else rounded


class ModelFactBlock(BaseModel):
    """The complete set of real figures a generated message may cite."""

    model_config = ConfigDict(extra="forbid")

    recency_band: _band(RECENCY_BANDS) | None = None
    value_band: _band(VALUE_BANDS) | None = None
    cadence_band: _band(CADENCE_BANDS) | None = None
    hold_band: _band(HOLD_BANDS) | None = None
    purchase_depth: _band(PURCHASE_DEPTHS) | None = None
    trend_band: _band(TREND_BANDS) | None = None
    exit_reason: _band(EXIT_REASONS) | None = None
    fund_type: _band(FUND_TYPES) | None = None
    in_wave: bool | None = None
    has_depth: bool | None = None
    staged_exit: bool | None = None
    stale_contact: bool | None = None
    newly_dormant: bool | None = None

    fund_name: _band(FUND_DISPLAY_NAMES.values()) | None = None
    years_since_exit: float | None = None
    typical_contribution_kes: int | float | None = None
    largest_contribution_kes: int | float | None = None
    invested_every_n_days: int | None = None
    days_held_after_last_topup: int | None = None
    month_they_left: str | None = None

    @model_validator(mode="after")
    def _apply_privacy_rules(self) -> ModelFactBlock:
        self.typical_contribution_kes = round_sig_figs(self.typical_contribution_kes)
        self.largest_contribution_kes = round_sig_figs(self.largest_contribution_kes)
        if self.years_since_exit is not None:
            self.years_since_exit = round(self.years_since_exit, 1)

        # A client with no cadence has nothing to quote; "None" is the band's
        # own value for that case, not an absent Python value.
        if self.cadence_band in (None, "None"):
            self.invested_every_n_days = None

        if self.month_they_left is not None and not _MONTH_FORMAT.match(self.month_they_left):
            raise ValueError(f"month_they_left must be YYYY-MM, got '{self.month_they_left}'")
        return self

    def to_dict(self, permitted_keys: Sequence[str] | None = None) -> dict[str, Any]:
        """Every present fact, or only the given subset when narrowing further.

        permitted_keys only ever narrows: it filters an already-validated,
        closed instance, so a key outside this schema can never appear here
        however permitted_keys is set.
        """
        raw = self.model_dump(exclude_none=True)
        if permitted_keys is None:
            return raw
        allowed = set(permitted_keys)
        return {key: value for key, value in raw.items() if key in allowed}
