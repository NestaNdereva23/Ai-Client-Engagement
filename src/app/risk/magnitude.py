"""Turn one client-fund's fired signals into one human-readable magnitude line.

Picks the single fired signal that weighs heaviest in this client's own
risk_config_version -- the signal that actually drove the score, not just
declaration order -- and renders one short phrase for how bad it is. Ties
break by SIGNAL_ORDER, the same order risk_reasons is joined in.
"""

from __future__ import annotations

from datetime import date

from app.risk.signals import SIGNAL_LABELS, SIGNAL_ORDER


def _days_since(occurred: date | None, reference_date: date) -> int | None:
    """Mirrors app.services.briefing._days_since: days to reference_date, clipped at zero."""
    if occurred is None:
        return None
    return max(0, (reference_date - occurred).days)


def _withdrawal_pct(largest_withdrawal: float | None, balance: float | None) -> float | None:
    """Mirrors app.services.briefing._withdrawal_pct."""
    if largest_withdrawal is None or balance is None:
        return None
    balance_before_withdrawal = balance + largest_withdrawal
    if balance_before_withdrawal <= 0:
        return None
    return largest_withdrawal / balance_before_withdrawal


def pick_primary_signal(signals: dict[str, bool], weights: dict[str, float]) -> str | None:
    """The fired signal with the largest weight in `weights`, or None when
    nothing fired. A tie goes to whichever comes first in SIGNAL_ORDER.

    Public because both the per-client magnitude phrase below and any
    book-wide "which signal drives most clients" breakdown need the exact
    same pick.
    """
    fired = [name for name in SIGNAL_ORDER if signals.get(name)]
    if not fired:
        return None
    return max(fired, key=lambda name: (weights.get(name, 0.0), -SIGNAL_ORDER.index(name)))


def _magnitude_text(
    name: str,
    *,
    days_since_deposit: int | None,
    overdue_multiple: float | None,
    withdrawal_pct: float | None,
    deposit_trend: float | None,
    months_until_empty: float | None,
) -> str | None:
    """The magnitude phrase for one signal, or None when the row doesn't
    carry the number that signal needs -- a gap to hide, not a crash.
    """
    if name == "sig_dormant" and days_since_deposit is not None:
        return f"{days_since_deposit} days since last deposit"
    if name == "sig_broken_pattern" and overdue_multiple is not None:
        return f"{overdue_multiple:.1f}x their own deposit pattern"
    if name == "sig_heavy_withdrawal" and withdrawal_pct is not None:
        return f"{withdrawal_pct * 100:.0f}% of balance withdrawn at once"
    if name == "sig_shrinking" and deposit_trend is not None:
        return f"deposit size declining, slope {deposit_trend:.2f}"
    if name == "sig_going_dormant" and months_until_empty is not None:
        return f"{months_until_empty:.1f} months until the balance empties"
    if name == "sig_never_repeated":
        return "only one deposit, ever"
    return None


def primary_signal_magnitude(
    *,
    signals: dict[str, bool],
    weights: dict[str, float],
    last_deposit: date | None,
    overdue_multiple: float | None,
    largest_withdrawal: float | None,
    balance: float | None,
    deposit_trend: float | None,
    months_until_empty: float | None,
    reference_date: date | None = None,
) -> str | None:
    """One line: the label and magnitude of whichever fired signal carries
    the largest weight in this client's own risk_config_version -- the
    signal that actually drove the score, not just the first one listed.
    None when nothing fired, the same "no signal" case risk_reasons
    already carries.
    """
    name = pick_primary_signal(signals, weights)
    if name is None:
        return None
    ref = reference_date if reference_date is not None else date.today()
    magnitude = _magnitude_text(
        name,
        days_since_deposit=_days_since(last_deposit, ref),
        overdue_multiple=overdue_multiple,
        withdrawal_pct=_withdrawal_pct(largest_withdrawal, balance),
        deposit_trend=deposit_trend,
        months_until_empty=months_until_empty,
    )
    label = SIGNAL_LABELS[name]
    return f"{label}: {magnitude}" if magnitude else label
