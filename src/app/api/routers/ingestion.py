"""Ingestion console: trigger a pull, and browse run status."""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.session import get_session
from app.ingestion.api_client import CytonnClient
from app.pagination import DEFAULT_LIMIT, MAX_LIMIT, InvalidCursor, Page
from app.schemas.ingestion import IngestionRunAccepted, IngestionRunOut, TriggerIngestionRequest
from app.services.ingestion import RunNotFound, get_run, list_runs, resolve_trigger_run_id
from app.services.ingestion import run_in_background as run_ingestion_in_background

router = APIRouter(prefix="/ingestion", tags=["ingestion"])


def get_cytonn_client() -> CytonnClient:
    """The live Cytonn client, or a 503 when the app isn't configured to reach it."""
    settings = get_settings()
    if not settings.cytonn_api_base_url or not settings.cytonn_api_key:
        raise HTTPException(status_code=503, detail="Cytonn API is not configured")
    return CytonnClient(settings.cytonn_api_base_url, settings.cytonn_api_key)


@router.post("/runs", response_model=IngestionRunAccepted, status_code=202)
def trigger_run(
    body: TriggerIngestionRequest,
    background_tasks: BackgroundTasks,
    client: CytonnClient = Depends(get_cytonn_client),
    session: Session = Depends(get_session),
) -> IngestionRunAccepted:
    """Start a fresh pull, or resume one that stopped. Runs in the background;
    poll GET /ingestion/runs/{run_id} for progress.

    A supplied run_id must already exist; a typo or an unedited API-docs
    placeholder must never silently start a fresh run under that name.
    """
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


@router.get("/runs", response_model=Page[IngestionRunOut])
def list_ingestion_runs(
    cursor: str | None = None,
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    session: Session = Depends(get_session),
) -> Page[IngestionRunOut]:
    """Runs newest first, one page at a time."""
    try:
        runs, next_cursor = list_runs(session, cursor=cursor, limit=limit)
    except InvalidCursor:
        raise HTTPException(status_code=400, detail="invalid cursor") from None
    return Page(items=[IngestionRunOut.model_validate(r) for r in runs], next_cursor=next_cursor)


@router.get("/runs/{run_id}", response_model=IngestionRunOut)
def get_ingestion_run(run_id: str, session: Session = Depends(get_session)) -> IngestionRunOut:
    """One run's progress and counters."""
    try:
        run = get_run(session, run_id)
    except RunNotFound:
        raise HTTPException(status_code=404, detail="run not found") from None
    return IngestionRunOut.model_validate(run)
