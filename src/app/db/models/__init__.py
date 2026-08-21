"""SQLAlchemy models, one module per domain area.

Importing this package registers every model on ``Base.metadata`` so migrations
and ``create_all`` see them.
"""

from app.db.models.active_clients import (
    INTERACTION_TYPES,
    ActiveClientFund,
    ActiveClientInteraction,
    ActiveTransaction,
)
from app.db.models.api import IdempotencyKey
from app.db.models.audit import AuditLog
from app.db.models.auth import REVIEWER_ROLES, ReviewerUser
from app.db.models.briefing import BriefingNarrative
from app.db.models.campaigns import (
    CONTACT_EVENT_TYPES,
    ENROLLMENT_STATUSES,
    CampaignStep,
    ContactEvent,
    Enrollment,
    TouchLog,
)
from app.db.models.complaints import (
    COMPLAINT_CATEGORIES,
    COMPLAINT_CHANNELS,
    COMPLAINT_STATUSES,
    ClientComplaint,
)
from app.db.models.digest import DigestEmailSend, DigestLine, DigestRun
from app.db.models.fa_assignment import FaAssignment
from app.db.models.generation_batch import (
    GENERATION_BATCH_ITEM_STATUSES,
    GENERATION_BATCH_STATUSES,
    GenerationBatch,
    GenerationBatchItem,
)
from app.db.models.generation_cost import GenerationCostConfigVersion
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
    COHORT_STATUSES,
    MESSAGE_STATUSES,
    REVIEW_OUTCOMES,
    Campaign,
    OutreachMessage,
    ReviewAction,
    ReviewCohort,
)
from app.db.models.rag import RagChunk, RagDocument, RagDocumentVersion
from app.db.models.risk import ClientRiskFeatures, RiskConfigVersion, RiskRun, RiskSnapshot
from app.db.models.rules import (
    BusinessRule,
    ClientMessageIndicators,
    MessageAngleCatalog,
    TierContract,
)
from app.db.models.suppression import Suppression
from app.db.models.template_generation_plan import TemplateGenerationPlan
from app.db.models.template_policy import CampaignTemplatePolicy, TemplatePolicyConfigVersion

__all__ = [
    "CAMPAIGN_STATUSES",
    "COHORT_STATUSES",
    "COMPLAINT_CATEGORIES",
    "COMPLAINT_CHANNELS",
    "COMPLAINT_STATUSES",
    "CONTACT_EVENT_TYPES",
    "ENROLLMENT_STATUSES",
    "GENERATION_BATCH_ITEM_STATUSES",
    "GENERATION_BATCH_STATUSES",
    "INGESTION_STATES",
    "INTERACTION_TYPES",
    "MESSAGE_STATUSES",
    "MESSAGE_TEMPLATE_STATUSES",
    "REVIEWER_ROLES",
    "REVIEW_OUTCOMES",
    "TEMPLATE_REVIEW_OUTCOMES",
    "ActiveClientFund",
    "ActiveClientInteraction",
    "ActiveTransaction",
    "AuditLog",
    "BriefingNarrative",
    "BusinessRule",
    "Campaign",
    "CampaignStep",
    "CampaignTemplatePolicy",
    "ClientComplaint",
    "ClientFeatures",
    "ClientFund",
    "ClientMessageIndicators",
    "ClientRiskFeatures",
    "Clients",
    "ContactEvent",
    "DigestEmailSend",
    "DigestLine",
    "DigestRun",
    "Enrollment",
    "FaAssignment",
    "Funds",
    "GenerationBatch",
    "GenerationBatchItem",
    "GenerationCostConfigVersion",
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
    "ReviewCohort",
    "ReviewerUser",
    "RiskConfigVersion",
    "RiskRun",
    "RiskSnapshot",
    "Suppression",
    "TemplateGenerationPlan",
    "TemplatePolicyConfigVersion",
    "TemplateReviewAction",
    "TierContract",
    "TouchLog",
    "Transactions",
]
