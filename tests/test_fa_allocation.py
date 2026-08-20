"""Tests for the advisor allocation rules.

Pure function, no database: ownership sticks across runs, an unassigned
client goes to the lightest-loaded advisor, an orphan is re-homed once, and
tonight's overflow is lent without moving who owns whom.
"""

from __future__ import annotations

from app.config import FaRecord
from app.risk.fa_allocation import ClientLoad, allocate_advisors

ROSTER = (
    FaRecord(fa_id=1, name="FA One", email="fa1@example.com", daily_capacity=2),
    FaRecord(fa_id=2, name="FA Two", email="fa2@example.com", daily_capacity=2),
)


def _clients(*specs: tuple[int, float, bool]) -> list[ClientLoad]:
    return [
        ClientLoad(client_id=cid, fund_at_risk=value, in_call_queue=queued)
        for cid, value, queued in specs
    ]


def test_empty_roster_assigns_nobody():
    result = allocate_advisors([], _clients((1, 100.0, True)), {})
    assert result.owner == {}
    assert result.covering == {}


def test_existing_owner_is_kept():
    clients = _clients((1, 500.0, False), (2, 10.0, False))
    result = allocate_advisors(ROSTER, clients, {1: 2, 2: 2})
    assert result.owner == {1: 2, 2: 2}


def test_unassigned_client_goes_to_the_lightest_advisor():
    clients = _clients((1, 900.0, False), (2, 50.0, False))
    result = allocate_advisors(ROSTER, clients, {1: 1})
    assert result.owner[1] == 1
    assert result.owner[2] == 2


def test_headcount_breaks_a_money_tie():
    roster = (
        *ROSTER,
        FaRecord(fa_id=3, name="FA Three", email="fa3@example.com", daily_capacity=2),
    )
    clients = _clients((1, 0.0, False), (2, 0.0, False), (3, 0.0, False))
    result = allocate_advisors(roster, clients, {})
    assert sorted(result.owner.values()) == [1, 2, 3]


def test_orphaned_client_is_rehomed_and_then_stays():
    clients = _clients((1, 100.0, False))
    first = allocate_advisors(ROSTER, clients, {1: 99})
    assert first.owner[1] in {1, 2}
    second = allocate_advisors(ROSTER, clients, first.owner)
    assert second.owner == first.owner


def test_allocation_is_stable_across_two_runs():
    clients = _clients((1, 800.0, True), (2, 400.0, True), (3, 100.0, False))
    first = allocate_advisors(ROSTER, clients, {})
    second = allocate_advisors(ROSTER, clients, first.owner)
    assert second.owner == first.owner


def test_overflow_is_lent_without_moving_ownership():
    clients = _clients((1, 400.0, True), (2, 300.0, True), (3, 200.0, True))
    owners = {1: 1, 2: 1, 3: 1}
    result = allocate_advisors(ROSTER, clients, owners)

    assert result.owner == owners
    assert result.covering == {3: 2}
    assert result.demoted == set()
    assert result.advisor_for(3) == 2
    assert result.advisor_for(1) == 1

    # Tomorrow, with the same data, the loan has not become a move.
    again = allocate_advisors(ROSTER, clients, result.owner)
    assert again.owner == owners


def test_overflow_is_demoted_when_nobody_has_room():
    roster = (FaRecord(fa_id=1, name="FA One", email="fa1@example.com", daily_capacity=1),)
    clients = _clients((1, 400.0, True), (2, 300.0, True))
    result = allocate_advisors(roster, clients, {1: 1, 2: 1})
    assert result.covering == {}
    assert result.demoted == {2}
    # Ownership never moves for a demoted client.
    assert result.owner == {1: 1, 2: 1}


def test_only_the_overflow_beyond_capacity_is_demoted():
    roster = (FaRecord(fa_id=1, name="FA One", email="fa1@example.com", daily_capacity=2),)
    clients = _clients((1, 900.0, True), (2, 500.0, True), (3, 100.0, True))
    result = allocate_advisors(roster, clients, {1: 1, 2: 1, 3: 1})
    assert result.demoted == {3}


def test_only_call_queue_lines_count_against_capacity():
    clients = _clients((1, 400.0, True), (2, 300.0, False), (3, 200.0, False))
    result = allocate_advisors(ROSTER, clients, {1: 1, 2: 1, 3: 1})
    assert result.covering == {}


def test_the_advisor_keeps_their_biggest_clients():
    roster = (
        FaRecord(fa_id=1, name="FA One", email="fa1@example.com", daily_capacity=1),
        FaRecord(fa_id=2, name="FA Two", email="fa2@example.com", daily_capacity=3),
    )
    clients = _clients((1, 900.0, True), (2, 100.0, True))
    result = allocate_advisors(roster, clients, {1: 1, 2: 1})
    assert result.covering == {2: 2}
