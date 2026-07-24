"""The single code path to the model API.

Every model call goes through run_model_boundary: it takes only allow-listed,
bucketed context, scans it, calls the model, then scans the response. No other
module talks to the model client directly, and no name, code, exact amount, or
date is ever in the payload. Re-attaching real values happens afterwards,
outside this function.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from app.privacy.scanners import (
    MODEL_ALLOWED_KEYS,
    scan_inbound,
    scan_outbound,
)

# A model call takes an allow-listed payload and returns the drafted text.
ModelCall = Callable[[dict[str, Any]], str]


def to_model_context(row: Mapping[str, Any]) -> dict[str, Any]:
    """Project a safe row to the allow-list, dropping client_id and anything else."""
    return {key: row[key] for key in MODEL_ALLOWED_KEYS if key in row}


def run_model_boundary(context: Mapping[str, Any], model_call: ModelCall) -> str:
    """Send allow-listed context to the model and return the drafted text.

    Fails closed: an inbound hit aborts before the call, an outbound hit aborts
    after it, and nothing is returned in either case.
    """
    payload = dict(context)
    scan_inbound(payload)
    draft = model_call(payload)
    scan_outbound(draft)
    return draft
