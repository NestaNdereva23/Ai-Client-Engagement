from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, update

from app.db.models.active_clients import ActiveClientFund, ActiveClientInteraction
from app.db.models.fa_assignment import FaAssignment
from app.db.models.risk import ClientRiskFeatures
from app.db.models.routing import ClientTier
from app.db.session import SessionLocal
from app.main import app
from app.routing.tiers import (
    NO_TRANSACTIONS_IN_REVIEW_PERIOD,
    SEGMENT_BY_TIER,
    TIER_FREQUENT_DEPOSITOR,
    TIER_FREQUENT_DEPOSITOR_WITHDRAWER,
    TIER_GRADUAL_DEPOSITOR,
    TIER_GRADUAL_DEPOSITOR_WITHDRAWER,
)

client = TestClient(app)

ROSTER = "/api/v1/active-clients"
ADVISOR = "roster-rank-fa-1"
OTHER_ADVISOR = "roster-rank-fa-2"
FUND = 956

CRITICAL_HIGH_SCORE_CLIENT = 95610
HIGH_SCORE_BIG_MONEY_CLIENT = 95611
HIGH_SCORE_SMALL_MONEY_CLIENT = 95612
LOW_SCORE_HUGE_MONEY_CLIENT = 95613
HELD_OUT_CLIENT = 95614
NOT_TIERED_CLIENT = 95615
OTHER_ADVISORS_CLIENT = 95616
UNSCORED_CLIENT = 95617
ALL_TEST_CLIENTS = list(range(95610, 95618))

NO_SIGNALS = {
    "sig_heavy_withdrawal": False,
    "sig_dormant": False,
    "sig_broken_pattern": False,
    "sig_shrinking": False,
    "sig_going_dormant": False,
    "sig_never_repeated": False,
}

# client id: (tier or None for no tier row, owner, (score, band, fund at risk) or None if unscored)
BOOK = {
    CRITICAL_HIGH_SCORE_CLIENT: (TIER_GRADUAL_DEPOSITOR, ADVISOR, (80, "Critical", 1_000.0)),
    HIGH_SCORE_BIG_MONEY_CLIENT: (
        TIER_FREQUENT_DEPOSITOR_WITHDRAWER,
        ADVISOR,
        (60, "High", 9_000.0),
    ),
    HIGH_SCORE_SMALL_MONEY_CLIENT: (
        TIER_GRADUAL_DEPOSITOR_WITHDRAWER,
        ADVISOR,
        (60, "High", 2_000.0),
    ),
    LOW_SCORE_HUGE_MONEY_CLIENT: (TIER_FREQUENT_DEPOSITOR, ADVISOR, (10, "Low", 50_000.0)),
    HELD_OUT_CLIENT: (NO_TRANSACTIONS_IN_REVIEW_PERIOD, ADVISOR, (90, "Critical", 3_000.0)),
    NOT_TIERED_CLIENT: (None, ADVISOR, (70, "High", 4_000.0)),
    OTHER_ADVISORS_CLIENT: (TIER_GRADUAL_DEPOSITOR, OTHER_ADVISOR, (99, "Critical", 8_000.0)),
    UNSCORED_CLIENT: (TIER_GRADUAL_DEPOSITOR, ADVISOR, None),
}
WORKLIST_BY_SCORE = [
    CRITICAL_HIGH_SCORE_CLIENT,
    HIGH_SCORE_BIG_MONEY_CLIENT,
    HIGH_SCORE_SMALL_MONEY_CLIENT,
    LOW_SCORE_HUGE_MONEY_CLIENT,
    UNSCORED_CLIENT,
]
WORKLIST_BY_FUND_AT_RISK = [
    LOW_SCORE_HUGE_MONEY_CLIENT,
    HIGH_SCORE_BIG_MONEY_CLIENT,
    HIGH_SCORE_SMALL_MONEY_CLIENT,
    CRITICAL_HIGH_SCORE_CLIENT,
    UNSCORED_CLIENT,
]


