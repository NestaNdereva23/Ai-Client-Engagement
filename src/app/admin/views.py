from __future__ import annotations

from sqladmin import ModelView

from app.db.models.active_clients import (
    ActiveClientFund,
    ActiveClientInteraction,
    ActiveTransaction,
)
from app.db.models.api import IdempotencyKey
from app.db.models.audit import AuditLog
from app.db.models.auth import ReviewerUser
from app.db.models.briefing import BriefingNarrative
from app.db.models.campaigns import CampaignStep, ContactEvent, Enrollment, TouchLog
from app.db.models.complaints import ClientComplaint
from app.db.models.digest import DigestEmailSend, DigestLine, DigestRun
from app.db.models.fa_assignment import FaAssignment
from app.db.models.generation_batch import GenerationBatch, GenerationBatchItem
from app.db.models.generation_cost import GenerationCostConfigVersion
from app.db.models.instantiation_batch import InstantiationBatch
from app.db.models.llmops import (
    Evaluation,
    GenerationRun,
    LLMRequest,
    LLMResponse,
    ModelVersion,
    PromptVersion,
    RubricVersion,
    TokenUsage,
    ToolCall,
    TraceRef,
)
from app.db.models.message_template import MessageTemplate, TemplateReviewAction
from app.db.models.models import (
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
from app.db.models.outreach import Campaign, OutreachMessage, ReviewAction, ReviewCohort
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


class _ReadOnlyView(ModelView):
    can_create = False
    can_edit = False
    can_delete = False
    can_export = False
    column_list = "__all__"


class RawStagingAdmin(_ReadOnlyView, model=RawStaging):
    name = "Raw Staging"
    name_plural = "Raw Staging"
    icon = "fa-solid fa-inbox"
    category = "Ingestion"
    category_icon = "fa-solid fa-download"


class IngestionStatusAdmin(_ReadOnlyView, model=IngestionStatus):
    name = "Ingestion Status"
    name_plural = "Ingestion Status"
    icon = "fa-solid fa-list-check"
    category = "Ingestion"
    category_icon = "fa-solid fa-download"


class IngestionRejectAdmin(_ReadOnlyView, model=IngestionReject):
    name = "Ingestion Reject"
    name_plural = "Ingestion Rejects"
    icon = "fa-solid fa-triangle-exclamation"
    category = "Ingestion"
    category_icon = "fa-solid fa-download"
    column_default_sort = [(IngestionReject.created_at, True)]


class FundsAdmin(_ReadOnlyView, model=Funds):
    name = "Fund"
    name_plural = "Funds"
    icon = "fa-solid fa-building-columns"
    category = "Clients & Funds"
    category_icon = "fa-solid fa-users"


class ClientsAdmin(_ReadOnlyView, model=Clients):
    name = "Client"
    name_plural = "Clients"
    icon = "fa-solid fa-user"
    category = "Clients & Funds"
    category_icon = "fa-solid fa-users"


class ClientFundAdmin(_ReadOnlyView, model=ClientFund):
    name = "Client Fund"
    name_plural = "Client Funds"
    icon = "fa-solid fa-link"
    category = "Clients & Funds"
    category_icon = "fa-solid fa-users"


class TransactionsAdmin(_ReadOnlyView, model=Transactions):
    name = "Transaction"
    name_plural = "Transactions"
    icon = "fa-solid fa-money-bill-transfer"
    category = "Clients & Funds"
    category_icon = "fa-solid fa-users"


class ClientFeaturesAdmin(_ReadOnlyView, model=ClientFeatures):
    name = "Client Features"
    name_plural = "Client Features"
    icon = "fa-solid fa-chart-simple"
    category = "Clients & Funds"
    category_icon = "fa-solid fa-users"


class PiiVaultAdmin(_ReadOnlyView, model=PiiVault):
    name = "PII Vault"
    name_plural = "PII Vault"
    icon = "fa-solid fa-lock"
    category = "Clients & Funds"
    category_icon = "fa-solid fa-users"


class FaAssignmentAdmin(_ReadOnlyView, model=FaAssignment):
    name = "FA Assignment"
    name_plural = "FA Assignments"
    icon = "fa-solid fa-user-tie"
    category = "Clients & Funds"
    category_icon = "fa-solid fa-users"


class ActiveClientFundAdmin(_ReadOnlyView, model=ActiveClientFund):
    name = "Active Client Fund"
    name_plural = "Active Client Funds"
    icon = "fa-solid fa-wallet"
    category = "Active Book"
    category_icon = "fa-solid fa-chart-line"


class ActiveClientInteractionAdmin(_ReadOnlyView, model=ActiveClientInteraction):
    name = "Active Client Interaction"
    name_plural = "Active Client Interactions"
    icon = "fa-solid fa-comments"
    category = "Active Book"
    category_icon = "fa-solid fa-chart-line"
    column_default_sort = [(ActiveClientInteraction.created_at, True)]


class ActiveTransactionAdmin(_ReadOnlyView, model=ActiveTransaction):
    name = "Active Transaction"
    name_plural = "Active Transactions"
    icon = "fa-solid fa-money-bill-transfer"
    category = "Active Book"
    category_icon = "fa-solid fa-chart-line"


class RiskConfigVersionAdmin(_ReadOnlyView, model=RiskConfigVersion):
    name = "Risk Config Version"
    name_plural = "Risk Config Versions"
    icon = "fa-solid fa-sliders"
    category = "Risk"
    category_icon = "fa-solid fa-triangle-exclamation"
    column_default_sort = [(RiskConfigVersion.created_at, True)]


class ClientRiskFeaturesAdmin(_ReadOnlyView, model=ClientRiskFeatures):
    name = "Client Risk Features"
    name_plural = "Client Risk Features"
    icon = "fa-solid fa-chart-simple"
    category = "Risk"
    category_icon = "fa-solid fa-triangle-exclamation"


class RiskRunAdmin(_ReadOnlyView, model=RiskRun):
    name = "Risk Run"
    name_plural = "Risk Runs"
    icon = "fa-solid fa-play"
    category = "Risk"
    category_icon = "fa-solid fa-triangle-exclamation"


class RiskSnapshotAdmin(_ReadOnlyView, model=RiskSnapshot):
    name = "Risk Snapshot"
    name_plural = "Risk Snapshots"
    icon = "fa-solid fa-camera"
    category = "Risk"
    category_icon = "fa-solid fa-triangle-exclamation"
    column_default_sort = [(RiskSnapshot.created_at, True)]


class CampaignAdmin(_ReadOnlyView, model=Campaign):
    name = "Campaign"
    name_plural = "Campaigns"
    icon = "fa-solid fa-bullhorn"
    category = "Campaigns & Outreach"
    category_icon = "fa-solid fa-bullhorn"
    column_list = [
        Campaign.campaign_id,
        Campaign.name,
        Campaign.campaign_type,
        Campaign.status,
        Campaign.start_date,
        Campaign.end_date,
        Campaign.created_at,
    ]
    column_default_sort = [(Campaign.created_at, True)]


class CampaignStepAdmin(_ReadOnlyView, model=CampaignStep):
    name = "Campaign Step"
    name_plural = "Campaign Steps"
    icon = "fa-solid fa-shoe-prints"
    category = "Campaigns & Outreach"
    category_icon = "fa-solid fa-bullhorn"
    column_list = [
        CampaignStep.step_id,
        CampaignStep.campaign_id,
        CampaignStep.step_no,
        CampaignStep.offset_days,
        CampaignStep.message_angle,
        CampaignStep.template_ref,
    ]
    column_default_sort = [(CampaignStep.campaign_id, False), (CampaignStep.step_no, False)]


class EnrollmentAdmin(_ReadOnlyView, model=Enrollment):
    name = "Enrollment"
    name_plural = "Enrollments"
    icon = "fa-solid fa-user-plus"
    category = "Campaigns & Outreach"
    category_icon = "fa-solid fa-bullhorn"


class TouchLogAdmin(_ReadOnlyView, model=TouchLog):
    name = "Touch Log"
    name_plural = "Touch Log"
    icon = "fa-solid fa-hand-pointer"
    category = "Campaigns & Outreach"
    category_icon = "fa-solid fa-bullhorn"
    column_default_sort = [(TouchLog.created_at, True)]


class ContactEventAdmin(_ReadOnlyView, model=ContactEvent):
    name = "Contact Event"
    name_plural = "Contact Events"
    icon = "fa-solid fa-calendar-check"
    category = "Campaigns & Outreach"
    category_icon = "fa-solid fa-bullhorn"
    column_default_sort = [(ContactEvent.created_at, True)]


class ReviewCohortAdmin(_ReadOnlyView, model=ReviewCohort):
    name = "Review Cohort"
    name_plural = "Review Cohorts"
    icon = "fa-solid fa-layer-group"
    category = "Campaigns & Outreach"
    category_icon = "fa-solid fa-bullhorn"
    column_default_sort = [(ReviewCohort.created_at, True)]


class OutreachMessageAdmin(_ReadOnlyView, model=OutreachMessage):
    name = "Outreach Message"
    name_plural = "Outreach Messages"
    icon = "fa-solid fa-envelope"
    category = "Campaigns & Outreach"
    category_icon = "fa-solid fa-bullhorn"
    column_default_sort = [(OutreachMessage.created_at, True)]


class ReviewActionAdmin(_ReadOnlyView, model=ReviewAction):
    name = "Review Action"
    name_plural = "Review Actions"
    icon = "fa-solid fa-check-to-slot"
    category = "Campaigns & Outreach"
    category_icon = "fa-solid fa-bullhorn"
    column_default_sort = [(ReviewAction.created_at, True)]


class SuppressionAdmin(_ReadOnlyView, model=Suppression):
    name = "Suppression"
    name_plural = "Suppressions"
    icon = "fa-solid fa-ban"
    category = "Campaigns & Outreach"
    category_icon = "fa-solid fa-bullhorn"
    column_default_sort = [(Suppression.created_at, True)]


class MessageTemplateAdmin(_ReadOnlyView, model=MessageTemplate):
    name = "Message Template"
    name_plural = "Message Templates"
    icon = "fa-solid fa-file-lines"
    category = "Templates & Rules"
    category_icon = "fa-solid fa-scale-balanced"
    column_list = [
        MessageTemplate.template_id,
        MessageTemplate.campaign_id,
        MessageTemplate.status,
        MessageTemplate.profile_key,
        MessageTemplate.created_at,
        MessageTemplate.updated_at,
    ]
    column_default_sort = [(MessageTemplate.created_at, True)]


class TemplateReviewActionAdmin(_ReadOnlyView, model=TemplateReviewAction):
    name = "Template Review Action"
    name_plural = "Template Review Actions"
    icon = "fa-solid fa-check-to-slot"
    category = "Templates & Rules"
    category_icon = "fa-solid fa-scale-balanced"
    column_default_sort = [(TemplateReviewAction.created_at, True)]


class BusinessRuleAdmin(_ReadOnlyView, model=BusinessRule):
    name = "Business Rule"
    name_plural = "Business Rules"
    icon = "fa-solid fa-scale-balanced"
    category = "Templates & Rules"
    category_icon = "fa-solid fa-scale-balanced"
    column_list = [
        BusinessRule.rule_id,
        BusinessRule.version,
        BusinessRule.priority,
        BusinessRule.name,
        BusinessRule.message_angle,
        BusinessRule.priority_tier,
        BusinessRule.valid_from,
        BusinessRule.valid_to,
    ]
    column_default_sort = [(BusinessRule.version, True), (BusinessRule.priority, False)]


class MessageAngleCatalogAdmin(_ReadOnlyView, model=MessageAngleCatalog):
    name = "Message Angle"
    name_plural = "Message Angle Catalog"
    icon = "fa-solid fa-comments"
    category = "Templates & Rules"
    category_icon = "fa-solid fa-scale-balanced"
    column_list = [
        MessageAngleCatalog.catalog_id,
        MessageAngleCatalog.version,
        MessageAngleCatalog.angle,
        MessageAngleCatalog.headline,
        MessageAngleCatalog.use,
        MessageAngleCatalog.held,
        MessageAngleCatalog.valid_from,
        MessageAngleCatalog.valid_to,
    ]
    column_default_sort = [(MessageAngleCatalog.version, True)]


class TierContractAdmin(_ReadOnlyView, model=TierContract):
    name = "Tier Contract"
    name_plural = "Tier Contracts"
    icon = "fa-solid fa-layer-group"
    category = "Templates & Rules"
    category_icon = "fa-solid fa-scale-balanced"
    column_list = [
        TierContract.contract_id,
        TierContract.version,
        TierContract.tier,
        TierContract.display_name,
        TierContract.primary_channel,
        TierContract.human_approval,
        TierContract.review_sample_rate,
        TierContract.valid_from,
        TierContract.valid_to,
    ]
    column_default_sort = [(TierContract.version, True)]


class ClientMessageIndicatorsAdmin(_ReadOnlyView, model=ClientMessageIndicators):
    name = "Client Message Indicators"
    name_plural = "Client Message Indicators"
    icon = "fa-solid fa-flag"
    category = "Templates & Rules"
    category_icon = "fa-solid fa-scale-balanced"


class TemplateGenerationPlanAdmin(_ReadOnlyView, model=TemplateGenerationPlan):
    name = "Template Generation Plan"
    name_plural = "Template Generation Plans"
    icon = "fa-solid fa-diagram-project"
    category = "Templates & Rules"
    category_icon = "fa-solid fa-scale-balanced"
    column_default_sort = [(TemplateGenerationPlan.created_at, True)]


class CampaignTemplatePolicyAdmin(_ReadOnlyView, model=CampaignTemplatePolicy):
    name = "Campaign Template Policy"
    name_plural = "Campaign Template Policies"
    icon = "fa-solid fa-gavel"
    category = "Templates & Rules"
    category_icon = "fa-solid fa-scale-balanced"


class TemplatePolicyConfigVersionAdmin(_ReadOnlyView, model=TemplatePolicyConfigVersion):
    name = "Template Policy Config Version"
    name_plural = "Template Policy Config Versions"
    icon = "fa-solid fa-sliders"
    category = "Templates & Rules"
    category_icon = "fa-solid fa-scale-balanced"
    column_default_sort = [(TemplatePolicyConfigVersion.created_at, True)]


class GenerationCostConfigVersionAdmin(_ReadOnlyView, model=GenerationCostConfigVersion):
    name = "Generation Cost Config Version"
    name_plural = "Generation Cost Config Versions"
    icon = "fa-solid fa-coins"
    category = "Templates & Rules"
    category_icon = "fa-solid fa-scale-balanced"
    column_default_sort = [(GenerationCostConfigVersion.created_at, True)]


class ModelVersionAdmin(_ReadOnlyView, model=ModelVersion):
    name = "Model Version"
    name_plural = "Model Versions"
    icon = "fa-solid fa-microchip"
    category = "LLM Ops"
    category_icon = "fa-solid fa-robot"
    column_default_sort = [(ModelVersion.created_at, True)]


class PromptVersionAdmin(_ReadOnlyView, model=PromptVersion):
    name = "Prompt Version"
    name_plural = "Prompt Versions"
    icon = "fa-solid fa-terminal"
    category = "LLM Ops"
    category_icon = "fa-solid fa-robot"
    column_default_sort = [(PromptVersion.created_at, True)]


class GenerationRunAdmin(_ReadOnlyView, model=GenerationRun):
    name = "Generation Run"
    name_plural = "Generation Runs"
    icon = "fa-solid fa-bolt"
    category = "LLM Ops"
    category_icon = "fa-solid fa-robot"
    column_default_sort = [(GenerationRun.created_at, True)]


class LLMRequestAdmin(_ReadOnlyView, model=LLMRequest):
    name = "LLM Request"
    name_plural = "LLM Requests"
    icon = "fa-solid fa-arrow-right"
    category = "LLM Ops"
    category_icon = "fa-solid fa-robot"
    column_default_sort = [(LLMRequest.created_at, True)]


class LLMResponseAdmin(_ReadOnlyView, model=LLMResponse):
    name = "LLM Response"
    name_plural = "LLM Responses"
    icon = "fa-solid fa-arrow-left"
    category = "LLM Ops"
    category_icon = "fa-solid fa-robot"
    column_default_sort = [(LLMResponse.created_at, True)]


class TokenUsageAdmin(_ReadOnlyView, model=TokenUsage):
    name = "Token Usage"
    name_plural = "Token Usage"
    icon = "fa-solid fa-hashtag"
    category = "LLM Ops"
    category_icon = "fa-solid fa-robot"
    column_default_sort = [(TokenUsage.created_at, True)]


class ToolCallAdmin(_ReadOnlyView, model=ToolCall):
    name = "Tool Call"
    name_plural = "Tool Calls"
    icon = "fa-solid fa-wrench"
    category = "LLM Ops"
    category_icon = "fa-solid fa-robot"
    column_default_sort = [(ToolCall.created_at, True)]


class TraceRefAdmin(_ReadOnlyView, model=TraceRef):
    name = "Trace Ref"
    name_plural = "Trace Refs"
    icon = "fa-solid fa-route"
    category = "LLM Ops"
    category_icon = "fa-solid fa-robot"
    column_default_sort = [(TraceRef.created_at, True)]


class RubricVersionAdmin(_ReadOnlyView, model=RubricVersion):
    name = "Rubric Version"
    name_plural = "Rubric Versions"
    icon = "fa-solid fa-ruler"
    category = "LLM Ops"
    category_icon = "fa-solid fa-robot"
    column_default_sort = [(RubricVersion.created_at, True)]


class EvaluationAdmin(_ReadOnlyView, model=Evaluation):
    name = "Evaluation"
    name_plural = "Evaluations"
    icon = "fa-solid fa-star"
    category = "LLM Ops"
    category_icon = "fa-solid fa-robot"
    column_default_sort = [(Evaluation.created_at, True)]


class GenerationBatchAdmin(_ReadOnlyView, model=GenerationBatch):
    name = "Generation Batch"
    name_plural = "Generation Batches"
    icon = "fa-solid fa-layer-group"
    category = "LLM Ops"
    category_icon = "fa-solid fa-robot"
    column_default_sort = [(GenerationBatch.created_at, True)]


class GenerationBatchItemAdmin(_ReadOnlyView, model=GenerationBatchItem):
    name = "Generation Batch Item"
    name_plural = "Generation Batch Items"
    icon = "fa-solid fa-cube"
    category = "LLM Ops"
    category_icon = "fa-solid fa-robot"
    column_default_sort = [(GenerationBatchItem.created_at, True)]


class InstantiationBatchAdmin(_ReadOnlyView, model=InstantiationBatch):
    name = "Instantiation Batch"
    name_plural = "Instantiation Batches"
    icon = "fa-solid fa-clone"
    category = "LLM Ops"
    category_icon = "fa-solid fa-robot"
    column_default_sort = [(InstantiationBatch.created_at, True)]


class RagDocumentAdmin(_ReadOnlyView, model=RagDocument):
    name = "RAG Document"
    name_plural = "RAG Documents"
    icon = "fa-solid fa-file"
    category = "RAG"
    category_icon = "fa-solid fa-database"
    column_default_sort = [(RagDocument.created_at, True)]


class RagDocumentVersionAdmin(_ReadOnlyView, model=RagDocumentVersion):
    name = "RAG Document Version"
    name_plural = "RAG Document Versions"
    icon = "fa-solid fa-code-branch"
    category = "RAG"
    category_icon = "fa-solid fa-database"


class RagChunkAdmin(_ReadOnlyView, model=RagChunk):
    name = "RAG Chunk"
    name_plural = "RAG Chunks"
    icon = "fa-solid fa-puzzle-piece"
    category = "RAG"
    category_icon = "fa-solid fa-database"
    column_default_sort = [(RagChunk.created_at, True)]


class DigestRunAdmin(_ReadOnlyView, model=DigestRun):
    name = "Digest Run"
    name_plural = "Digest Runs"
    icon = "fa-solid fa-play"
    category = "Digest"
    category_icon = "fa-solid fa-newspaper"


class DigestLineAdmin(_ReadOnlyView, model=DigestLine):
    name = "Digest Line"
    name_plural = "Digest Lines"
    icon = "fa-solid fa-list"
    category = "Digest"
    category_icon = "fa-solid fa-newspaper"
    column_default_sort = [(DigestLine.created_at, True)]


class DigestEmailSendAdmin(_ReadOnlyView, model=DigestEmailSend):
    name = "Digest Email Send"
    name_plural = "Digest Email Sends"
    icon = "fa-solid fa-paper-plane"
    category = "Digest"
    category_icon = "fa-solid fa-newspaper"
    column_default_sort = [(DigestEmailSend.created_at, True)]


class ClientComplaintAdmin(_ReadOnlyView, model=ClientComplaint):
    name = "Client Complaint"
    name_plural = "Client Complaints"
    icon = "fa-solid fa-circle-exclamation"
    category = "Complaints"
    category_icon = "fa-solid fa-circle-exclamation"


class BriefingNarrativeAdmin(_ReadOnlyView, model=BriefingNarrative):
    name = "Briefing Narrative"
    name_plural = "Briefing Narratives"
    icon = "fa-solid fa-book-open"
    category = "Briefing"
    category_icon = "fa-solid fa-book-open"


class ReviewerUserAdmin(_ReadOnlyView, model=ReviewerUser):
    name = "Reviewer User"
    name_plural = "Reviewer Users"
    icon = "fa-solid fa-user-shield"
    category = "Access & Audit"
    category_icon = "fa-solid fa-shield-halved"
    column_default_sort = [(ReviewerUser.created_at, True)]


class AuditLogAdmin(_ReadOnlyView, model=AuditLog):
    name = "Audit Log"
    name_plural = "Audit Log"
    icon = "fa-solid fa-shield-halved"
    category = "Access & Audit"
    category_icon = "fa-solid fa-shield-halved"
    column_default_sort = [(AuditLog.created_at, True)]


class IdempotencyKeyAdmin(_ReadOnlyView, model=IdempotencyKey):
    name = "Idempotency Key"
    name_plural = "Idempotency Keys"
    icon = "fa-solid fa-key"
    category = "Access & Audit"
    category_icon = "fa-solid fa-shield-halved"
    column_default_sort = [(IdempotencyKey.created_at, True)]


ADMIN_VIEWS = [
    RawStagingAdmin,
    IngestionStatusAdmin,
    IngestionRejectAdmin,
    FundsAdmin,
    ClientsAdmin,
    ClientFundAdmin,
    TransactionsAdmin,
    ClientFeaturesAdmin,
    PiiVaultAdmin,
    FaAssignmentAdmin,
    ActiveClientFundAdmin,
    ActiveClientInteractionAdmin,
    ActiveTransactionAdmin,
    RiskConfigVersionAdmin,
    ClientRiskFeaturesAdmin,
    RiskRunAdmin,
    RiskSnapshotAdmin,
    CampaignAdmin,
    CampaignStepAdmin,
    EnrollmentAdmin,
    TouchLogAdmin,
    ContactEventAdmin,
    ReviewCohortAdmin,
    OutreachMessageAdmin,
    ReviewActionAdmin,
    SuppressionAdmin,
    MessageTemplateAdmin,
    TemplateReviewActionAdmin,
    BusinessRuleAdmin,
    MessageAngleCatalogAdmin,
    TierContractAdmin,
    ClientMessageIndicatorsAdmin,
    TemplateGenerationPlanAdmin,
    CampaignTemplatePolicyAdmin,
    TemplatePolicyConfigVersionAdmin,
    GenerationCostConfigVersionAdmin,
    ModelVersionAdmin,
    PromptVersionAdmin,
    GenerationRunAdmin,
    LLMRequestAdmin,
    LLMResponseAdmin,
    TokenUsageAdmin,
    ToolCallAdmin,
    TraceRefAdmin,
    RubricVersionAdmin,
    EvaluationAdmin,
    GenerationBatchAdmin,
    GenerationBatchItemAdmin,
    InstantiationBatchAdmin,
    RagDocumentAdmin,
    RagDocumentVersionAdmin,
    RagChunkAdmin,
    DigestRunAdmin,
    DigestLineAdmin,
    DigestEmailSendAdmin,
    ClientComplaintAdmin,
    BriefingNarrativeAdmin,
    ReviewerUserAdmin,
    AuditLogAdmin,
    IdempotencyKeyAdmin,
]
