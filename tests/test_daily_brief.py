from __future__ import annotations

from datetime import UTC, datetime, time, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.agents.situations import (
    FREQUENT_DEPOSITOR_ONE_FUND,
    GRADUAL_DEPOSITOR_DUE,
    GRADUAL_DEPOSITOR_WITHDRAWING,
)
from app.db.models.active_clients import (
    ActiveClientFund,
    ActiveClientTouchpoint,
    ActiveTransaction,
)
from app.db.models.fa_assignment import FaAssignment
from app.db.models.risk import ClientRiskFeatures
from app.db.models.routing import ClientSide, ClientTier
from app.db.models.signals import ClientSituationState, SignalRun
from app.db.session import SessionLocal
from app.main import app
from app.routing.tiers import SEGMENT_BY_TIER, TIER_GRADUAL_DEPOSITOR_WITHDRAWER
from app.services.client_touchpoints import today_in_nairobi
from app.services.daily_brief import build_daily_brief

client = TestClient(app)

FA = "brief-fa-1"
OTHER_FA = "brief-fa-2"
FUND = 9992
HEAVY_RECENT = 99921
SMALL_WITHDRAWER = 99922
QUIET = 99923
NO_GROUP = 99924
LEFT_THE_LINE = 99926
OTHER_ADVISORS = 99927
CLIENTS = [HEAVY_RECENT, SMALL_WITHDRAWER, QUIET, NO_GROUP, 99925, LEFT_THE_LINE, OTHER_ADVISORS]
TXN_BASE = 9_992_000_000


def _clear(session) -> None:
    for model in (
        ActiveTransaction,
        ActiveClientTouchpoint,
        ClientSituationState,
        ClientSide,
        ClientRiskFeatures,
        ClientTier,
        FaAssignment,
        ActiveClientFund,
    ):
        session.execute(delete(model).where(model.client_id.in_(CLIENTS)))
    session.execute(delete(SignalRun).where(SignalRun.run_id == RUN_ID))
    session.commit()


RUN_ID = "brief-test-run"


def _fund(client_id: int, **fields) -> ActiveClientFund:
    base = {
        "client_id": client_id,
        "unit_fund_id": FUND,
        "client_code": f"C{client_id}",
        "balance": 100_000.0,
        "n_deposits": 5,
        "n_withdrawals": 1,
    }
    return ActiveClientFund(**{**base, **fields})


def _risk(client_id: int, at_risk: float, heavy: bool) -> ClientRiskFeatures:
    return ClientRiskFeatures(
        client_id=client_id,
        unit_fund_id=FUND,
        balance_tier="Small",
        sig_heavy_withdrawal=heavy,
        sig_dormant=False,
        sig_broken_pattern=False,
        sig_shrinking=False,
        sig_going_dormant=False,
        sig_never_repeated=False,
        risk_score=50,
        risk_band="High",
        risk_reasons="test",
        fund_at_risk=at_risk,
        config_version=2,
        route="fa_action",
        queue_rank=None,
    )


def _tier(client_id: int) -> ClientTier:
    tier = TIER_GRADUAL_DEPOSITOR_WITHDRAWER
    return ClientTier(
        client_id=client_id,
        tier=tier,
        segment=SEGMENT_BY_TIER[tier],
        purchased_recently=False,
        real_withdrawer=False,
        computed_at=datetime.now(UTC),
        thresholds_version=1,
        run_id="test",
    )


def _situation(client_id: int, code: str, today) -> ClientSituationState:
    return ClientSituationState(
        client_id=client_id,
        unit_fund_id=FUND,
        situation_code=code,
        is_active=True,
        signal_codes=[],
        since=today,
        run_id=RUN_ID,
    )


