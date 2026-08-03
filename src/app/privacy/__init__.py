"""The single code path that talks to the LLM, ensuring no personal data crosses the boundary."""

from app.privacy.boundary import (
    MODEL_ALLOWED_KEYS,
    AuditSink,
    BoundaryAudit,
    ModelCall,
    run_model_boundary,
    to_model_context,
)
from app.privacy.fact_block import ModelFactBlock, round_sig_figs
from app.privacy.llm_client import (
    AnthropicLLMClient,
    LLMClient,
    LLMClientError,
    OllamaLLMClient,
    as_model_call,
    get_llm_client,
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
    "AnthropicLLMClient",
    "AuditSink",
    "BoundaryAudit",
    "BoundaryLeak",
    "InboundLeak",
    "LLMClient",
    "LLMClientError",
    "ModelCall",
    "ModelFactBlock",
    "OllamaLLMClient",
    "OutboundLeak",
    "as_model_call",
    "get_llm_client",
    "round_sig_figs",
    "run_model_boundary",
    "scan_inbound",
    "scan_outbound",
    "to_model_context",
]
