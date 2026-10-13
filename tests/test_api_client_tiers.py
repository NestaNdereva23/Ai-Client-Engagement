from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.db.models.active_clients import ActiveClientFund
from app.db.models.routing import ClientSide, ClientTier
from app.db.session import SessionLocal
from app.main import app
from app.routing.tier_playbook import SEGMENT_PLAYBOOK, TIER_PLAYBOOK
from app.routing.tiers import (
    ACTIVE_TIERS_IN_DISPLAY_ORDER,
    BELOW_CAPITAL_PRESERVATION_THRESHOLD,
    NO_TRANSACTIONS_IN_REVIEW_PERIOD,
    SEGMENT_ACTIVE_ACTIVE,
    SEGMENT_ACTIVE_DORMANT,
    SEGMENT_BY_TIER,
    TIER_AND_SEGMENT_BY_FACTS,
    TIER_FREQUENT_DEPOSITOR_WITHDRAWER,
    TIER_GRADUAL_DEPOSITOR,
)

client = TestClient(app)

ROSTER = "/api/v1/active-clients"
TIER_SUMMARY = "/api/v1/active-clients/tier-summary"
FUND_ID = 943

GRADUAL_CLIENT = 94360
FREQUENT_WITHDRAWER_CLIENT = 94361
NO_TRANSACTIONS_CLIENT = 94362
NOT_YET_TIERED_CLIENT = 94363
BELOW_THRESHOLD_CLIENT = 94364
INACTIVE_ONLY_CLIENT = 94365
ALL_TEST_CLIENTS = list(range(94360, 94366))


@pytest.fixture(autouse=True)
def _authed(configured_reviewers, reviewer_1_headers):
    client.headers.update(reviewer_1_headers)
    yield
    client.headers.pop("Authorization", None)


def _client_side(client_id: int, side: str, balance_kes: float) -> ClientSide:
    return ClientSide(
        client_id=client_id,
        side=side,
        balance=balance_kes,
        threshold_kes=6_000.0,
        source_feed="active" if side == "active" else "inactive",
    )


def _client_tier(
    client_id: int, tier: str, segment: str | None, purchased_recently: bool, real_withdrawer: bool
) -> ClientTier:
    return ClientTier(
        client_id=client_id,
        purchased_recently=purchased_recently,
        real_withdrawer=real_withdrawer,
        tier=tier,
        segment=segment,
        computed_at=datetime.now(UTC),
        thresholds_version=1,
        run_id="tier-api-test",
    )


def _delete_scenario(session) -> None:
    session.execute(delete(ClientTier).where(ClientTier.client_id.in_(ALL_TEST_CLIENTS)))
    session.execute(delete(ClientSide).where(ClientSide.client_id.in_(ALL_TEST_CLIENTS)))
    session.execute(
        delete(ActiveClientFund).where(ActiveClientFund.client_id.in_(ALL_TEST_CLIENTS))
    )
    session.commit()


@contextmanager
def seeded_tier_scenario():
    with SessionLocal() as session:
        _delete_scenario(session)
        session.add_all(
            ActiveClientFund(
                client_id=client_id,
                unit_fund_id=FUND_ID,
                balance=20_000.0,
                n_deposits=1,
                n_withdrawals=0,
            )
            for client_id in (
                GRADUAL_CLIENT,
                FREQUENT_WITHDRAWER_CLIENT,
                NO_TRANSACTIONS_CLIENT,
                NOT_YET_TIERED_CLIENT,
                BELOW_THRESHOLD_CLIENT,
            )
        )
        session.add_all(
            [
                _client_side(GRADUAL_CLIENT, "active", 20_000.0),
                _client_side(FREQUENT_WITHDRAWER_CLIENT, "active", 20_000.0),
                _client_side(NO_TRANSACTIONS_CLIENT, "active", 20_000.0),
                _client_side(NOT_YET_TIERED_CLIENT, "active", 20_000.0),
                _client_side(BELOW_THRESHOLD_CLIENT, "inactive", 100.0),
                _client_side(INACTIVE_ONLY_CLIENT, "inactive", 50.0),
            ]
        )
        session.flush()
        session.add_all(
            [
                _client_tier(
                    GRADUAL_CLIENT, TIER_GRADUAL_DEPOSITOR, SEGMENT_ACTIVE_DORMANT, False, False
                ),
                _client_tier(
                    FREQUENT_WITHDRAWER_CLIENT,
                    TIER_FREQUENT_DEPOSITOR_WITHDRAWER,
                    SEGMENT_ACTIVE_ACTIVE,
                    True,
                    True,
                ),
                _client_tier(
                    NO_TRANSACTIONS_CLIENT, NO_TRANSACTIONS_IN_REVIEW_PERIOD, None, False, False
                ),
                _client_tier(
                    BELOW_THRESHOLD_CLIENT, BELOW_CAPITAL_PRESERVATION_THRESHOLD, None, True, False
                ),
            ]
        )
        session.commit()
    try:
        yield
    finally:
        with SessionLocal() as session:
            _delete_scenario(session)


@pytest.fixture
def tier_scenario(db):
    with seeded_tier_scenario():
        yield


def _roster_row(client_id: int, **filters) -> dict | None:
    response = client.get(ROSTER, params={"client_id": client_id, **filters})
    assert response.status_code == 200
    items = response.json()["items"]
    return items[0] if items else None


