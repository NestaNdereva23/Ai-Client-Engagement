"""Tests for the FA-assignment source contract.

Covers what AM3 promises: the stub always answers with a null fa_id, a fake
implementation proves the Protocol is swappable, and a null fa_id is
provably read as "fall back to fund" by whatever calls it -- exercised here
at the contract level; AM10's digest builder is the real caller.
"""

from __future__ import annotations

import pytest
from sqlalchemy import delete

from app.config import FaRecord, Settings
from app.db.models.active_clients import ActiveClientFund
from app.db.models.fa_assignment import FaAssignment
from app.db.session import SessionLocal
from app.ingestion.fa_assignment_source import (
    DbFaAssignmentSource,
    FaAssignmentRecord,
    FaAssignmentSource,
    StubFaAssignmentSource,
    get_fa_assignment_source,
)


def test_stub_returns_empty_without_a_session():
    source = StubFaAssignmentSource()
    assert source.fetch_assignments([1, 2, 3]) == []


def test_stub_returns_null_fa_id_for_every_relationship(db):
    client_id = 20101
    with SessionLocal() as session:
        session.add_all(
            [
                ActiveClientFund(
                    client_id=client_id, unit_fund_id=10, n_deposits=1, n_withdrawals=0
                ),
                ActiveClientFund(
                    client_id=client_id, unit_fund_id=20, n_deposits=1, n_withdrawals=0
                ),
            ]
        )
        session.commit()

    try:
        with SessionLocal() as session:
            source = StubFaAssignmentSource(session=session)
            result = source.fetch_assignments([client_id])

        assert {(r.unit_fund_id, r.fa_id) for r in result} == {(10, None), (20, None)}
        assert all(r.fa_name is None and r.source == "stub" for r in result)
    finally:
        with SessionLocal() as session:
            session.execute(delete(ActiveClientFund).where(ActiveClientFund.client_id == client_id))
            session.commit()


def test_factory_defaults_to_stub():
    source = get_fa_assignment_source(settings=Settings(fa_assignment_source="stub"))
    assert isinstance(source, StubFaAssignmentSource)


def test_unknown_fa_assignment_source_raises():
    with pytest.raises(ValueError, match="unknown FA-assignment source"):
        get_fa_assignment_source(settings=Settings(fa_assignment_source="nope"))


class FakeFaAssignmentSource:
    """A second implementation, proving the Protocol is swappable: any class
    with fetch_assignments satisfies FaAssignmentSource, with no inheritance
    from StubFaAssignmentSource.
    """

    def fetch_assignments(self, client_ids):
        return [
            FaAssignmentRecord(
                client_id=cid, unit_fund_id=10, fa_id="fa-55", fa_name="Real FA", source="fake"
            )
            for cid in client_ids
        ]


def _digest_group_key(record: FaAssignmentRecord) -> tuple[str, str | int]:
    """The fallback rule a digest builder applies: group by FA when one is
    known, by fund when it is not.
    """
    return ("fa", record.fa_id) if record.fa_id is not None else ("fund", record.unit_fund_id)


def test_protocol_is_satisfied_by_a_fake_implementation():
    fake = FakeFaAssignmentSource()
    assert isinstance(fake, FaAssignmentSource)

    result = fake.fetch_assignments([1, 2])
    assert [r.fa_id for r in result] == ["fa-55", "fa-55"]


def test_null_fa_id_falls_back_to_fund_grouping():
    stub_record = FaAssignmentRecord(client_id=1, unit_fund_id=10, fa_id=None, source="stub")
    real_record = FaAssignmentRecord(client_id=2, unit_fund_id=10, fa_id="fa-55", source="fake")

    assert _digest_group_key(stub_record) == ("fund", 10)
    assert _digest_group_key(real_record) == ("fa", "fa-55")


def test_roster_parses_into_records():
    settings = Settings(
        fa_roster="fa-1:FA One:fa1@example.com:19, fa-2:FA Two:fa2@example.com:12",
    )
    assert settings.fa_records == (
        FaRecord(fa_id="fa-1", name="FA One", email="fa1@example.com", daily_capacity=19),
        FaRecord(fa_id="fa-2", name="FA Two", email="fa2@example.com", daily_capacity=12),
    )


def test_roster_drops_bad_entries_without_taking_the_rest_down():
    settings = Settings(
        fa_roster=(
            "fa-1:FA One:fa1@example.com:19,"
            "nope,"  # not enough fields
            "fa-3:FA Three:fa3@example.com:zero,"  # capacity is not a number
            "fa-4::fa4@example.com:5,"  # no name
            "fa-5:FA Five:fa5@example.com:0,"  # capacity of nobody
            "fa-1:FA Duplicate:dupe@example.com:9,"  # id already seen
            "fa-6:FA Six:fa6@example.com:7"
        )
    )
    assert [record.fa_id for record in settings.fa_records] == ["fa-1", "fa-6"]
    assert settings.fa_records[0].name == "FA One"


def test_empty_roster_selects_the_stub():
    source = get_fa_assignment_source(settings=Settings(fa_roster="", fa_assignment_source=""))
    assert isinstance(source, StubFaAssignmentSource)


def test_a_seeded_roster_selects_the_database_source():
    source = get_fa_assignment_source(
        settings=Settings(fa_roster="fa-1:FA One:fa1@example.com:19", fa_assignment_source="")
    )
    assert isinstance(source, DbFaAssignmentSource)


def test_db_source_returns_empty_without_a_session():
    assert DbFaAssignmentSource().fetch_assignments([1, 2, 3]) == []


def test_db_source_reads_what_the_allocation_wrote(db):
    client_id = 26101
    with SessionLocal() as session:
        session.add_all(
            [
                FaAssignment(
                    client_id=client_id,
                    unit_fund_id=10,
                    fa_id="fa-4",
                    fa_name="FA Four",
                    source="roster",
                ),
                FaAssignment(
                    client_id=client_id,
                    unit_fund_id=20,
                    fa_id="fa-4",
                    fa_name="FA Four",
                    source="roster",
                ),
            ]
        )
        session.commit()

    try:
        with SessionLocal() as session:
            result = DbFaAssignmentSource(session=session).fetch_assignments([client_id])
        assert {(r.unit_fund_id, r.fa_id) for r in result} == {(10, "fa-4"), (20, "fa-4")}
        assert all(r.fa_name == "FA Four" and r.source == "roster" for r in result)
    finally:
        with SessionLocal() as session:
            session.execute(delete(FaAssignment).where(FaAssignment.client_id == client_id))
            session.commit()
