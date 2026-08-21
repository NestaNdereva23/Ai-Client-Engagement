"""The reviewer console: server-rendered pages over the same review
service the JSON /reviews API uses (app.services.review).

Session-authenticated (app.auth.session), not X-Reviewer-Key -- this is
the real login for a human at a browser. reviewer_id on every decision is
the logged-in user's username, resolved from their session the same way
the JSON API resolves it from a header: never a value the request itself
claims.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.passwords import verify_password
from app.auth.session import login_user, logout_user, require_role
from app.db.models.auth import ReviewerUser
from app.db.session import get_session
from app.pagination import DEFAULT_LIMIT
from app.services.review import (
    CohortNotFound,
    CohortNotReady,
    EditedContentRequired,
    InvalidOutcome,
    MessageAlreadyDecided,
    MessageNotFound,
    build_review_context,
    check_cohort_ready,
    count_pending_messages,
    get_message,
    get_review_history,
    list_pending_messages_for_queue,
)
from app.services.review import approve_cohort_remainder as approve_cohort_remainder_service
from app.services.review import decide as decide_message

router = APIRouter(prefix="/reviewer", tags=["reviewer_ui"])

_TEMPLATES_DIR = Path(__file__).resolve().parents[2] / "templates" / "reviewer"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

# Roles allowed to work the review queue. FA and RelationshipManager have
# no reason to be in here yet -- they're read on the FA console side of
# the system, not the reviewer side. A module-level singleton rather than
# a bare require_role(*_QUEUE_ROLES) call in each Depends(...) below, since
# a function call in an argument default is evaluated once at import time
# either way -- naming it here just makes that explicit.
_QUEUE_ROLES = ("reviewer", "team_lead", "admin")
_require_queue_role = require_role(*_QUEUE_ROLES)


@router.get("/login")
def login_form(request: Request) -> object:
    return templates.TemplateResponse(request, "login.html", {"error": None, "current_user": None})


@router.post("/login")
def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    session: Session = Depends(get_session),
) -> object:
    user = session.scalar(select(ReviewerUser).where(ReviewerUser.username == username))
    if user is None or not user.active or not verify_password(password, user.password_hash):
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "Invalid username or password.", "current_user": None},
            status_code=401,
        )
    login_user(request, user)
    return RedirectResponse(url="/reviewer/queue", status_code=303)


@router.post("/logout")
def logout_submit(request: Request) -> RedirectResponse:
    logout_user(request)
    return RedirectResponse(url="/reviewer/login", status_code=303)


@router.get("/queue")
def queue(
    request: Request,
    cursor: str | None = None,
    current_user: ReviewerUser = Depends(_require_queue_role),
    session: Session = Depends(get_session),
) -> object:
    items, next_cursor = list_pending_messages_for_queue(
        session, cursor=cursor, limit=DEFAULT_LIMIT
    )
    total_count = count_pending_messages(session)
    return templates.TemplateResponse(
        request,
        "queue.html",
        {
            "current_user": current_user,
            "items": items,
            "next_cursor": next_cursor,
            "total_count": total_count,
        },
    )


@router.get("/messages/{message_id}")
def message_detail(
    request: Request,
    message_id: str,
    current_user: ReviewerUser = Depends(_require_queue_role),
    session: Session = Depends(get_session),
    error: str | None = None,
) -> object:
    try:
        message = get_message(session, message_id)
    except MessageNotFound:
        return templates.TemplateResponse(
            request,
            "queue.html",
            {
                "current_user": current_user,
                "items": [],
                "next_cursor": None,
                "total_count": 0,
                "error": f"message {message_id} not found",
            },
            status_code=404,
        )

    context = build_review_context(session, message)
    history = get_review_history(session, message_id)
    return templates.TemplateResponse(
        request,
        "message_detail.html",
        {
            "current_user": current_user,
            "message": message,
            "context": context,
            "history": history,
            "can_hold_or_reject": message.cohort_id is None,
            "error": error,
        },
    )


@router.post("/messages/{message_id}/decide")
def decide_submit(
    request: Request,
    message_id: str,
    outcome: str = Form(...),
    reason: str = Form(""),
    edited_subject: str = Form(""),
    edited_body: str = Form(""),
    current_user: ReviewerUser = Depends(_require_queue_role),
    session: Session = Depends(get_session),
) -> object:
    edited_content = (
        {"subject": edited_subject, "body": edited_body} if outcome == "edit_approve" else None
    )
    try:
        message = get_message(session, message_id)
        decide_message(
            session,
            message_id,
            outcome=outcome,
            reviewer_id=current_user.username,
            reason=reason or None,
            edited_content=edited_content,
        )
        check_cohort_ready(session, message)
        session.commit()
    except (
        MessageNotFound,
        MessageAlreadyDecided,
        EditedContentRequired,
        InvalidOutcome,
    ) as exc:
        session.rollback()
        return message_detail(
            request, message_id, current_user=current_user, session=session, error=str(exc)
        )

    return RedirectResponse(url="/reviewer/queue", status_code=303)


@router.post("/cohorts/{cohort_id}/approve-remaining")
def approve_cohort_remaining_submit(
    request: Request,
    cohort_id: str,
    current_user: ReviewerUser = Depends(_require_queue_role),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    try:
        approve_cohort_remainder_service(session, cohort_id, reviewer_id=current_user.username)
        session.commit()
    except (CohortNotFound, CohortNotReady):
        session.rollback()
    return RedirectResponse(url="/reviewer/queue", status_code=303)