def test_the_wording_table_covers_every_tier_the_classifier_can_return() -> None:
    classifier_tiers = {tier for tier, _ in TIER_AND_SEGMENT_BY_FACTS.values()}
    held_out_tiers = {NO_TRANSACTIONS_IN_REVIEW_PERIOD, BELOW_CAPITAL_PRESERVATION_THRESHOLD}
    assert set(TIER_PLAYBOOK) == classifier_tiers | held_out_tiers
    assert set(SEGMENT_PLAYBOOK) == set(SEGMENT_BY_TIER.values())
    for tier in ACTIVE_TIERS_IN_DISPLAY_ORDER:
        playbook = TIER_PLAYBOOK[tier]
        assert playbook.goal and playbook.fa_action and playbook.success_measure


def test_a_roster_row_carries_segment_tier_goal_and_action(tier_scenario) -> None:
    row = _roster_row(FREQUENT_WITHDRAWER_CLIENT)
    assert row["segment"] == "active_active"
    assert row["segment_label"] == "Active Active"
    assert row["tier"] == "frequent_depositor_withdrawer"
    assert row["tier_label"] == "Frequent Depositors + Withdrawers"
    assert row["tier_goal"] == "Turn activity into growth"
    assert row["tier_action"] == "Encourage net purchases; show interest earned by staying invested"


def test_a_roster_row_with_no_tier_row_returns_nulls(tier_scenario) -> None:
    row = _roster_row(NOT_YET_TIERED_CLIENT)
    assert row is not None
    for field in ("segment", "segment_label", "tier", "tier_label", "tier_goal", "tier_action"):
        assert row[field] is None


def test_a_held_out_client_shows_its_label_but_no_segment_goal_or_action(tier_scenario) -> None:
    row = _roster_row(NO_TRANSACTIONS_CLIENT)
    assert row["tier"] == NO_TRANSACTIONS_IN_REVIEW_PERIOD
    assert row["tier_label"] == "Not transacting in the review period"
    assert row["segment"] is None
    assert row["tier_goal"] is None
    assert row["tier_action"] is None


def test_the_roster_filters_by_segment_and_by_tier(tier_scenario) -> None:
    assert _roster_row(GRADUAL_CLIENT, segment="active_dormant") is not None
    assert _roster_row(GRADUAL_CLIENT, segment="active_active") is None
    assert _roster_row(GRADUAL_CLIENT, tier="gradual_depositor") is not None
    assert _roster_row(GRADUAL_CLIENT, tier="frequent_depositor") is None
    assert _roster_row(NOT_YET_TIERED_CLIENT, segment="active_dormant") is None


def test_the_profile_carries_the_segment_and_tier_block(tier_scenario) -> None:
    response = client.get(f"{ROSTER}/{FREQUENT_WITHDRAWER_CLIENT}/{FUND_ID}/profile")
    assert response.status_code == 200
    block = response.json()["segment_and_tier"]
    assert block["tier_label"] == "Frequent Depositors + Withdrawers"
    assert block["segment_label"] == "Active Active"
    assert block["purchased_recently"] is True
    assert block["real_withdrawer"] is True
    assert block["computed_at"] is not None


def test_the_profile_of_a_client_with_no_tier_row_returns_nulls(tier_scenario) -> None:
    response = client.get(f"{ROSTER}/{NOT_YET_TIERED_CLIENT}/{FUND_ID}/profile")
    assert response.status_code == 200
    assert set(response.json()["segment_and_tier"].values()) == {None}


def _clients_by_tier(summary: dict) -> dict[str, int]:
    return {row["tier"]: row["clients"] for row in summary["tiers"]}


def _held_out_clients(summary: dict) -> dict[str, int]:
    return {row["tier"]: row["clients"] for row in summary["held_out"]}


def test_the_tier_summary_lists_the_four_tiers_in_the_business_review_order(db) -> None:
    summary = client.get(TIER_SUMMARY).json()
    assert [row["tier"] for row in summary["tiers"]] == list(ACTIVE_TIERS_IN_DISPLAY_ORDER)
    assert [row["segment"] for row in summary["segments"]] == [
        SEGMENT_ACTIVE_ACTIVE,
        SEGMENT_ACTIVE_DORMANT,
    ]


def test_the_tier_summary_counts_add_up_to_the_active_base(db) -> None:
    before = client.get(TIER_SUMMARY).json()
    with seeded_tier_scenario():
        after = client.get(TIER_SUMMARY).json()

    assert after["active_clients"] - before["active_clients"] == 4
    assert after["inactive_clients"] - before["inactive_clients"] == 2
    assert (
        _clients_by_tier(after)[TIER_GRADUAL_DEPOSITOR]
        - _clients_by_tier(before)[TIER_GRADUAL_DEPOSITOR]
        == 1
    )
    assert (
        _clients_by_tier(after)[TIER_FREQUENT_DEPOSITOR_WITHDRAWER]
        - _clients_by_tier(before)[TIER_FREQUENT_DEPOSITOR_WITHDRAWER]
        == 1
    )
    held_out_before, held_out_after = _held_out_clients(before), _held_out_clients(after)
    assert (
        held_out_after[NO_TRANSACTIONS_IN_REVIEW_PERIOD]
        - held_out_before[NO_TRANSACTIONS_IN_REVIEW_PERIOD]
        == 1
    )
    assert (
        held_out_after[BELOW_CAPITAL_PRESERVATION_THRESHOLD]
        - held_out_before[BELOW_CAPITAL_PRESERVATION_THRESHOLD]
        == 1
    )
    assert after["active_clients_not_yet_tiered"] - before["active_clients_not_yet_tiered"] == 1

    tiered = sum(row["clients"] for row in after["tiers"])
    assert (
        tiered
        + held_out_after[NO_TRANSACTIONS_IN_REVIEW_PERIOD]
        + after["active_clients_not_yet_tiered"]
        == after["active_clients"]
    )
    assert sum(row["clients"] for row in after["segments"]) == tiered
