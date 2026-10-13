from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.db.models.active_clients import ActiveClientFund
from app.db.models.fa_assignment import FaAssignment
from app.db.models.risk import ClientRiskFeatures
from app.db.models.routing import ClientTier
from app.db.session import SessionLocal
from app.main import app
from app.routing.tiers import (
    ACTIVE_TIERS_IN_DISPLAY_ORDER,
    BELOW_CAPITAL_PRESERVATION_THRESHOLD,
    NO_TRANSACTIONS_IN_REVIEW_PERIOD,
    SEGMENT_BY_TIER,
    TIER_FREQUENT_DEPOSITOR,
    TIER_FREQUENT_DEPOSITOR_WITHDRAWER,
    TIER_GRADUAL_DEPOSITOR,
    TIER_GRADUAL_DEPOSITOR_WITHDRAWER,
)
from app.workers.fa_assignment import FEED_SOURCE, SOURCE

client = TestClient(app)

ADVISOR = "fa-action-test-1"
OTHER_ADVISOR = "fa-action-test-2"
FUND = 945
SECOND_FUND = 946
BALANCE_PER_FUND_KES = 10_000.0
FUND_AT_RISK_PER_FUND_KES = 250.0
NO_SIGNALS = {
    "sig_heavy_withdrawal": False,
    "sig_dormant": False,
    "sig_broken_pattern": False,
    "sig_shrinking": False,
    "sig_going_dormant": False,
    "sig_never_repeated": False,
}

TWO_FUND_CLIENT = 94452
EMAIL_MATCHED_CLIENT = 94450
FALLBACK_OWNED_CLIENT = 94451
NO_TRANSACTIONS_CLIENT = 94455
BELOW_THRESHOLD_CLIENT = 94456
NOT_TIERED_CLIENT = 94457
OTHER_ADVISORS_CLIENT = 94458
UNASSIGNED_CLIENT = 94459
ALL_TEST_CLIENTS = list(range(94450, 94460))

# client id: (tier or None for no tier row, funds held, how the owner was found)
ADVISOR_BOOK = {
    EMAIL_MATCHED_CLIENT: (TIER_GRADUAL_DEPOSITOR, [FUND], FEED_SOURCE),
    FALLBACK_OWNED_CLIENT: (TIER_GRADUAL_DEPOSITOR, [FUND], SOURCE),
    TWO_FUND_CLIENT: (TIER_GRADUAL_DEPOSITOR_WITHDRAWER, [FUND, SECOND_FUND], FEED_SOURCE),
    94453: (TIER_FREQUENT_DEPOSITOR, [FUND], FEED_SOURCE),
    94454: (TIER_FREQUENT_DEPOSITOR_WITHDRAWER, [FUND], FEED_SOURCE),
    NO_TRANSACTIONS_CLIENT: (NO_TRANSACTIONS_IN_REVIEW_PERIOD, [FUND], FEED_SOURCE),
    BELOW_THRESHOLD_CLIENT: (BELOW_CAPITAL_PRESERVATION_THRESHOLD, [FUND], SOURCE),
    NOT_TIERED_CLIENT: (None, [FUND], FEED_SOURCE),
}


@pytest.fixture(autouse=True)
def _authed(configured_reviewers, reviewer_1_headers):
    client.headers.update(reviewer_1_headers)
    yield
    client.headers.pop("Authorization", None)


def _delete_test_rows(session) -> None:
    session.execute(
        delete(ClientRiskFeatures).where(ClientRiskFeatures.client_id.in_(ALL_TEST_CLIENTS))
    )
    session.execute(delete(FaAssignment).where(FaAssignment.client_id.in_(ALL_TEST_CLIENTS)))
    session.execute(delete(ClientTier).where(ClientTier.client_id.in_(ALL_TEST_CLIENTS)))
    session.execute(
        delete(ActiveClientFund).where(ActiveClientFund.client_id.in_(ALL_TEST_CLIENTS))
    )
    session.commit()


def _add_client(session, client_id, tier, funds, owner, owner_source) -> None:
    for fund_id in funds:
        session.add(
            ActiveClientFund(
                client_id=client_id,
                unit_fund_id=fund_id,
                client_code=f"C{client_id}",
                balance=BALANCE_PER_FUND_KES,
                n_deposits=1,
                n_withdrawals=0,
            )
        )
        if client_id != BELOW_THRESHOLD_CLIENT:
            session.add(
                ClientRiskFeatures(
                    client_id=client_id,
                    unit_fund_id=fund_id,
                    balance_tier="Small",
                    **NO_SIGNALS,
                    risk_score=25,
                    risk_band="Watch",
                    risk_reasons="test",
                    fund_at_risk=FUND_AT_RISK_PER_FUND_KES,
                    config_version=1,
                    route="monitor_only",
                    queue_rank=None,
                )
            )
        if owner is not None:
            session.add(
                FaAssignment(
                    client_id=client_id,
                    unit_fund_id=fund_id,
                    fa_id=owner,
                    fa_name="Test Advisor",
                    source=owner_source,
                )
            )
    if tier is not None:
        session.add(
            ClientTier(
                client_id=client_id,
                purchased_recently=False,
                real_withdrawer=False,
                tier=tier,
                segment=SEGMENT_BY_TIER.get(tier),
                computed_at=datetime.now(UTC),
                thresholds_version=1,
                run_id="fa-action-test",
            )
        )


