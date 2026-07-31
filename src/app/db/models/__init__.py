"""SQLAlchemy models, one module per domain area.

Importing this package registers every model on ``Base.metadata`` so migrations
and ``create_all`` see them.
"""

from app.db.models.api import IdempotencyKey
from app.db.models.audit import AuditLog
from app.db.models.campaigns import (
    CONTACT_EVENT_TYPES,
    ENROLLMENT_STATUSES,
    CampaignStep,
    ContactEvent,
    Enrollment,
    TouchLog,
)
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
from app.db.models.outreach import (
    CAMPAIGN_STATUSES,
    MESSAGE_STATUSES,
    REVIEW_OUTCOMES,
    Campaign,
    OutreachMessage,
    ReviewAction,
)
from app.db.models.rag import RagChunk, RagDocument, RagDocumentVersion
from app.db.models.rules import BusinessRule, ClientMessageIndicators
from app.db.models.suppression import Suppression

__all__ = [
    "CAMPAIGN_STATUSES",
    "CONTACT_EVENT_TYPES",
    "ENROLLMENT_STATUSES",
    "INGESTION_STATES",
    "MESSAGE_STATUSES",
    "REVIEW_OUTCOMES",
    "AuditLog",
    "BusinessRule",
    "Campaign",
    "CampaignStep",
    "ClientFeatures",
    "ClientMessageIndicators",
    "Clients",
    "ContactEvent",
    "Enrollment",
    "Funds",
    "GenerationRun",
    "IdempotencyKey",
    "IngestionReject",
    "IngestionStatus",
    "ModelVersion",
    "OutreachMessage",
    "PiiVault",
    "PromptVersion",
    "RagChunk",
    "RagDocument",
    "RagDocumentVersion",
    "RawStaging",
    "ReviewAction",
    "Suppression",
    "TouchLog",
    "Transactions",
]
