"""Golden-file tests for briefing/render.py against active_eda_out's own
rendered output for the same two fixture rows (see the 2026-08-12 changelog
entry for why this supersedes the earlier eda3_out reference), plus one test
per independently-triggered caveat.
"""

from __future__ import annotations

from app.briefing.render import BriefingFacts, render_briefing

_NO_SIGNALS = {
    "sig_cadence_break": False,
    "sig_dormant": False,
    "sig_drawdown": False,
    "sig_shrinking": False,
    "sig_fee_erosion": False,
    "sig_never_repeated": False,
}


def _base(**overrides) -> BriefingFacts:
    fields = dict(
        client_code="1",
        fund_name="Cytonn High Yield Fund",
        risk_score=25,
        risk_band="Watch",
        route="fa_call_priority",
        balance=100_000.0,
        balance_tier="Small",
        days_since_purchase=100,
        last_ticket=5_000.0,
        own_rhythm_days=None,
        overdue_multiple=None,
        typical_ticket=5_000.0,
        largest_ticket=5_000.0,
        ticket_trend=None,
        largest_real_redemption=None,
        drawdown_depth=None,
        days_since_real_redemption=None,
        signals=dict(_NO_SIGNALS),
        purchases_censored=False,
        redemption_history_blind=False,
        holds_both_funds=False,
        fee_runway_months=24.0,
        fee_runway_threshold=12.0,
        has_open_complaint=False,
    )
    fields.update(overrides)
    return BriefingFacts(**fields)


def test_matches_the_notebooks_own_rendering_for_a_call_priority_client() -> None:
    facts = _base(
        client_code="81734051",
        risk_score=25,
        risk_band="Watch",
        route="fa_call_priority",
        balance=16_347_768.42,
        balance_tier="Institutional",
        days_since_purchase=481,
        last_ticket=100_000.0,
        own_rhythm_days=None,
        typical_ticket=130_000.0,
        largest_ticket=150_000.0,
        ticket_trend=-0.02,
        largest_real_redemption=150_000.0,
        drawdown_depth=0.01,
        days_since_real_redemption=273,
        signals={**_NO_SIGNALS, "sig_dormant": True},
        purchases_censored=True,
    )
    expected = "\n".join(
        [
            "CLIENT BRIEFING  |  81734051  |  Cytonn High Yield Fund",
            "Risk 25/100 (Watch)   Route: fa_call_priority",
            "-" * 78,
            "Holding      KES 16,347,768.42  (Institutional)",
            "Last deposit 481 days ago, KES 100,000",
            "Their pattern  not measurable from the returned purchases",
            "Typical top-up  KES 130,000   largest KES 150,000",
            "Deposit trend   shrinking (-0.02 log10 per top-up)",
            "Largest visible redemption  KES 150,000  (1% of the balance it left)  273 days ago",
            "",
            "WHY THIS CLIENT SURFACED",
            "  - No contribution in 12m",
            "",
            "CAVEATS - do not assert beyond these",
            "  ! purchase history truncated at 5 - true frequency is unknown",
        ]
    )
    assert render_briefing(facts) == expected


def test_matches_the_notebooks_own_rendering_for_an_automated_nurture_client() -> None:
    facts = _base(
        client_code="9683051",
        risk_score=90,
        risk_band="Critical",
        route="automated_nurture",
        balance=9_799.29,
        balance_tier="Small",
        days_since_purchase=925,
        last_ticket=5_000.0,
        own_rhythm_days=77.0,
        overdue_multiple=12.0,
        typical_ticket=41_412.0,
        largest_ticket=80_000.0,
        ticket_trend=-0.32,
        largest_real_redemption=20_000.0,
        drawdown_depth=0.67,
        days_since_real_redemption=1094,
        signals={
            **_NO_SIGNALS,
            "sig_cadence_break": True,
            "sig_dormant": True,
            "sig_drawdown": True,
            "sig_shrinking": True,
        },
        purchases_censored=True,
    )
    expected = "\n".join(
        [
            "CLIENT BRIEFING  |  9683051  |  Cytonn High Yield Fund",
            "Risk 90/100 (Critical)   Route: automated_nurture",
            "-" * 78,
            "Holding      KES 9,799.29  (Small)",
            "Last deposit 925 days ago, KES 5,000",
            "Their pattern was roughly every 77 days - now 12.0x overdue",
            "Typical top-up  KES 41,412   largest KES 80,000",
            "Deposit trend   shrinking (-0.32 log10 per top-up)",
            "Largest visible redemption  KES 20,000  (67% of the balance it left)  1,094 days ago",
            "",
            "WHY THIS CLIENT SURFACED",
            "  - Broke their own cadence",
            "  - No contribution in 12m",
            "  - Heavy redemption",
            "  - Shrinking deposits",
            "",
            "CAVEATS - do not assert beyond these",
            "  ! purchase history truncated at 5 - true frequency is unknown",
        ]
    )
    assert render_briefing(facts) == expected


def test_no_redemption_visible_falls_back_to_the_caveat_pointer() -> None:
    text = render_briefing(_base(largest_real_redemption=None))
    assert "Redemptions     none visible in the returned window (see caveats)" in text


def test_purchases_censored_caveat_fires_independently() -> None:
    text = render_briefing(_base(purchases_censored=True))
    assert "purchase history truncated at 5 - true frequency is unknown" in text


def test_redemption_history_blind_caveat_fires_independently() -> None:
    text = render_briefing(_base(redemption_history_blind=True))
    assert "both sale slots hold system postings - redemption history is hidden" in text


def test_holds_both_funds_caveat_fires_independently() -> None:
    text = render_briefing(_base(holds_both_funds=True))
    assert "this client also holds a position in the other fund" in text


def test_fee_runway_caveat_fires_independently() -> None:
    text = render_briefing(_base(fee_runway_months=6.0, fee_runway_threshold=12.0))
    assert "balance covers only 6.0 months of fees" in text


def test_fee_runway_caveat_does_not_fire_above_the_threshold() -> None:
    text = render_briefing(_base(fee_runway_months=24.0, fee_runway_threshold=12.0))
    assert "months of fees" not in text


def test_open_complaint_caveat_fires_independently() -> None:
    text = render_briefing(_base(has_open_complaint=True))
    assert "this client has an open complaint" in text


def test_no_caveats_means_no_caveats_block_at_all() -> None:
    text = render_briefing(_base())
    assert "CAVEATS" not in text


def test_all_caveats_can_fire_together() -> None:
    text = render_briefing(
        _base(
            purchases_censored=True,
            redemption_history_blind=True,
            holds_both_funds=True,
            fee_runway_months=1.0,
            fee_runway_threshold=12.0,
            has_open_complaint=True,
        )
    )
    for caveat in (
        "purchase history truncated at 5",
        "redemption history is hidden",
        "also holds a position in the other fund",
        "months of fees",
        "open complaint",
    ):
        assert caveat in text