@pytest.fixture
def advisor_book(db):
    today = today_in_nairobi()
    yesterday = today - timedelta(days=1)
    with SessionLocal() as session:
        _clear(session)
        session.add(SignalRun(run_id=RUN_ID, state="completed"))
        session.flush()
        session.add_all(
            [
                _fund(
                    HEAVY_RECENT,
                    balance=410_000.0,
                    largest_withdrawal=660_000.0,
                    last_withdrawal_date=yesterday,
                ),
                _fund(SMALL_WITHDRAWER, balance=900_000.0, largest_withdrawal=100.0),
                _fund(
                    QUIET,
                    balance=50_000.0,
                    last_deposit_date=today - timedelta(days=240),
                    typical_gap_days=30.0,
                ),
                _fund(NO_GROUP),
                _fund(99925),
                _fund(LEFT_THE_LINE, balance=4_000.0),
                _fund(OTHER_ADVISORS, balance=410_000.0),
            ]
        )
        session.flush()
        for client_id in (HEAVY_RECENT, SMALL_WITHDRAWER, QUIET, NO_GROUP, OTHER_ADVISORS):
            session.add(_tier(client_id))
        session.add_all(
            [
                _risk(HEAVY_RECENT, 50_000.0, True),
                _risk(SMALL_WITHDRAWER, 80_000.0, False),
                _risk(QUIET, 20_000.0, False),
                _risk(NO_GROUP, 10_000.0, False),
                _risk(OTHER_ADVISORS, 70_000.0, True),
            ]
        )
        for client_id in (HEAVY_RECENT, SMALL_WITHDRAWER, QUIET, NO_GROUP, 99925, LEFT_THE_LINE):
            session.add(
                FaAssignment(client_id=client_id, unit_fund_id=FUND, fa_id=FA, source="test")
            )
        session.add(
            FaAssignment(client_id=OTHER_ADVISORS, unit_fund_id=FUND, fa_id=OTHER_FA, source="test")
        )
        session.add_all(
            [
                _situation(HEAVY_RECENT, GRADUAL_DEPOSITOR_WITHDRAWING, today),
                _situation(SMALL_WITHDRAWER, GRADUAL_DEPOSITOR_WITHDRAWING, today),
                _situation(QUIET, GRADUAL_DEPOSITOR_DUE, today),
                _situation(OTHER_ADVISORS, GRADUAL_DEPOSITOR_WITHDRAWING, today),
                _situation(NO_GROUP, FREQUENT_DEPOSITOR_ONE_FUND, today),
            ]
        )
        session.add(
            ClientSide(
                client_id=LEFT_THE_LINE,
                side="inactive",
                balance=4_000.0,
                threshold_kes=6_000.0,
                source_feed="active",
                previous_side="active",
                side_changed_at=datetime.now(UTC),
            )
        )
        session.add_all(
            [
                ActiveTransaction(
                    txn_id=TXN_BASE + 1,
                    txn_type="sale",
                    client_id=HEAVY_RECENT,
                    unit_fund_id=FUND,
                    txn_date=yesterday,
                    amount=660_000.0,
                    sale_type="unit_sale",
                ),
                ActiveTransaction(
                    txn_id=TXN_BASE + 2,
                    txn_type="sale",
                    client_id=OTHER_ADVISORS,
                    unit_fund_id=FUND,
                    txn_date=yesterday,
                    amount=700_000.0,
                    sale_type="unit_sale",
                ),
                ActiveTransaction(
                    txn_id=TXN_BASE + 3,
                    txn_type="purchase",
                    client_id=NO_GROUP,
                    unit_fund_id=FUND,
                    txn_date=yesterday,
                    amount=25_000.0,
                ),
            ]
        )
        session.add_all(
            [
                ActiveClientTouchpoint(
                    client_id=QUIET,
                    unit_fund_id=FUND,
                    kind="call",
                    title="Check in",
                    due_date=today,
                    due_time=time(10, 30),
                    created_by="test",
                ),
                ActiveClientTouchpoint(
                    client_id=SMALL_WITHDRAWER,
                    unit_fund_id=FUND,
                    kind="reminder",
                    title="Follow up by whatsapp",
                    due_date=today,
                    created_by="test",
                ),
            ]
        )
        session.commit()
    yield today
    with SessionLocal() as session:
        _clear(session)


