"""The seeded v3 rule set: all twelve angles, in router order, reachable.

Full parity against the analysis output (all 4,497 rows) belongs to the
harness built for the feature bands; this checks the seed on its own terms,
one representative client per rule.
"""

from __future__ import annotations

from datetime import date

from app.db.session import SessionLocal
from app.rules.engine import resolve
from app.rules.store import load_active_rules

V3_VERSION = 3
# Inside v3's window: 2026-08-04 (fa894fc0413a's cutover) to 2026-08-11
# (209a9c997624 closed it for v4's hold_band rename). v3's content is
# immutable and this still pins it, just against the window it actually had
# rather than the open-ended one it shipped with before that cutover.
IN_FORCE = date(2026, 8, 10)

# In the order the router applies them.
EXPECTED_ORDER = (
    "not_a_goodbye",
    "wrong_shelf",
    "see_what_changed",
    "the_long_hold",
    "your_next_deposit",
    "second_try",
    "you_wound_down",
    "you_were_scaling",
    "you_were_fading",
    "back_on_schedule",
    "onboarding_retry",
    "pick_up_again",
)

# The bands every client carries, so a fixture only has to override what its
# rule actually tests.
_BASELINE = {
    "exit_reason": "client_sale",
    "fund_type": "money_market",
    "hold_band": "Stayed months",
    "in_wave": "false",
    "has_depth": "false",
    "purchase_depth": "few",
    "staged_exit": "false",
    "trend_band": "flat",
    "cadence_band": "Regular",
}


def _features(**overrides: str) -> dict[str, str]:
    view = dict(_BASELINE)
    view.update(overrides)
    return view


# One feature set per angle, built to match that rule and nothing above it.
CASES = {
    "not_a_goodbye": _features(exit_reason="charge_settled"),
    "wrong_shelf": _features(fund_type="high_yield", hold_band="Under 6m"),
    "see_what_changed": _features(in_wave="true", hold_band="Stayed years", has_depth="true"),
    "the_long_hold": _features(in_wave="true", hold_band="Stayed years", has_depth="false"),
    "your_next_deposit": _features(hold_band="Parked briefly", purchase_depth="capped"),
    "second_try": _features(hold_band="Parked briefly", purchase_depth="single"),
    "you_wound_down": _features(staged_exit="true"),
    "you_were_scaling": _features(trend_band="rising"),
    "you_were_fading": _features(trend_band="falling"),
    "back_on_schedule": _features(purchase_depth="capped", cadence_band="Tight"),
    "onboarding_retry": _features(purchase_depth="single"),
    "pick_up_again": _features(),
}


def test_the_seed_ships_all_twelve_angles_at_version_3() -> None:
    with SessionLocal() as session:
        active = load_active_rules(session, at=IN_FORCE)
    assert {r.version for r in active} == {V3_VERSION}
    assert {r.message_angle for r in active} == set(EXPECTED_ORDER)


def test_the_seed_keeps_the_appendix_a_order() -> None:
    with SessionLocal() as session:
        active = load_active_rules(session, at=IN_FORCE)
    ordered = sorted(active, key=lambda r: r.priority)
    assert tuple(r.message_angle for r in ordered) == EXPECTED_ORDER
    assert [r.priority for r in ordered] == sorted(r.priority for r in ordered)


def test_the_last_rule_is_the_unconditional_catch_all() -> None:
    with SessionLocal() as session:
        active = load_active_rules(session, at=IN_FORCE)
    last = max(active, key=lambda r: r.priority)
    assert last.message_angle == "pick_up_again"
    assert last.match == {}


# There is deliberately no "re-validate the stored set against RULE_FIELD_DOMAINS"
# test here any more: v3's shipped rows still legitimately name "Parked briefly",
# but that value left the live hold_band vocabulary when v4 (the rename) shipped.
# Re-validating frozen content against a vocabulary that has since moved on
# will always fail once any value it used is retired -- that is expected, not
# a defect, the same way v1 and v2's own vocabulary is gone from RULE_FIELD_DOMAINS
# without either of them being touched. test_rules_v4_seed.py re-proves this
# same check against the live vocabulary, which is the one it should hold for.


def test_prompt_variant_is_the_angle_identifier() -> None:
    """Ahead of the catalogue lookup: the seed already carries the angle name."""
    with SessionLocal() as session:
        active = load_active_rules(session, at=IN_FORCE)
    assert all(r.prompt_variant == r.message_angle for r in active)


def test_each_angles_representative_client_resolves_to_it() -> None:
    with SessionLocal() as session:
        rules = load_active_rules(session, at=IN_FORCE)
    for angle, features in CASES.items():
        resolution = resolve(features, rules)
        assert resolution.message_angle == angle, (
            f"features for {angle} resolved to {resolution.message_angle} instead"
        )


def test_a_client_matching_nothing_specific_falls_through_to_the_catch_all() -> None:
    with SessionLocal() as session:
        rules = load_active_rules(session, at=IN_FORCE)
    resolution = resolve(_features(), rules)
    assert resolution.message_angle == "pick_up_again"


def test_a_charge_settled_exit_wins_over_every_other_signal() -> None:
    """not_a_goodbye sits first because it overrides everything else true of the client."""
    with SessionLocal() as session:
        rules = load_active_rules(session, at=IN_FORCE)
    features = _features(
        exit_reason="charge_settled",
        in_wave="true",
        hold_band="Stayed years",
        has_depth="true",
        staged_exit="true",
        trend_band="rising",
    )
    resolution = resolve(features, rules)
    assert resolution.message_angle == "not_a_goodbye"
