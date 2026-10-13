from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select

from app.db.models.audit import AuditLog
from app.db.models.routing import ClientTier
from app.db.session import SessionLocal
from app.routing.tiers import (
    BELOW_CAPITAL_PRESERVATION_THRESHOLD,
    NO_TRANSACTIONS_IN_REVIEW_PERIOD,
    ClientActivity,
    TierThresholds,
    classify_client_tier,
    combine_fund_rows_into_client_activity,
    write_client_tiers,
)
from app.transform.active_features import SYSTEM_FEE_MAX, derive_active_measures
from app.transform.active_flatten import ActiveClientRow, ActiveFlattenResult, ActiveTxnRow

NAIROBI = timezone(timedelta(hours=3))
REFERENCE_MOMENT = datetime(2026, 10, 9, 9, 0, tzinfo=NAIROBI)
REFERENCE_DATE = REFERENCE_MOMENT.date()
TEST_THRESHOLDS = TierThresholds(
    version=1,
    review_period_days=365,
    recent_purchase_days=90,
    capital_preservation_threshold_kes=6_000.0,
)

TEST_CLIENT_IDS = list(range(925000, 925010))


def _date_days_before_reference(days: int) -> date:
    return REFERENCE_DATE - timedelta(days=days)


def _build_transaction(
    client_id: int, fund_id: int, kind: str, day: date, amount_kes: float
) -> ActiveTxnRow:
    return ActiveTxnRow(
        txn_id=None,
        txn_type=kind,
        client_id=client_id,
        unit_fund_id=fund_id,
        fund_short_name=None,
        date=day,
        amount=amount_kes,
        unit_price=None,
        fees_incurred=None,
        sale_type=None,
    )


def _build_client_book(
    client_id: int,
    balance_and_deposit_age_by_fund: dict[int, tuple[float, int | None]],
    withdrawals_as_age_and_amount: list[tuple[int, float]] | None = None,
) -> ActiveFlattenResult:
    withdrawals_as_age_and_amount = withdrawals_as_age_and_amount or []
    book = ActiveFlattenResult()
    for fund_id, (balance_kes, deposit_age_days) in balance_and_deposit_age_by_fund.items():
        last_deposit_date = (
            None if deposit_age_days is None else _date_days_before_reference(deposit_age_days)
        )
        book.clients.append(
            ActiveClientRow(
                client_id=client_id,
                client_code=None,
                client_name=None,
                client_email=None,
                client_phone=None,
                unit_fund_id=fund_id,
                balance=balance_kes,
                n_deposits=0 if last_deposit_date is None else 1,
                n_withdrawals=len(withdrawals_as_age_and_amount),
                last_deposit_date=last_deposit_date,
                last_withdrawal_slot_date=None,
                deposit_count_capped=False,
                withdrawal_history_hidden=False,
                computed_at=None,
            )
        )
        if last_deposit_date is not None:
            book.transactions.append(
                _build_transaction(client_id, fund_id, "purchase", last_deposit_date, 5_000.0)
            )
    first_fund_id = next(iter(balance_and_deposit_age_by_fund))
    for age_days, amount_kes in withdrawals_as_age_and_amount:
        book.transactions.append(
            _build_transaction(
                client_id, first_fund_id, "sale", _date_days_before_reference(age_days), amount_kes
            )
        )
    return book


def _fund_measures_for(book: ActiveFlattenResult):
    return derive_active_measures(
        book, reference_date=REFERENCE_MOMENT, system_fee_max=SYSTEM_FEE_MAX
    )


def _tier_for_single_client_book(book: ActiveFlattenResult) -> str:
    activity = next(
        iter(
            combine_fund_rows_into_client_activity(
                _fund_measures_for(book).values(), REFERENCE_DATE
            ).values()
        )
    )
    return classify_client_tier(activity, TEST_THRESHOLDS).tier


@pytest.mark.parametrize(
    ("deposit_age_days", "withdrawals_as_age_and_amount", "expected_tier"),
    [
        (200, [], "gradual_depositor"),
        (200, [(100, 5_000.0)], "gradual_depositor_withdrawer"),
        (30, [], "frequent_depositor"),
        (30, [(40, 5_000.0)], "frequent_depositor_withdrawer"),
    ],
)
def test_the_four_tier_cells(
    deposit_age_days, withdrawals_as_age_and_amount, expected_tier
) -> None:
    book = _build_client_book(1, {10: (50_000.0, deposit_age_days)}, withdrawals_as_age_and_amount)
    assert _tier_for_single_client_book(book) == expected_tier


def test_a_deposit_exactly_on_the_recent_purchase_line_counts_as_recent() -> None:
    on_the_line = _build_client_book(1, {10: (50_000.0, 90)})
    one_day_past = _build_client_book(1, {10: (50_000.0, 91)})
    assert _tier_for_single_client_book(on_the_line) == "frequent_depositor"
    assert _tier_for_single_client_book(one_day_past) == "gradual_depositor"


def test_a_fee_posting_is_not_a_withdrawal() -> None:
    book = _build_client_book(1, {10: (50_000.0, 30)}, withdrawals_as_age_and_amount=[(20, 50.0)])
    assert _tier_for_single_client_book(book) == "frequent_depositor"


