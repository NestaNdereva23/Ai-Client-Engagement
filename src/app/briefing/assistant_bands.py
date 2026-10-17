from __future__ import annotations

WITHDRAWAL_SMALL_BELOW = 0.2
WITHDRAWAL_MODERATE_BELOW = 0.5
WITHDRAWAL_LARGE_BELOW = 0.75

THIS_WEEK_DAYS = 7
THIS_MONTH_DAYS = 31
LAST_THREE_MONTHS_DAYS = 92

WEEKLY_GAP_DAYS = 10
MONTHLY_GAP_DAYS = 35
EVERY_TWO_MONTHS_GAP_DAYS = 70
QUARTERLY_GAP_DAYS = 130

SEVERAL_CONTACTS = 2


def withdrawal_size_band(share_of_balance: float | None) -> str:
    if share_of_balance is None or share_of_balance <= 0:
        return "none"
    if share_of_balance < WITHDRAWAL_SMALL_BELOW:
        return "small"
    if share_of_balance < WITHDRAWAL_MODERATE_BELOW:
        return "moderate"
    if share_of_balance < WITHDRAWAL_LARGE_BELOW:
        return "large"
    return "nearly_all"


def _age_band(days: int | None, missing: str) -> str:
    if days is None:
        return missing
    if days <= THIS_WEEK_DAYS:
        return "this_week"
    if days <= THIS_MONTH_DAYS:
        return "this_month"
    if days <= LAST_THREE_MONTHS_DAYS:
        return "last_three_months"
    return "older"


def withdrawal_recency_band(days_since: int | None) -> str:
    return _age_band(days_since, "none")


def contact_age_band(days_since: int | None) -> str:
    return _age_band(days_since, "never")


def deposit_rhythm_band(typical_gap_days: float | None) -> str:
    if typical_gap_days is None or typical_gap_days < 1:
        return "unknown"
    if typical_gap_days <= WEEKLY_GAP_DAYS:
        return "weekly"
    if typical_gap_days <= MONTHLY_GAP_DAYS:
        return "monthly"
    if typical_gap_days <= EVERY_TWO_MONTHS_GAP_DAYS:
        return "every_two_months"
    if typical_gap_days <= QUARTERLY_GAP_DAYS:
        return "quarterly"
    return "rarely"


def contact_count_band(count: int) -> str:
    if count <= 0:
        return "none"
    if count < SEVERAL_CONTACTS:
        return "one"
    return "several"
