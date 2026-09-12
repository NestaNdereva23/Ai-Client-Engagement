from __future__ import annotations

from fastapi import APIRouter

from app.api.routers import (
    active_clients,
    admin,
    agent_insights,
    agent_proposals,
    agent_runs,
    audit,
    briefing,
    campaigns,
    clients,
    data_quality,
    digest,
    ingestion,
    integration,
    prompt_config,
    rag,
    review,
    risk,
    rules,
    templates,
)

router = APIRouter(prefix="/api/v1")
router.include_router(review.router)
router.include_router(agent_proposals.router)
router.include_router(agent_insights.router)
router.include_router(agent_runs.router)
router.include_router(ingestion.router)
router.include_router(data_quality.router)
router.include_router(clients.router)
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
router.include_router(active_clients.router)
