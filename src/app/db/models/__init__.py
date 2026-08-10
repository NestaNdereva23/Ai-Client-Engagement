"""SQLAlchemy models, one module per domain area.

Importing this package registers every model on ``Base.metadata`` so migrations
and ``create_all`` see them.

risk, fa_assignment, complaints, and digest are scaffolded but still empty;
their imports land here once each ships its first model.
"""

from app.db.models.active_clients import ActiveClientFund
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
from app.db.models.generation_batch import (
    GENERATION_BATCH_ITEM_STATUSES,
    GENERATION_BATCH_STATUSES,
    GenerationBatch,
    GenerationBatchItem,
)
from app.db.models.llmops import GenerationRun, ModelVersion, PromptVersion
from app.db.models.message_template import (
    MESSAGE_TEMPLATE_STATUSES,
    TEMPLATE_REVIEW_OUTCOMES,
    MessageTemplate,
    TemplateReviewAction,
)
from app.db.models.models import (
    INGESTION_STATES,
    ClientFeatures,
    ClientFund,
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
from app.db.models.rules import (
    BusinessRule,
    ClientMessageIndicators,
    MessageAngleCatalog,
    TierContract,
)
from app.db.models.suppression import Suppression

__all__ = [
    "CAMPAIGN_STATUSES",
    "CONTACT_EVENT_TYPES",
    "ENROLLMENT_STATUSES",
    "GENERATION_BATCH_ITEM_STATUSES",
    "GENERATION_BATCH_STATUSES",
    "INGESTION_STATES",
    "MESSAGE_STATUSES",
    "MESSAGE_TEMPLATE_STATUSES",
    "REVIEW_OUTCOMES",
    "TEMPLATE_REVIEW_OUTCOMES",
    "ActiveClientFund",
    "AuditLog",
    "BusinessRule",
    "Campaign",
    "CampaignStep",
    "ClientFeatures",
    "ClientFund",
    "ClientMessageIndicators",
    "Clients",
    "ContactEvent",
    "Enrollment",
    "Funds",
    "GenerationBatch",
    "GenerationBatchItem",
    "GenerationRun",
    "IdempotencyKey",
    "IngestionReject",
    "IngestionStatus",
    "MessageAngleCatalog",
    "MessageTemplate",
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
    "TemplateReviewAction",
    "TierContract",
    "TouchLog",
    "Transactions",
]
