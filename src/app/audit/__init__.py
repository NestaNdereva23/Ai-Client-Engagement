"""Append-only audit trail that records every action which writes data."""

from app.audit.boundary import audit_boundary_crossing
from app.audit.log import record_audit

__all__ = ["audit_boundary_crossing", "record_audit"]