def test_a_withdrawal_just_over_the_fee_is_a_real_withdrawal() -> None:
    book = _build_client_book(1, {10: (50_000.0, 30)}, withdrawals_as_age_and_amount=[(20, 51.0)])
    assert _tier_for_single_client_book(book) == "frequent_depositor_withdrawer"


def test_no_deposit_or_real_withdrawal_in_the_review_period_is_held_out() -> None:
    only_old_transactions = _build_client_book(
        1, {10: (50_000.0, 400)}, withdrawals_as_age_and_amount=[(400, 5_000.0)]
    )
    only_a_fee_posting = _build_client_book(
        1, {10: (50_000.0, None)}, withdrawals_as_age_and_amount=[(20, 50.0)]
    )
    assert _tier_for_single_client_book(only_old_transactions) == NO_TRANSACTIONS_IN_REVIEW_PERIOD
    assert _tier_for_single_client_book(only_a_fee_posting) == NO_TRANSACTIONS_IN_REVIEW_PERIOD


def test_a_balance_under_the_capital_preservation_threshold_is_held_out_even_when_active() -> None:
    just_under = _build_client_book(1, {10: (5_999.0, 5)})
    exactly_on = _build_client_book(1, {10: (6_000.0, 5)})
    assert _tier_for_single_client_book(just_under) == BELOW_CAPITAL_PRESERVATION_THRESHOLD
    assert _tier_for_single_client_book(exactly_on) == "frequent_depositor"


def test_held_out_clients_have_no_segment() -> None:
    activity = ClientActivity(
        client_id=1,
        total_balance_kes=100.0,
        days_since_newest_deposit=5,
        days_since_newest_real_withdrawal=None,
    )
    assert classify_client_tier(activity, TEST_THRESHOLDS).segment is None


def _delete_test_tier_rows(session) -> None:
    session.execute(delete(ClientTier).where(ClientTier.client_id.in_(TEST_CLIENT_IDS)))
    session.commit()


def test_a_client_with_two_funds_gets_one_row_from_the_newest_deposit(db) -> None:
    book = _build_client_book(925000, {10: (3_000.0, 200), 11: (3_500.0, 20)})
    run_id = uuid4().hex
    with SessionLocal() as session:
        _delete_test_tier_rows(session)
        write_client_tiers(
            session,
            _fund_measures_for(book).values(),
            reference_date=REFERENCE_DATE,
            run_id=run_id,
        )
        session.commit()

        rows = session.scalars(
            select(ClientTier).where(ClientTier.client_id.in_(TEST_CLIENT_IDS))
        ).all()
        assert [(row.client_id, row.tier, row.segment) for row in rows] == [
            (925000, "frequent_depositor", "active_active")
        ]
        assert rows[0].thresholds_version == 1
        _delete_test_tier_rows(session)


def test_the_old_tier_is_kept_only_when_the_tier_changes(db) -> None:
    slow_book = _build_client_book(925002, {10: (50_000.0, 200)})
    fast_book = _build_client_book(925002, {10: (50_000.0, 10)})
    with SessionLocal() as session:
        _delete_test_tier_rows(session)
        seen = []
        for book in (slow_book, fast_book, fast_book):
            write_client_tiers(
                session,
                _fund_measures_for(book).values(),
                reference_date=REFERENCE_DATE,
                run_id=uuid4().hex,
            )
            session.commit()
            session.expire_all()
            row = session.get(ClientTier, 925002)
            seen.append((row.tier, row.previous_tier, row.tier_changed_at))
        _delete_test_tier_rows(session)

    assert seen[0] == ("gradual_depositor", None, None)
    assert seen[1][:2] == ("frequent_depositor", "gradual_depositor")
    assert seen[1][2] is not None
    assert seen[2] == seen[1]


def test_a_second_write_updates_the_row_and_audits_both_runs(db) -> None:
    first_run_id, second_run_id = uuid4().hex, uuid4().hex
    book_before = _build_client_book(925001, {10: (50_000.0, 200)})
    book_after = _build_client_book(925001, {10: (50_000.0, 10)})
    with SessionLocal() as session:
        _delete_test_tier_rows(session)
        for run_id, book in ((first_run_id, book_before), (second_run_id, book_after)):
            write_client_tiers(
                session,
                _fund_measures_for(book).values(),
                reference_date=REFERENCE_DATE,
                run_id=run_id,
            )
            session.commit()

        row = session.get(ClientTier, 925001)
        assert (row.tier, row.run_id) == ("frequent_depositor", second_run_id)
        row_count = session.scalar(
            select(func.count()).select_from(ClientTier).where(ClientTier.client_id == 925001)
        )
        assert row_count == 1
        audit_entries = session.scalars(
            select(AuditLog).where(
                AuditLog.entity_type == "client_tier",
                AuditLog.run_id.in_([first_run_id, second_run_id]),
            )
        ).all()
        assert sorted(entry.run_id for entry in audit_entries) == sorted(
            [first_run_id, second_run_id]
        )
        assert audit_entries[0].detail["thresholds_version"] == 1
        _delete_test_tier_rows(session)
