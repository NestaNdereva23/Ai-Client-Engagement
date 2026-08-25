"""The rule vocabulary against the two things it has to agree with.

A rule matches on an exact string, so a value the derivation never produces
makes a rule that silently never fires, and an angle with no catalogue entry
makes a message with no brief behind it. Neither fails on its own, so both are
pinned here.
"""

from __future__ import annotations

from datetime import date

from app.db.models.models import ClientFeatures
from app.db.session import SessionLocal
from app.rules.catalog import load_active_angles
from app.rules.store import MESSAGE_ANGLES, PRIORITY_TIERS, RULE_FIELD_DOMAINS
from app.transform import features as feat

IN_FORCE = date(2026, 8, 24)

# Each band, its declared vocabulary, and the function that produces it.
_BANDS = [
    ("recency_band", feat.RECENCY_BANDS, feat._recency_band, [None, 0, 365, 366, 1095, 2190, 5000]),
    ("value_band", feat.VALUE_BANDS, feat._value_band, [None, 0, *feat.VALUE_BAND_CUTOFFS, 1e9]),
    ("cadence_band", feat.CADENCE_BANDS, feat._cadence_band, [None, 0, 0.5, 1, 45, 90, 365, 400]),
    ("hold_band", feat.HOLD_BANDS, feat._hold_band, [None, 0, 60, 180, 364, 365, 4000]),
    ("purchase_depth", feat.PURCHASE_DEPTHS, feat._purchase_depth, [0, 1, 2, 4, 5, 9]),
    ("trend_band", feat.TREND_BANDS, feat._trend_band, [None, -1.0, -0.15, 0.0, 0.15, 1.0]),
    (
        "exit_reason",
        feat.EXIT_REASONS,
        feat._exit_reason,
        [None, "unit_sale", "bill_payment", "interest", "other"],
    ),
    (
        "fund_type",
        feat.FUND_TYPES,
        feat._fund_type,
        [None, "Cytonn Money Market Fund", "Cytonn High Yield Fund", "Something Else"],
    ),
]


def test_every_band_function_stays_inside_its_declared_vocabulary() -> None:
    for name, vocabulary, derive, inputs in _BANDS:
        produced = {derive(value) for value in inputs}
        outside = produced - set(vocabulary)
        assert not outside, f"{name} produced {outside}, which no rule could name"


def test_every_declared_band_value_is_reachable() -> None:
    """A value nothing produces would let someone write a rule that never fires."""
    for name, vocabulary, derive, inputs in _BANDS:
        produced = {derive(value) for value in inputs}
        unreachable = set(vocabulary) - produced
        assert not unreachable, f"{name} declares {unreachable} but never produces it"


def test_the_rule_domains_match_the_derivation() -> None:
    for name, vocabulary, _derive, _inputs in _BANDS:
        assert RULE_FIELD_DOMAINS[name] == set(vocabulary), f"{name} domain has drifted"


def test_every_band_is_a_column_a_rule_can_read() -> None:
    columns = set(ClientFeatures.__table__.columns.keys())
    for field in RULE_FIELD_DOMAINS:
        assert field in columns, f"rules may match {field}, but no feature column carries it"


def test_the_boolean_fields_are_carried_as_booleans() -> None:
    flags = (
        "in_wave",
        "has_depth",
        "staged_exit",
        "stale_contact",
        "newly_dormant",
        "holds_other_funds",
    )
    for flag in flags:
        assert RULE_FIELD_DOMAINS[flag] == {"true", "false"}


def test_every_angle_a_rule_may_resolve_to_has_a_brief(db: None) -> None:
    """Excludes the two the earlier rule sets use, which predate the catalogue."""
    with SessionLocal() as session:
        catalogued = set(load_active_angles(session, IN_FORCE))
    assert catalogued <= MESSAGE_ANGLES, "the catalogue holds an angle no rule may resolve to"

    legacy = {"winback_habit", "winback_flexible"}
    assert MESSAGE_ANGLES - legacy == catalogued


def test_the_four_derived_tiers_are_allowed() -> None:
    assert {"T1", "T2", "T3", "T4"} <= PRIORITY_TIERS
