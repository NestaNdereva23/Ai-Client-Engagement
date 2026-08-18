"""Golden-file tests for briefing/render.py.

The two fixture rows originally matched active_eda_out's own rendered
output verbatim (see the 2026-08-12 changelog entry for why that superseded
the earlier eda3_out reference); as of the FA-readability pass the wording
diverges from the notebook on purpose (plain percentages instead of raw
log10 slopes, no unexplained jargon) and these goldens were updated to
match render.py's new output, not the notebook's. Plus one test per
independently-triggered caveat.
"""

from __future__ import annotations

from app.briefing.render import BriefingFacts, render_briefing

_NO_SIGNALS = {
    "sig_broken_pattern": False,
    "sig_dormant": False,
    "sig_heavy_withdrawal": False,
    "sig_shrinking": False,
    "sig_going_dormant": False,
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
        days_since_deposit=100,
        last_deposit_amount=5_000.0,
        typical_gap_days=None,
        overdue_multiple=None,
        typical_deposit_amount=5_000.0,
        largest_deposit_amount=5_000.0,
        deposit_trend=None,
        largest_withdrawal=None,
        withdrawal_pct=None,
        days_since_withdrawal=None,
        signals=dict(_NO_SIGNALS),
        deposit_count_capped=False,
        withdrawal_history_hidden=False,
        holds_both_funds=False,
        months_until_empty=24.0,
        months_until_empty_threshold=12.0,
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
        days_since_deposit=481,
        last_deposit_amount=100_000.0,
        typical_gap_days=None,
        typical_deposit_amount=130_000.0,
        largest_deposit_amount=150_000.0,
        deposit_trend=-0.02,
        largest_withdrawal=150_000.0,
        withdrawal_pct=0.01,
        days_since_withdrawal=273,
        signals={**_NO_SIGNALS, "sig_dormant": True},
        deposit_count_capped=True,
    )
    expected = "\n".join(
        [
            "CLIENT BRIEFING  |  81734051  |  Cytonn High Yield Fund",
            "Risk 25/100 (Watch)   Route: fa_call_priority",
            "-" * 78,
            "Holding      KES 16,347,768.42  (Institutional)",
            "Last deposit 481 days ago, KES 100,000",
            "Their pattern  not measurable from the returned deposits",
            "Typical top-up  KES 130,000   largest KES 150,000",
            "Deposit trend   holding steady - no clear rise or fall in top-up size",
            "Largest visible withdrawal  KES 150,000  (1% of the balance it left)  273 days ago",
            "",
            "WHY THIS CLIENT SURFACED",
            "  - No deposit in 12 months",
            "",
            "KEEP IN MIND - don't say more to the client than these allow",
            "  ! only their last 5 deposits are visible here - their true frequency may differ",
        ]
    )
    assert render_briefing(facts) == expected


def test_matches_the_notebooks_own_rendering_for_an_auto_checkin_client() -> None:
    facts = _base(
        client_code="9683051",
        risk_score=90,
        risk_band="Critical",
        route="auto_checkin",
        balance=9_799.29,
        balance_tier="Small",
        days_since_deposit=925,
        last_deposit_amount=5_000.0,
        typical_gap_days=77.0,
        overdue_multiple=12.0,
        typical_deposit_amount=41_412.0,
        largest_deposit_amount=80_000.0,
        deposit_trend=-0.32,
        largest_withdrawal=20_000.0,
        withdrawal_pct=0.67,
        days_since_withdrawal=1094,
        signals={
            **_NO_SIGNALS,
            "sig_broken_pattern": True,
            "sig_dormant": True,
            "sig_heavy_withdrawal": True,
            "sig_shrinking": True,
        },
        deposit_count_capped=True,
    )
    expected = "\n".join(
        [
            "CLIENT BRIEFING  |  9683051  |  Cytonn High Yield Fund",
            "Risk 90/100 (Critical)   Route: auto_checkin",
            "-" * 78,
            "Holding      KES 9,799.29  (Small)",
            "Last deposit 925 days ago, KES 5,000",
            "Their pattern was roughly every 77 days - it's now been "
            "12.0x that long (well overdue)",
            "Typical top-up  KES 41,412   largest KES 80,000",
            "Deposit trend   shrinking - about 52% less each top-up",
            "Largest visible withdrawal  KES 20,000  (67% of the balance it left)  1,094 days ago",
            "",
            "WHY THIS CLIENT SURFACED",
            "  - Broke their own pattern",
            "  - No deposit in 12 months",
            "  - Heavy withdrawal",
            "  - Shrinking deposits",
            "",
            "KEEP IN MIND - don't say more to the client than these allow",
            "  ! only their last 5 deposits are visible here - their true frequency may differ",
        ]
    )
    assert render_briefing(facts) == expected


def test_no_withdrawal_visible_falls_back_to_the_caveat_pointer() -> None:
    text = render_briefing(_base(largest_withdrawal=None))
    assert "Withdrawals     none visible in the returned window (see note below)" in text


def test_deposit_count_capped_caveat_fires_independently() -> None:
    text = render_briefing(_base(deposit_count_capped=True))
    assert "only their last 5 deposits are visible here - their true frequency may differ" in text


def test_withdrawal_history_hidden_caveat_fires_independently() -> None:
    text = render_briefing(_base(withdrawal_history_hidden=True))
    assert "we can't see this client's withdrawal history" in text


def test_holds_both_funds_caveat_fires_independently() -> None:
    text = render_briefing(_base(holds_both_funds=True))
    assert "this client also holds a position in the other fund" in text


def test_months_until_empty_caveat_fires_independently() -> None:
    text = render_briefing(_base(months_until_empty=6.0, months_until_empty_threshold=12.0))
    assert "balance covers only 6.0 months of fees" in text


def test_months_until_empty_caveat_does_not_fire_above_the_threshold() -> None:
    text = render_briefing(_base(months_until_empty=24.0, months_until_empty_threshold=12.0))
    assert "months of fees" not in text


def test_open_complaint_caveat_fires_independently() -> None:
    text = render_briefing(_base(has_open_complaint=True))
    assert "this client has an open complaint" in text


def test_no_caveats_means_no_caveats_block_at_all() -> None:
    text = render_briefing(_base())
    assert "KEEP IN MIND" not in text


def test_all_caveats_can_fire_together() -> None:
    text = render_briefing(
        _base(
            deposit_count_capped=True,
            withdrawal_history_hidden=True,
            holds_both_funds=True,
            months_until_empty=1.0,
            months_until_empty_threshold=12.0,
            has_open_complaint=True,
        )
    )
    for caveat in (
        "only their last 5 deposits are visible here",
        "can't see this client's withdrawal history",
        "also holds a position in the other fund",
        "months of fees",
        "open complaint",
    ):
        assert caveat in text
