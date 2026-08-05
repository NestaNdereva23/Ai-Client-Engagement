"""Every model crossing is recorded once, and the trail is append-only.

The pure tests check the record the boundary builds via an injected sink,
including on a block. The database tests check the shared helper and the
boundary sink write a row that survives even when the crossing was blocked.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import delete, select, text, update

from app.audit.boundary import audit_boundary_crossing
from app.audit.log import record_audit
from app.db.models.audit import AuditLog
from app.db.session import SessionLocal
from app.privacy.boundary import BoundaryAudit, run_model_boundary
from app.privacy.scanners import InboundLeak, OutboundLeak

ALLOWLISTED = {
    "recency_band": "Over 6y",
    "value_band": "High",
    "cadence_band": "Regular",
    "hold_band": "Unknown",
}


def _capture() -> tuple[list[BoundaryAudit], object]:
    records: list[BoundaryAudit] = []
    return records, records.append


def test_a_passed_crossing_records_fields_and_verdicts() -> None:
    records, sink = _capture()
    run_model_boundary(
        ALLOWLISTED,
        lambda payload: "Dear {{first_name}}, come back.",
        run_id="run-1",
        trace_id="trace-1",
        audit=sink,
    )
    (record,) = records
    assert record.fields == sorted(ALLOWLISTED)
    assert (record.inbound, record.outbound) == ("pass", "pass")
    assert (record.run_id, record.trace_id) == ("run-1", "trace-1")
    assert record.reason is None


def test_an_inbound_block_is_still_audited() -> None:
    records, sink = _capture()
    with pytest.raises(InboundLeak):
        run_model_boundary({**ALLOWLISTED, "client_id": 1001}, lambda payload: "draft", audit=sink)
    (record,) = records
    assert record.inbound == "blocked"
    assert record.outbound == "skipped"
    assert record.reason is not None


def test_an_outbound_block_is_still_audited() -> None:
    records, sink = _capture()
    with pytest.raises(OutboundLeak):
        run_model_boundary(
            ALLOWLISTED,
            lambda payload: "Regards, Jane Doe",
            identifiers=["Jane Doe"],
            audit=sink,
        )
    (record,) = records
    assert record.inbound == "pass"
    assert record.outbound == "blocked"


@pytest.fixture
def audit_run_id(db: None):
    """A unique run id whose audit rows are removed after the test."""
    run_id = uuid4().hex
    yield run_id
    with SessionLocal() as session:
        session.execute(delete(AuditLog).where(AuditLog.run_id == run_id))
        session.commit()


def test_record_audit_inserts_a_row(audit_run_id: str) -> None:
    with SessionLocal() as session:
        record_audit(
            session,
            entity_type="test",
            action="noop",
            run_id=audit_run_id,
            trace_id="trace-x",
            detail={"k": "v"},
        )
        session.commit()

    with SessionLocal() as session:
        row = session.scalar(select(AuditLog).where(AuditLog.run_id == audit_run_id))
        assert row is not None
        assert row.entity_type == "test"
        assert row.trace_id == "trace-x"
        assert row.detail == {"k": "v"}
        assert row.created_at is not None


def test_boundary_sink_persists_fields_run_and_trace(audit_run_id: str) -> None:
    run_model_boundary(
        ALLOWLISTED,
        lambda payload: "Dear {{first_name}}, come back.",
        entity_id="1001",
        run_id=audit_run_id,
        trace_id="trace-join",
        audit=audit_boundary_crossing,
    )

    with SessionLocal() as session:
        row = session.scalar(select(AuditLog).where(AuditLog.run_id == audit_run_id))
    assert row is not None
    assert row.entity_type == "model_boundary"
    assert row.entity_id == "1001"
    assert row.trace_id == "trace-join"
    assert row.detail["fields"] == sorted(ALLOWLISTED)
    assert row.detail["inbound"] == "pass"
    assert row.detail["outbound"] == "pass"


def test_boundary_sink_persists_even_when_the_crossing_is_blocked(audit_run_id: str) -> None:
    with pytest.raises(InboundLeak):
        run_model_boundary(
            {**ALLOWLISTED, "client_id": 1001},
            lambda payload: "draft",
            run_id=audit_run_id,
            audit=audit_boundary_crossing,
        )

    with SessionLocal() as session:
        row = session.scalar(select(AuditLog).where(AuditLog.run_id == audit_run_id))
    assert row is not None
    assert row.detail["inbound"] == "blocked"
    assert row.detail["reason"] is not None


def _role_present(session, name: str) -> bool:
    return bool(session.scalar(text("SELECT 1 FROM pg_roles WHERE rolname = :n"), {"n": name}))


def test_audit_log_is_append_only_for_the_safe_role(audit_run_id: str) -> None:
    with SessionLocal() as session:
        if not _role_present(session, "ace_safe"):
            pytest.skip("boundary roles not present; run alembic upgrade head")

    with SessionLocal() as session:
        record_audit(session, entity_type="test", action="noop", run_id=audit_run_id)
        session.commit()

    with SessionLocal() as session:
        session.execute(text('SET ROLE "ace_safe"'))
        with pytest.raises(Exception, match="permission denied"):
            session.execute(update(AuditLog).values(action="tampered"))
        session.rollback()
        session.execute(text("RESET ROLE"))
