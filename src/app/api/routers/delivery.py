from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.reviewer_auth import get_current_reviewer_id
from app.db.session import get_session
from app.delivery.sms_sender import send_test_sms
from app.schemas.delivery import TestSmsSendOut, TestSmsSendRequest

router = APIRouter(
    prefix="/delivery", tags=["delivery"], dependencies=[Depends(get_current_reviewer_id)]
)


@router.post("/sms/test", response_model=TestSmsSendOut)
def test_send_sms(
    body: TestSmsSendRequest, session: Session = Depends(get_session)
) -> TestSmsSendOut:
    # A one-off send to a staff number, kept apart from any campaign or touch.
    result = send_test_sms(session, to=body.to, body=body.body)
    return TestSmsSendOut(
        sent=result.sent,
        recipient=result.recipient,
        parts=result.parts,
        provider_status=result.provider_status,
        reason=result.reason,
    )
