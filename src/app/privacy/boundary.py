"""The single code path to the model API.

Every model call goes through run_model_boundary: it takes only allow-listed,
bucketed context, scans it, calls the model, then scans the response. No other
module talks to the model client directly, and no name, code, exact amount, or
date is ever in the payload. Re-attaching real values happens afterwards,
outside this function.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from app.privacy.scanners import (
    MODEL_ALLOWED_KEYS,
    InboundLeak,
    OutboundLeak,
    scan_inbound,
    scan_outbound,
)

# A model call takes an allow-listed payload and returns the drafted text.
ModelCall = Callable[[dict[str, Any]], str]


@dataclass
class BoundaryAudit:
    """One model crossing: the fields sent, the scanner verdicts, and its refs."""

    fields: list[str]
    inbound: str = "skipped"  # pass | blocked | skipped
    outbound: str = "skipped"
    entity_id: str | None = None
    run_id: str | None = None
    trace_id: str | None = None
    reason: str | None = None


# An audit sink receives one record per crossing, whether it passed or blocked.
AuditSink = Callable[[BoundaryAudit], None]


def to_model_context(row: Mapping[str, Any]) -> dict[str, Any]:
    """Project a safe row to the allow-list, dropping client_id and anything else."""
    return {key: row[key] for key in MODEL_ALLOWED_KEYS if key in row}


def run_model_boundary(
    context: Mapping[str, Any],
    model_call: ModelCall,
    *,
    identifiers: Iterable[str] = (),
    entity_id: str | None = None,
    run_id: str | None = None,
    trace_id: str | None = None,
    audit: AuditSink | None = None,
) -> str:
    """Send allow-listed context to the model and return the drafted text.

    identifiers are the request's real client values; the scanners block them
    from the payload and from the draft. Fails closed: an inbound hit aborts
    before the call, an outbound hit aborts after it, returning nothing. The
    audit sink, when given, records one row per crossing including blocks.
    """
    payload = dict(context)
    identifiers = tuple(identifiers)
    record = BoundaryAudit(
        fields=sorted(payload), entity_id=entity_id, run_id=run_id, trace_id=trace_id
    )
    try:
        scan_inbound(payload, identifiers)
        record.inbound = "pass"
        draft = model_call(payload)
        scan_outbound(draft, identifiers)
        record.outbound = "pass"
        return draft
    except InboundLeak as leak:
        record.inbound = "blocked"
        record.reason = str(leak)
        raise
    except OutboundLeak as leak:
        record.outbound = "blocked"
        record.reason = str(leak)
        raise
    finally:
        if audit is not None:
            audit(record)