@pytest.fixture(autouse=True)
def _authed(configured_reviewers, reviewer_1_headers):
    client.headers.update(reviewer_1_headers)
    yield
    client.headers.pop("Authorization", None)


def _delete_test_rows(session) -> None:
    for model in (ActiveClientInteraction, ClientRiskFeatures, FaAssignment, ActiveClientFund):
        session.execute(delete(model).where(model.client_id.in_(ALL_TEST_CLIENTS)))
    session.execute(delete(ClientTier).where(ClientTier.client_id.in_(ALL_TEST_CLIENTS)))
    session.commit()


@pytest.fixture
def seeded_book(db):
    with SessionLocal() as session:
        _delete_test_rows(session)
        for client_id, (tier, owner, risk) in BOOK.items():
            session.add(
                ActiveClientFund(
                    client_id=client_id,
                    unit_fund_id=FUND,
                    client_code=f"C{client_id}",
                    balance=10_000.0,
                    n_deposits=1,
                    n_withdrawals=0,
                )
            )
            session.add(
                FaAssignment(
                    client_id=client_id,
                    unit_fund_id=FUND,
                    fa_id=owner,
                    fa_name="Rank Test",
                    source="feed",
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
                        run_id="roster-rank-test",
                    )
                )
            if risk is not None:
                score, band, fund_at_risk = risk
                session.add(
                    ClientRiskFeatures(
                        client_id=client_id,
                        unit_fund_id=FUND,
                        balance_tier="Small",
                        **NO_SIGNALS,
                        risk_score=score,
                        risk_band=band,
                        risk_reasons="test",
                        fund_at_risk=fund_at_risk,
                        config_version=1,
                        route="monitor_only",
                        queue_rank=None,
                    )
                )
        session.commit()
    yield
    with SessionLocal() as session:
        _delete_test_rows(session)


def _log_handled_call(client_id: int, band_when_logged: str | None) -> None:
    with SessionLocal() as session:
        session.add(
            ActiveClientInteraction(
                client_id=client_id,
                unit_fund_id=FUND,
                type="call_logged",
                reviewer_id=ADVISOR,
                risk_band_at_interaction=band_when_logged,
            )
        )
        session.commit()


def _ids(params: dict) -> list[int]:
    response = client.get(ROSTER, params={"fa_id": ADVISOR, "limit": 200, **params})
    assert response.status_code == 200
    return [row["client_id"] for row in response.json()["items"]]


def test_fa_id_returns_only_that_advisors_client_funds(seeded_book) -> None:
    assert set(_ids({})) == set(BOOK) - {OTHER_ADVISORS_CLIENT}
    assert _ids({"fa_id": OTHER_ADVISOR}) == [OTHER_ADVISORS_CLIENT]


def test_unassigned_lists_clients_with_a_blank_advisor_or_no_advisor_row(seeded_book) -> None:
    with SessionLocal() as session:
        session.execute(
            update(FaAssignment)
            .where(FaAssignment.client_id == UNSCORED_CLIENT)
            .values(fa_id=None, fa_name=None, source="unassigned")
        )
        session.execute(delete(FaAssignment).where(FaAssignment.client_id == NOT_TIERED_CLIENT))
        session.commit()

    response = client.get(ROSTER, params={"unassigned": True, "limit": 200})

    assert response.status_code == 200
    listed = {row["client_id"] for row in response.json()["items"]}
    assert {UNSCORED_CLIENT, NOT_TIERED_CLIENT} <= listed
    assert not listed & {CRITICAL_HIGH_SCORE_CLIENT, OTHER_ADVISORS_CLIENT}


def test_worklist_only_leaves_out_held_out_and_untiered_clients(seeded_book) -> None:
    assert not {HELD_OUT_CLIENT, NOT_TIERED_CLIENT} & set(_ids({"worklist_only": True}))
    assert {HELD_OUT_CLIENT, NOT_TIERED_CLIENT} <= set(_ids({}))


