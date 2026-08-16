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


def _drawdown_ratio(largest_real_sale: float | None, balance: float | None) -> float | None:
    """Mirrors app.services.briefing._drawdown_depth."""
    if largest_real_sale is None or balance is None:
        return None
    implied_prior_balance = balance + largest_real_sale
    if implied_prior_balance <= 0:
        return None
    return largest_real_sale / implied_prior_balance


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
    days_since_purchase: int | None,
    lapse_ratio: float | None,
    drawdown_ratio: float | None,
    ticket_trend: float | None,
    fee_runway_months: float | None,
) -> str | None:
    """The magnitude phrase for one signal, or None when the row doesn't
    carry the number that signal needs -- a gap to hide, not a crash.
    """
    if name == "sig_dormant" and days_since_purchase is not None:
        return f"{days_since_purchase} days since last purchase"
    if name == "sig_cadence_break" and lapse_ratio is not None:
        return f"{lapse_ratio:.1f}x their own purchase rhythm"
    if name == "sig_drawdown" and drawdown_ratio is not None:
        return f"{drawdown_ratio * 100:.0f}% of balance withdrawn in one sale"
    if name == "sig_shrinking" and ticket_trend is not None:
        return f"ticket size declining, slope {ticket_trend:.2f}"
    if name == "sig_fee_erosion" and fee_runway_months is not None:
        return f"{fee_runway_months:.1f} months of fee runway left"
    if name == "sig_never_repeated":
        return "only one purchase, ever"
    return None


def primary_signal_magnitude(
    *,
    signals: dict[str, bool],
    weights: dict[str, float],
    last_purchase: date | None,
    lapse_ratio: float | None,
    largest_real_sale: float | None,
    balance: float | None,
    ticket_trend: float | None,
    fee_runway_months: float | None,
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
        days_since_purchase=_days_since(last_purchase, ref),
        lapse_ratio=lapse_ratio,
        drawdown_ratio=_drawdown_ratio(largest_real_sale, balance),
        ticket_trend=ticket_trend,
        fee_runway_months=fee_runway_months,
    )
    label = SIGNAL_LABELS[name]
    return f"{label}: {magnitude}" if magnitude else label
