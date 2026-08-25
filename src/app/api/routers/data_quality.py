from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.reviewer_auth import get_current_reviewer_id
from app.db.session import get_session
from app.schemas.data_quality import DataQualityOut, RejectReasonCount
from app.services.data_quality import NoRunsYet, quality_summary
from app.services.ingestion import RunNotFound

router = APIRouter(
    prefix="/data", tags=["data-quality"], dependencies=[Depends(get_current_reviewer_id)]
)


@router.get("/quality", response_model=DataQualityOut)
def get_data_quality(
    run_id: str | None = None, session: Session = Depends(get_session)
) -> DataQualityOut:
    try:
        run, reasons = quality_summary(session, run_id=run_id)
    except RunNotFound:
        raise HTTPException(status_code=404, detail="run not found") from None
    except NoRunsYet:
        raise HTTPException(status_code=404, detail="no ingestion runs yet") from None
    return DataQualityOut(
        run_id=run.run_id,
        records_seen=run.records_seen,
        records_written=run.records_written,
        records_rejected=run.records_rejected,
        shortfall=run.shortfall,
        reject_reasons=[RejectReasonCount(reason=r, count=c) for r, c in reasons],
    )
