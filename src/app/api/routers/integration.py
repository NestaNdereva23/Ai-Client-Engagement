"""Integration inbound: the parent system pushes contact channels and
suppressions, and can trigger a data refresh. Every route here sits behind
require_integration_key, a stopgap ahead of M8A.7's real scoped API keys;
this plane never reaches review, approve, or send (design ADR-06A).
"""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.integration_auth import require_integration_key
from app.api.routers.ingestion import get_cytonn_client
from app.db.session import get_session
from app.ingestion.api_client import CytonnClient
from app.schemas.ingestion import IngestionRunAccepted, TriggerIngestionRequest
from app.schemas.integration import (
    ContactUpsertOut,
    ContactUpsertRequest,
    SuppressionOut,
    SuppressionRequest,
)
from app.services.ingestion import RunNotFound, resolve_trigger_run_id
from app.services.ingestion import run_in_background as run_ingestion_in_background
from app.services.integration import ClientNotFound, record_suppression, resolve_client_id
from app.services.integration import upsert_contact as upsert_contact_service

router = APIRouter(
    prefix="/integration", tags=["integration"], dependencies=[Depends(require_integration_key)]
)


@router.post("/contacts", response_model=ContactUpsertOut)
def upsert_contact(
    body: ContactUpsertRequest, session: Session = Depends(get_session)
) -> ContactUpsertOut:
    """Upsert contact channels and consent for a client_id or client_code."""
    try:
        client_id = resolve_client_id(
            session, client_id=body.client_id, client_code=body.client_code
        )
    except ClientNotFound:
        raise HTTPException(status_code=404, detail="client_code not found") from None

    record = upsert_contact_service(
        client_id=client_id,
        contact_email=body.contact_email,
        contact_whatsapp=body.contact_whatsapp,
        consent=body.consent,
        source=body.source,
    )
    return ContactUpsertOut(
        client_id=record.client_id,
        contact_email=record.contact_email,
        contact_whatsapp=record.contact_whatsapp,
        consent=record.consent,
        updated_at=record.updated_at,
    )


@router.post("/suppressions", response_model=SuppressionOut)
def add_suppression(body: SuppressionRequest) -> SuppressionOut:
    """Sync one unsubscribe or opt-out from Cytonn's CRM."""
    record = record_suppression(client_id=body.client_id, reason=body.reason, source=body.source)
    return SuppressionOut(
        client_id=record.client_id,
        reason=record.reason,
        source=record.source,
        created_at=record.created_at,
    )


@router.post("/ingestion/runs", response_model=IngestionRunAccepted, status_code=202)
def trigger_run(
    body: TriggerIngestionRequest,
    background_tasks: BackgroundTasks,
    client: CytonnClient = Depends(get_cytonn_client),
    session: Session = Depends(get_session),
) -> IngestionRunAccepted:
    """Let the parent system trigger a data refresh; runs in the background."""
    try:
        run_id = resolve_trigger_run_id(session, body.run_id)
    except RunNotFound:
        raise HTTPException(
            status_code=400, detail=f"no existing run to resume: {body.run_id}"
        ) from None

    background_tasks.add_task(
        run_ingestion_in_background,
        client,
        run_id=run_id,
        endpoint=body.endpoint,
        max_pages=body.max_pages,
    )
    return IngestionRunAccepted(run_id=run_id)
