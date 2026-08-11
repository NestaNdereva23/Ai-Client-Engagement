"""ModelViews for the operational admin. List and detail only, no writes yet.

Once audit coverage on admin writes is in place, can_create/can_edit can be
turned on model by model; can_delete stays off, since nothing in this schema
is meant to disappear.
"""

from __future__ import annotations

from sqladmin import ModelView

from app.db.models.campaigns import CampaignStep
from app.db.models.message_template import MessageTemplate, TemplateReviewAction
from app.db.models.outreach import Campaign
from app.db.models.rules import BusinessRule, MessageAngleCatalog, TierContract


class _ReadOnlyView(ModelView):
    can_create = False
    can_edit = False
    can_delete = False
    can_export = False


class CampaignAdmin(_ReadOnlyView, model=Campaign):
    name = "Campaign"
    name_plural = "Campaigns"
    icon = "fa-solid fa-bullhorn"
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
    column_list = [
        CampaignStep.step_id,
        CampaignStep.campaign_id,
        CampaignStep.step_no,
        CampaignStep.offset_days,
        CampaignStep.message_angle,
        CampaignStep.template_ref,
    ]
    column_default_sort = [(CampaignStep.campaign_id, False), (CampaignStep.step_no, False)]


class MessageTemplateAdmin(_ReadOnlyView, model=MessageTemplate):
    name = "Message Template"
    name_plural = "Message Templates"
    icon = "fa-solid fa-file-lines"
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
    column_default_sort = [(TemplateReviewAction.created_at, True)]


class BusinessRuleAdmin(_ReadOnlyView, model=BusinessRule):
    name = "Business Rule"
    name_plural = "Business Rules"
    icon = "fa-solid fa-scale-balanced"
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
    column_list = [
        MessageAngleCatalog.catalog_id,
        MessageAngleCatalog.version,
        MessageAngleCatalog.angle,
        MessageAngleCatalog.headline,
        MessageAngleCatalog.held,
        MessageAngleCatalog.valid_from,
        MessageAngleCatalog.valid_to,
    ]
    column_default_sort = [(MessageAngleCatalog.version, True)]


class TierContractAdmin(_ReadOnlyView, model=TierContract):
    name = "Tier Contract"
    name_plural = "Tier Contracts"
    icon = "fa-solid fa-layer-group"
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


ADMIN_VIEWS = [
    CampaignAdmin,
    CampaignStepAdmin,
    MessageTemplateAdmin,
    TemplateReviewActionAdmin,
    BusinessRuleAdmin,
    MessageAngleCatalogAdmin,
    TierContractAdmin,
]
