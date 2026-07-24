"""The single code path that talks to the LLM, ensuring no personal data crosses the boundary."""

from app.privacy.boundary import (
    MODEL_ALLOWED_KEYS,
    AuditSink,
    BoundaryAudit,
    ModelCall,
    run_model_boundary,
    to_model_context,
)
from app.privacy.scanners import (
    BoundaryLeak,
    InboundLeak,
    OutboundLeak,
    scan_inbound,
    scan_outbound,
)

__all__ = [
    "MODEL_ALLOWED_KEYS",
    "AuditSink",
    "BoundaryAudit",
    "BoundaryLeak",
    "InboundLeak",
    "ModelCall",
    "OutboundLeak",
    "run_model_boundary",
    "scan_inbound",
    "scan_outbound",
    "to_model_context",
]