def test_the_brief_says_what_happened_and_orders_the_groups(advisor_book) -> None:
    with SessionLocal() as session:
        brief = build_daily_brief(session, FA, today=advisor_book)

    kinds = [headline.kind for headline in brief.headlines]
    assert kinds == ["large_withdrawals", "calendar", "below_line", "deposits"]
    withdrawals, calendar, below_line, deposits = brief.headlines
    assert (withdrawals.count, withdrawals.amount_kes) == (1, 660_000.0)
    assert withdrawals.lead == "1 client made a large withdrawal yesterday."
    assert calendar.text == "A call at 10:30 and a WhatsApp follow-up."
    assert below_line.count == 1
    assert (deposits.count, deposits.amount_kes) == (1, 25_000.0)

    assert [group.code for group in brief.groups] == ["withdrawing", "stopped", "second_product"]
    withdrawing = brief.groups[0]
    assert withdrawing.clients == 2
    assert [example.client_id for example in withdrawing.examples] == [
        HEAVY_RECENT,
        SMALL_WITHDRAWER,
    ]
    assert withdrawing.examples[0].reason.startswith("Took out KSh 660K, 62% of the balance")
    assert withdrawing.examples[1].reason == "Has taken out money while deposits slowed."
    assert withdrawing.sharp_fact == (
        "1 of them has taken out more than half of their balance in the last 30 days, "
        "KSh 660K in total. 1 of them did it yesterday."
    )
    assert withdrawing.warning_signs == "Most common warning signs: large withdrawal (1 of 2)."
    assert brief.groups[1].sharp_fact == "1 of them has not made a deposit for over six months."
    assert brief.rest_clients == 0
    assert brief.worklist_clients == 4


def test_the_brief_holds_only_this_advisors_clients(advisor_book) -> None:
    with SessionLocal() as session:
        brief = build_daily_brief(session, FA, today=advisor_book)
        other = build_daily_brief(session, OTHER_FA, today=advisor_book)

    shown = {example.client_id for group in brief.groups for example in group.examples}
    assert OTHER_ADVISORS not in shown
    assert [headline.kind for headline in other.headlines] == ["large_withdrawals"]
    assert other.headlines[0].amount_kes == 700_000.0


def test_the_table_lists_clients_in_the_order_of_the_brief_across_pages(
    advisor_book, reviewer_1_headers, configured_reviewers
) -> None:
    params = {"fa_id": FA, "worklist_only": True, "sort": "brief", "limit": 3}

    first = client.get("/api/v1/active-clients", params=params, headers=reviewer_1_headers).json()
    second = client.get(
        "/api/v1/active-clients",
        params={**params, "cursor": first["next_cursor"]},
        headers=reviewer_1_headers,
    ).json()

    rows = first["items"] + second["items"]
    assert [row["client_id"] for row in rows] == [
        HEAVY_RECENT,
        SMALL_WITHDRAWER,
        QUIET,
        NO_GROUP,
    ]
    assert [row["brief_group"] for row in rows] == [
        "withdrawing",
        "withdrawing",
        "stopped",
        "second_product",
    ]
    assert second["next_cursor"] is None
    assert rows[0]["next_action"]["title"] == "Review their cash needs"
    assert rows[0]["next_action"]["why"].startswith("Took out KSh 660K, 62% of the balance")
    assert rows[1]["next_action"] is None
    assert rows[1]["next_action_note"] == "Reminder planned for today"
    assert rows[2]["next_action_note"] == "Call planned for today"
    assert rows[3]["next_action"]["title"] == "Suggest a second fund"

    stopped = client.get(
        "/api/v1/active-clients",
        params={**params, "brief_group": "stopped"},
        headers=reviewer_1_headers,
    ).json()
    assert [row["client_id"] for row in stopped["items"]] == [QUIET]
    unknown = client.get(
        "/api/v1/active-clients", params={"brief_group": "nope"}, headers=reviewer_1_headers
    )
    assert unknown.status_code == 422


def test_the_brief_endpoint_returns_the_same_brief(
    advisor_book, reviewer_1_headers, configured_reviewers
) -> None:
    response = client.get(f"/api/v1/fa-action/{FA}/brief", headers=reviewer_1_headers)

    assert response.status_code == 200
    body = response.json()
    assert body["worklist_clients"] == 4
    assert [headline["kind"] for headline in body["headlines"]][:2] == [
        "large_withdrawals",
        "calendar",
    ]
    assert body["groups"][0]["code"] == "withdrawing"
    assert body["groups"][0]["examples"][0]["client_id"] == HEAVY_RECENT
    assert body["calendar"][0]["label"] == "Call"