@pytest.fixture
def seeded_books(db):
    with SessionLocal() as session:
        _delete_test_rows(session)
        for client_id, (tier, funds, owner_source) in ADVISOR_BOOK.items():
            _add_client(session, client_id, tier, funds, ADVISOR, owner_source)
        _add_client(
            session,
            OTHER_ADVISORS_CLIENT,
            TIER_GRADUAL_DEPOSITOR,
            [FUND],
            OTHER_ADVISOR,
            FEED_SOURCE,
        )
        _add_client(session, UNASSIGNED_CLIENT, TIER_GRADUAL_DEPOSITOR, [FUND], None, SOURCE)
        session.commit()
    yield
    with SessionLocal() as session:
        _delete_test_rows(session)


def _get_book(fa_id: str = ADVISOR, **params) -> dict:
    response = client.get(f"/api/v1/fa-action/{fa_id}", params={"limit": 200, **params})
    assert response.status_code == 200
    return response.json()


def _listed_ids_by_tier(book: dict) -> dict[str, list[int]]:
    return {
        group["tier"]: [item["client_id"] for item in group["items"]] for group in book["groups"]
    }


def _all_listed_ids(book: dict) -> list[int]:
    return [item["client_id"] for group in book["groups"] for item in group["items"]]


def test_every_assigned_active_client_appears_once_in_its_tier_group(seeded_books) -> None:
    book = _get_book()

    assert [group["tier"] for group in book["groups"]] == list(ACTIVE_TIERS_IN_DISPLAY_ORDER)
    assert _listed_ids_by_tier(book) == {
        TIER_GRADUAL_DEPOSITOR: [EMAIL_MATCHED_CLIENT, FALLBACK_OWNED_CLIENT],
        TIER_GRADUAL_DEPOSITOR_WITHDRAWER: [TWO_FUND_CLIENT],
        TIER_FREQUENT_DEPOSITOR: [94453],
        TIER_FREQUENT_DEPOSITOR_WITHDRAWER: [94454],
    }
    listed = _all_listed_ids(book)
    assert len(listed) == len(set(listed)) == 5


def test_a_client_with_two_funds_is_one_item_with_the_balances_added(seeded_books) -> None:
    group = next(g for g in _get_book()["groups"] if g["tier"] == TIER_GRADUAL_DEPOSITOR_WITHDRAWER)

    assert group["clients"] == 1
    assert group["items"][0]["funds_held"] == 2
    assert group["items"][0]["total_balance_kes"] == 2 * BALANCE_PER_FUND_KES


def test_held_out_clients_are_not_listed_but_are_counted(seeded_books) -> None:
    book = _get_book()

    assert not {NO_TRANSACTIONS_CLIENT, BELOW_THRESHOLD_CLIENT, NOT_TIERED_CLIENT} & set(
        _all_listed_ids(book)
    )
    held_out = {row["tier"]: row["clients"] for row in book["held_out"]}
    assert held_out == {
        NO_TRANSACTIONS_IN_REVIEW_PERIOD: 1,
        BELOW_CAPITAL_PRESERVATION_THRESHOLD: 1,
    }
    assert book["not_yet_tiered"] == 1


def test_the_counts_add_up_to_the_advisors_whole_book(seeded_books) -> None:
    book = _get_book()

    listed = sum(group["clients"] for group in book["groups"])
    held_out = sum(row["clients"] for row in book["held_out"])
    assert book["assigned_clients"] == len(ADVISOR_BOOK)
    assert listed + held_out + book["not_yet_tiered"] == book["assigned_clients"]


def test_each_group_carries_its_goal_and_action_text(seeded_books) -> None:
    groups = {group["tier"]: group for group in _get_book()["groups"]}

    gradual = groups[TIER_GRADUAL_DEPOSITOR]
    assert gradual["tier_label"] == "Gradual Depositors"
    assert gradual["segment"] == "active_dormant"
    assert gradual["tier_goal"] == "Raise deposit frequency"
    assert gradual["tier_action"] == (
        "Call to review goals; set up a regular top-up or standing order"
    )
    assert groups[TIER_FREQUENT_DEPOSITOR]["tier_goal"] == "Diversify"