def test_sorting_by_risk_score_puts_a_small_balance_with_a_high_score_first(seeded_book) -> None:
    ids = _ids({"worklist_only": True, "sort": "risk_score"})

    assert ids == WORKLIST_BY_SCORE
    assert ids.index(CRITICAL_HIGH_SCORE_CLIENT) < ids.index(LOW_SCORE_HUGE_MONEY_CLIENT)


def test_a_score_tie_is_broken_by_the_bigger_amount_at_risk(seeded_book) -> None:
    ids = _ids({"worklist_only": True, "sort": "risk_score"})

    assert ids.index(HIGH_SCORE_BIG_MONEY_CLIENT) < ids.index(HIGH_SCORE_SMALL_MONEY_CLIENT)


def test_sorting_by_fund_at_risk_puts_the_biggest_amount_first(seeded_book) -> None:
    assert _ids({"worklist_only": True, "sort": "fund_at_risk"}) == WORKLIST_BY_FUND_AT_RISK


def test_an_already_actioned_client_drops_to_the_bottom(seeded_book) -> None:
    _log_handled_call(CRITICAL_HIGH_SCORE_CLIENT, band_when_logged="Critical")

    ids = _ids({"worklist_only": True, "sort": "risk_score"})

    assert ids[-1] == CRITICAL_HIGH_SCORE_CLIENT
    assert ids[:-1] == WORKLIST_BY_SCORE[1:]


def test_a_client_whose_band_rose_since_the_call_comes_back_up(seeded_book) -> None:
    _log_handled_call(HIGH_SCORE_BIG_MONEY_CLIENT, band_when_logged="Watch")
    _log_handled_call(HIGH_SCORE_SMALL_MONEY_CLIENT, band_when_logged="High")

    ids = _ids({"worklist_only": True, "sort": "risk_score"})

    assert ids == [
        CRITICAL_HIGH_SCORE_CLIENT,
        HIGH_SCORE_BIG_MONEY_CLIENT,
        LOW_SCORE_HUGE_MONEY_CLIENT,
        UNSCORED_CLIENT,
        HIGH_SCORE_SMALL_MONEY_CLIENT,
    ]


@pytest.mark.parametrize("sort", ["risk_score", "fund_at_risk"])
def test_paging_walks_every_client_once_in_order(seeded_book, sort) -> None:
    _log_handled_call(HIGH_SCORE_SMALL_MONEY_CLIENT, band_when_logged="High")
    expected = _ids({"worklist_only": True, "sort": sort})
    seen: list[int] = []
    cursor = None
    for _ in range(10):
        params = {
            "fa_id": ADVISOR,
            "worklist_only": True,
            "sort": sort,
            "limit": 2,
            **({"cursor": cursor} if cursor else {}),
        }
        body = client.get(ROSTER, params=params).json()
        seen += [row["client_id"] for row in body["items"]]
        cursor = body["next_cursor"]
        if cursor is None:
            break

    assert seen == expected
    assert len(seen) == len(set(seen)) == 5


def test_a_cursor_from_the_other_kind_of_sort_is_a_400(seeded_book) -> None:
    by_client_id = client.get(ROSTER, params={"fa_id": ADVISOR, "limit": 1}).json()
    by_score = client.get(
        ROSTER, params={"fa_id": ADVISOR, "sort": "risk_score", "limit": 1}
    ).json()

    assert (
        client.get(
            ROSTER, params={"sort": "risk_score", "cursor": by_client_id["next_cursor"]}
        ).status_code
        == 400
    )
    assert client.get(ROSTER, params={"cursor": by_score["next_cursor"]}).status_code == 400


def test_an_unknown_sort_is_rejected(seeded_book) -> None:
    assert client.get(ROSTER, params={"sort": "balance"}).status_code == 422
