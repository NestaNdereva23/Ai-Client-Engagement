"""Retiring the v1 and v2 rule sets: their windows close where the next one
began, each version is active for exactly the window it was given with no gap
and no overlap (v3 handed over to v4 later, for the hold_band rename, and v4
handed over to v5, for sitting_still), and the v1 vocabulary itself is gone
from the schema, the model boundary, the rule vocabulary, and the client
console.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import inspect, text

from app.db.models.models import ClientFeatures, Clients
from app.db.session import SessionLocal
from app.privacy.scanners import MODEL_ALLOWED_KEYS
from app.rules.store import MESSAGE_ANGLES, PRIORITY_TIERS, RULE_FIELD_DOMAINS, load_active_rules

_V1_ENDS = date(2026, 8, 1)
_V2_ENDS = date(2026, 8, 4)
_V3_ENDS = date(2026, 8, 11)
_V4_ENDS = date(2026, 8, 24)
_DROPPED_CLIENT_FEATURES_COLUMNS = {"archetype", "recency_bucket", "value_tier", "rhythm_band"}
_DROPPED_CLIENTS_COLUMNS = {"net_flow"}


def test_v1_is_active_only_before_v2_took_over() -> None:
    with SessionLocal() as session:
        before = load_active_rules(session, at=date(2026, 1, 15))
        on_the_boundary = load_active_rules(session, at=_V1_ENDS)
    assert {r.version for r in before} == {1}
    # valid_to is exclusive: the boundary date itself belongs to v2.
    assert {r.version for r in on_the_boundary} == {2}


def test_v2_is_active_only_between_v1_and_v3() -> None:
    with SessionLocal() as session:
        mid_window = load_active_rules(session, at=date(2026, 8, 2))
        on_the_boundary = load_active_rules(session, at=_V2_ENDS)
    assert {r.version for r in mid_window} == {2}
    assert {r.version for r in on_the_boundary} == {3}


def test_v3_is_the_active_version_between_v2_and_v4() -> None:
    with SessionLocal() as session:
        just_after = load_active_rules(session, at=date(2026, 8, 5))
        on_the_boundary = load_active_rules(session, at=_V3_ENDS)
    assert {r.version for r in just_after} == {3}
    # valid_to is exclusive: the boundary date itself belongs to v4.
    assert {r.version for r in on_the_boundary} == {4}


def test_v4_is_the_active_version_between_v3_and_v5() -> None:
    """v4 exists to carry the hold_band rename forward without mutating v3."""
    with SessionLocal() as session:
        just_after = load_active_rules(session, at=date(2026, 8, 12))
        on_the_boundary = load_active_rules(session, at=_V4_ENDS)
    assert {r.version for r in just_after} == {4}
    # valid_to is exclusive: the boundary date itself belongs to v5.
    assert {r.version for r in on_the_boundary} == {5}


def test_v5_is_the_active_version_from_its_cutover_onward() -> None:
    """v5 exists to add sitting_still for auto_checkin-routed clients."""
    with SessionLocal() as session:
        just_after = load_active_rules(session, at=date(2026, 8, 25))
        well_after = load_active_rules(session, at=date(2027, 1, 1))
    assert {r.version for r in just_after} == {5}
    assert {r.version for r in well_after} == {5}


def test_the_four_windows_neither_gap_nor_overlap() -> None:
    """Every day from v1's start onward resolves to exactly one version."""
    with SessionLocal() as session:
        for probe in (
            date(2026, 1, 1),
            date(2026, 7, 31),
            date(2026, 8, 1),
            date(2026, 8, 3),
            date(2026, 8, 4),
            date(2026, 8, 10),
            date(2026, 8, 11),
            date(2026, 9, 1),
            date(2026, 12, 1),
        ):
            active = load_active_rules(session, at=probe)
            versions = {r.version for r in active}
            assert len(versions) == 1, f"{probe} resolved to {versions}, expected exactly one"


# --- V8.5: nothing reads a dropped column ------------------------------


def test_the_dropped_columns_are_gone_from_the_schema() -> None:
    with SessionLocal() as session:
        db_columns = {
            row[0]
            for row in session.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'client_features'"
                )
            )
        }
        clients_columns = {
            row[0]
            for row in session.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'clients'"
                )
            )
        }
    assert db_columns.isdisjoint(_DROPPED_CLIENT_FEATURES_COLUMNS)
    assert clients_columns.isdisjoint(_DROPPED_CLIENTS_COLUMNS)


def test_the_dropped_columns_are_gone_from_the_orm_models() -> None:
    feature_columns = {c.key for c in inspect(ClientFeatures).columns}
    client_columns = {c.key for c in inspect(Clients).columns}
    assert feature_columns.isdisjoint(_DROPPED_CLIENT_FEATURES_COLUMNS)
    assert client_columns.isdisjoint(_DROPPED_CLIENTS_COLUMNS)


def test_the_rule_vocabulary_no_longer_names_the_v1_fields_or_angles() -> None:
    assert _DROPPED_CLIENT_FEATURES_COLUMNS.isdisjoint(RULE_FIELD_DOMAINS)
    assert "winback_habit" not in MESSAGE_ANGLES
    assert "winback_flexible" not in MESSAGE_ANGLES
    assert not {"P1", "P2", "P3"} & PRIORITY_TIERS


def test_the_model_boundary_allowlist_no_longer_names_the_v1_fields() -> None:
    assert _DROPPED_CLIENT_FEATURES_COLUMNS.isdisjoint(MODEL_ALLOWED_KEYS)
    assert "value_tier_label" not in MODEL_ALLOWED_KEYS
