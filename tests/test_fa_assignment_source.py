"""Tests for the FA-assignment source contract.

Covers what AM3 promises: the stub always answers with a null fa_id, a fake
implementation proves the Protocol is swappable, and a null fa_id is
provably read as "fall back to fund" by whatever calls it -- exercised here
at the contract level; AM10's digest builder is the real caller.
"""

from __future__ import annotations

import pytest
from sqlalchemy import delete

from app.config import Settings
from app.db.models.active_clients import ActiveClientFund
from app.db.session import SessionLocal
from app.ingestion.fa_assignment_source import (
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
                ActiveClientFund(client_id=client_id, unit_fund_id=10, n_purchases=1, n_sales=0),
                ActiveClientFund(client_id=client_id, unit_fund_id=20, n_purchases=1, n_sales=0),
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
                client_id=cid, unit_fund_id=10, fa_id=55, fa_name="Real FA", source="fake"
            )
            for cid in client_ids
        ]


def _digest_group_key(record: FaAssignmentRecord) -> tuple[str, int]:
    """The fallback rule a digest builder applies: group by FA when one is
    known, by fund when it is not.
    """
    return ("fa", record.fa_id) if record.fa_id is not None else ("fund", record.unit_fund_id)


def test_protocol_is_satisfied_by_a_fake_implementation():
    fake = FakeFaAssignmentSource()
    assert isinstance(fake, FaAssignmentSource)

    result = fake.fetch_assignments([1, 2])
    assert [r.fa_id for r in result] == [55, 55]


def test_null_fa_id_falls_back_to_fund_grouping():
    stub_record = FaAssignmentRecord(client_id=1, unit_fund_id=10, fa_id=None, source="stub")
    real_record = FaAssignmentRecord(client_id=2, unit_fund_id=10, fa_id=55, source="fake")

    assert _digest_group_key(stub_record) == ("fund", 10)
    assert _digest_group_key(real_record) == ("fa", 55)