def test_each_group_carries_its_balance_and_amount_at_risk(seeded_books) -> None:
    groups = {group["tier"]: group for group in _get_book()["groups"]}

    assert groups[TIER_GRADUAL_DEPOSITOR]["balance_kes"] == 2 * BALANCE_PER_FUND_KES
    assert groups[TIER_GRADUAL_DEPOSITOR]["fund_at_risk_kes"] == 2 * FUND_AT_RISK_PER_FUND_KES
    two_fund_group = groups[TIER_GRADUAL_DEPOSITOR_WITHDRAWER]
    assert two_fund_group["balance_kes"] == 2 * BALANCE_PER_FUND_KES
    assert two_fund_group["fund_at_risk_kes"] == 2 * FUND_AT_RISK_PER_FUND_KES
    assert groups[TIER_FREQUENT_DEPOSITOR]["fund_at_risk_kes"] == FUND_AT_RISK_PER_FUND_KES


def test_balances_of_clients_off_the_worklist_are_reported(seeded_books) -> None:
    book = _get_book()

    held_out = {row["tier"]: row["balance_kes"] for row in book["held_out"]}
    assert held_out == {
        NO_TRANSACTIONS_IN_REVIEW_PERIOD: BALANCE_PER_FUND_KES,
        BELOW_CAPITAL_PRESERVATION_THRESHOLD: BALANCE_PER_FUND_KES,
    }
    assert book["not_yet_tiered_balance_kes"] == BALANCE_PER_FUND_KES


def test_each_group_carries_the_success_line_and_the_segment_wording(seeded_books) -> None:
    groups = {group["tier"]: group for group in _get_book()["groups"]}

    assert groups[TIER_GRADUAL_DEPOSITOR]["success_measure"] == "Moves to Active Active"
    assert groups[TIER_GRADUAL_DEPOSITOR]["segment_goal"] == "Re-engage and raise deposit frequency"
    assert groups[TIER_GRADUAL_DEPOSITOR]["segment_definition"] == "No investment for over 3 months"
    assert groups[TIER_FREQUENT_DEPOSITOR]["segment_goal"] == "Deepen and diversify"


def test_the_book_says_when_the_tiers_were_computed(seeded_books) -> None:
    assert _get_book()["computed_at"] is not None


def test_the_book_is_named_for_the_fa_action_route_and_its_advisor(seeded_books) -> None:
    book = _get_book()

    assert book["route"] == "fa_action"
    assert book["fa_id"] == ADVISOR
    assert book["fa_name"] == "Test Advisor"


def test_the_owner_found_by_email_is_marked_apart_from_a_fallback_owner(seeded_books) -> None:
    group = next(g for g in _get_book()["groups"] if g["tier"] == TIER_GRADUAL_DEPOSITOR)
    matched_by_email = {
        item["client_id"]: item["owner_matched_by_email"] for item in group["items"]
    }

    assert matched_by_email == {EMAIL_MATCHED_CLIENT: True, FALLBACK_OWNED_CLIENT: False}


def test_another_advisors_clients_and_unassigned_clients_do_not_appear(seeded_books) -> None:
    own_ids = set(_all_listed_ids(_get_book()))
    other_book = _get_book(OTHER_ADVISOR)

    assert OTHER_ADVISORS_CLIENT not in own_ids
    assert UNASSIGNED_CLIENT not in own_ids
    assert _all_listed_ids(other_book) == [OTHER_ADVISORS_CLIENT]
    assert other_book["assigned_clients"] == 1


def test_paging_inside_one_group_walks_every_client_once(seeded_books) -> None:
    first = _get_book(tier=TIER_GRADUAL_DEPOSITOR, limit=1)
    assert [g["tier"] for g in first["groups"]] == [TIER_GRADUAL_DEPOSITOR]
    group = first["groups"][0]
    assert group["clients"] == 2
    assert [item["client_id"] for item in group["items"]] == [EMAIL_MATCHED_CLIENT]
    assert group["next_cursor"] is not None

    second = _get_book(tier=TIER_GRADUAL_DEPOSITOR, limit=1, cursor=group["next_cursor"])
    second_group = second["groups"][0]
    assert [item["client_id"] for item in second_group["items"]] == [FALLBACK_OWNED_CLIENT]
    assert second_group["next_cursor"] is None


def test_a_bad_request_is_a_400(seeded_books) -> None:
    url = f"/api/v1/fa-action/{ADVISOR}"

    assert client.get(url, params={"cursor": "abc"}).status_code == 400
    assert (
        client.get(url, params={"tier": TIER_GRADUAL_DEPOSITOR, "cursor": "abc"}).status_code == 400
    )
    assert client.get(url, params={"tier": NO_TRANSACTIONS_IN_REVIEW_PERIOD}).status_code == 400


def test_an_advisor_with_no_clients_gets_an_empty_book(db) -> None:
    book = _get_book("fa-action-nobody")

    assert book["assigned_clients"] == 0
    assert [group["clients"] for group in book["groups"]] == [0, 0, 0, 0]
    assert _all_listed_ids(book) == []
    assert book["fa_name"] is None
    assert book["computed_at"] is None
    assert [group["balance_kes"] for group in book["groups"]] == [0, 0, 0, 0]
