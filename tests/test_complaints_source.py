from __future__ import annotations

import json
from datetime import date

import pytest

from app.config import Settings
from app.ingestion.complaints_source import (
    ComplaintRecord,
    ComplaintsSource,
    StubComplaintsSource,
    get_complaints_source,
)


def test_stub_returns_empty_by_default():
    source = StubComplaintsSource()
    assert source.fetch_open_complaints([1, 2, 3]) == []


def test_stub_never_enabled_by_default_configuration():
    """The app's own factory never wires a fixture; only a test constructing
    StubComplaintsSource directly can do that.
    """
    source = get_complaints_source(Settings(complaints_source="stub"))
    assert isinstance(source, StubComplaintsSource)
    assert source.fetch_open_complaints([1]) == []


def test_fixture_seeded_stub_returns_expected_records(tmp_path):
    fixture = tmp_path / "complaints.json"
    fixture.write_text(
        json.dumps(
            [
                {
                    "client_id": 101,
                    "opened_at": "2026-07-01",
                    "closed_at": None,
                    "status": "open",
                    "category": "billing",
                    "channel": "call",
                    "source": "stub",
                },
                {
                    "client_id": 102,
                    "opened_at": "2026-06-01",
                    "closed_at": "2026-06-15",
                    "status": "closed",
                    "category": "service",
                    "channel": "email",
                    "source": "stub",
                },
            ]
        ),
        encoding="utf-8",
    )

    source = StubComplaintsSource(fixture_path=fixture)

    # Only the open complaint for a requested client comes back; the closed
    # one and complaints for clients not asked about are excluded.
    result = source.fetch_open_complaints([101, 102, 999])
    assert len(result) == 1
    assert result[0].client_id == 101
    assert result[0].category == "billing"


def test_unknown_client_id_returns_nothing(tmp_path):
    fixture = tmp_path / "complaints.json"
    fixture.write_text(
        json.dumps(
            [
                {
                    "client_id": 101,
                    "opened_at": "2026-07-01",
                    "closed_at": None,
                    "status": "open",
                    "category": "billing",
                    "channel": "call",
                    "source": "stub",
                }
            ]
        ),
        encoding="utf-8",
    )
    source = StubComplaintsSource(fixture_path=fixture)
    assert source.fetch_open_complaints([555]) == []


class FakeComplaintsSource:
    """A second implementation, proving the Protocol is swappable: any class
    with fetch_open_complaints satisfies ComplaintsSource, with no inheritance
    from StubComplaintsSource.
    """

    def fetch_open_complaints(self, client_ids):
        return [
            ComplaintRecord(
                client_id=cid,
                opened_at=date(2026, 1, 1),
                status="open",
                category="product",
                channel="branch",
                source="fake",
            )
            for cid in client_ids
        ]


def _call_downstream(source: ComplaintsSource, client_ids: list[int]) -> list[ComplaintRecord]:
    """Stands in for a caller (six-signal engine, router, briefing) that only
    knows the protocol, never a concrete implementation.
    """
    return source.fetch_open_complaints(client_ids)


def test_protocol_is_satisfied_by_a_fake_implementation():
    fake = FakeComplaintsSource()
    assert isinstance(fake, ComplaintsSource)

    result = _call_downstream(fake, [7, 8])
    assert [r.client_id for r in result] == [7, 8]
    assert all(r.status == "open" for r in result)


def test_unknown_complaints_source_raises():
    with pytest.raises(ValueError, match="unknown complaints source"):
        get_complaints_source(Settings(complaints_source="nope"))
