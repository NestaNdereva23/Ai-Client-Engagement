from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.access import require_ai_assistant
from app.api.routers import (
    access,
    action_catalog,
    active_clients,
    admin,
    agent_insights,
    agent_metrics,
    agent_patterns,
    agent_proposals,
    agent_runs,
    agent_studio,
    app_settings,
    audit,
    briefing,
    campaign_agent_studio,
    campaign_suggestions,
    campaigns,
    chat,
    client_assistant,
    clients,
    data_quality,
    delivery,
    digest,
    fa_action,
    ingestion,
    integration,
    prompt_config,
    rag,
    rates,
    review,
    risk,
    rules,
    situations,
    templates,
)

ai_assistant_only = [Depends(require_ai_assistant)]

router = APIRouter(prefix="/api/v1")
router.include_router(review.router)
router.include_router(agent_proposals.router, dependencies=ai_assistant_only)
router.include_router(agent_insights.router, dependencies=ai_assistant_only)
router.include_router(agent_patterns.router, dependencies=ai_assistant_only)
router.include_router(agent_runs.router)
router.include_router(ingestion.router)
router.include_router(data_quality.router)
router.include_router(clients.router)
router.include_router(campaign_suggestions.router)
router.include_router(campaign_agent_studio.router)
router.include_router(campaigns.router)
router.include_router(templates.router)
router.include_router(rules.router)
router.include_router(prompt_config.router)
router.include_router(audit.router)
router.include_router(rag.router)
router.include_router(integration.router)
router.include_router(admin.router)
router.include_router(risk.router)
router.include_router(digest.router)
router.include_router(briefing.router)
router.include_router(client_assistant.router)
router.include_router(active_clients.router)
router.include_router(fa_action.router)
router.include_router(delivery.router)
router.include_router(situations.router, dependencies=ai_assistant_only)
router.include_router(agent_studio.router)
router.include_router(app_settings.router)
router.include_router(rates.router)
router.include_router(access.router)
router.include_router(chat.router, dependencies=ai_assistant_only)
router.include_router(agent_metrics.router, dependencies=ai_assistant_only)
router.include_router(action_catalog.router, dependencies=ai_assistant_only)
