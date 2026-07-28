"""SQLAlchemy models, one module per domain area.

Importing this package registers every model on ``Base.metadata`` so migrations
and ``create_all`` see them.
"""

from app.db.models.audit import AuditLog
from app.db.models.llmops import GenerationRun, ModelVersion, PromptVersion
from app.db.models.models import (
    INGESTION_STATES,
    ClientFeatures,
    Clients,
    Funds,
    IngestionReject,
    IngestionStatus,
    PiiVault,
    RawStaging,
    Transactions,
)
from app.db.models.rag import RagChunk, RagDocument, RagDocumentVersion
from app.db.models.rules import BusinessRule, ClientMessageIndicators

__all__ = [
    "INGESTION_STATES",
    "AuditLog",
    "BusinessRule",
    "ClientFeatures",
    "ClientMessageIndicators",
    "Clients",
    "Funds",
    "GenerationRun",
    "IngestionReject",
    "IngestionStatus",
    "ModelVersion",
    "PiiVault",
    "PromptVersion",
    "RagChunk",
    "RagDocument",
    "RagDocumentVersion",
    "RawStaging",
    "Transactions",
]
