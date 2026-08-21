"""Reviewer console login: session auth and role gating (app.auth.session).

Drives it through TestClient against the real app, the same way
test_api_review.py drives the JSON API, so this proves the actual request
flow (cookie set on login, redirect when logged out, 403 on the wrong
role) rather than re-testing the dependency functions in isolation.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.auth.passwords import hash_password
from app.db.models.auth import ReviewerUser
from app.db.session import SessionLocal
from app.main import app

client = TestClient(app)

LOGIN = "/reviewer/login"
QUEUE = "/reviewer/queue"


@pytest.fixture
def reviewer_account(db: None):
    with SessionLocal() as session:
        user = ReviewerUser(
            username="test-reviewer-1",
            password_hash=hash_password("s3cret-pass"),
            display_name="Test Reviewer",
            role="reviewer",
        )
        session.add(user)
        session.commit()
        user_id = user.user_id

    yield "test-reviewer-1", "s3cret-pass"

    with SessionLocal() as session:
        session.execute(delete(ReviewerUser).where(ReviewerUser.user_id == user_id))
        session.commit()


@pytest.fixture
def fa_account(db: None):
    """A role not permitted into the review queue."""
    with SessionLocal() as session:
        user = ReviewerUser(
            username="test-fa-1",
            password_hash=hash_password("fa-pass"),
            display_name="Test FA",
            role="fa",
        )
        session.add(user)
        session.commit()
        user_id = user.user_id

    yield "test-fa-1", "fa-pass"

    with SessionLocal() as session:
        session.execute(delete(ReviewerUser).where(ReviewerUser.user_id == user_id))
        session.commit()


@pytest.fixture
def inactive_account(db: None):
    with SessionLocal() as session:
        user = ReviewerUser(
            username="test-inactive-1",
            password_hash=hash_password("inactive-pass"),
            display_name="Test Inactive",
            role="reviewer",
            active=False,
        )
        session.add(user)
        session.commit()
        user_id = user.user_id

    yield "test-inactive-1", "inactive-pass"

    with SessionLocal() as session:
        session.execute(delete(ReviewerUser).where(ReviewerUser.user_id == user_id))
        session.commit()


def test_queue_redirects_to_login_when_logged_out() -> None:
    response = client.get(QUEUE, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == LOGIN


def test_correct_login_reaches_the_queue(reviewer_account) -> None:
    username, password = reviewer_account
    with TestClient(app) as session_client:
        login = session_client.post(
            LOGIN, data={"username": username, "password": password}, follow_redirects=False
        )
        assert login.status_code == 303
        assert login.headers["location"] == QUEUE

        queue = session_client.get(QUEUE)
        assert queue.status_code == 200


def test_wrong_password_is_rejected_without_hinting_which_field(reviewer_account) -> None:
    username, _password = reviewer_account
    response = client.post(LOGIN, data={"username": username, "password": "not-it"})
    assert response.status_code == 401
    assert "invalid username or password" in response.text.lower()


def test_unknown_username_gets_the_same_error(reviewer_account) -> None:
    response = client.post(LOGIN, data={"username": "no-such-user", "password": "anything"})
    assert response.status_code == 401
    assert "invalid username or password" in response.text.lower()


def test_inactive_account_cannot_log_in(inactive_account) -> None:
    username, password = inactive_account
    response = client.post(LOGIN, data={"username": username, "password": password})
    assert response.status_code == 401


def test_wrong_role_is_forbidden_from_the_queue(fa_account) -> None:
    username, password = fa_account
    with TestClient(app) as session_client:
        session_client.post(LOGIN, data={"username": username, "password": password})
        response = session_client.get(QUEUE)
        assert response.status_code == 403


def test_logout_clears_the_session(reviewer_account) -> None:
    username, password = reviewer_account
    with TestClient(app) as session_client:
        session_client.post(LOGIN, data={"username": username, "password": password})
        assert session_client.get(QUEUE).status_code == 200

        session_client.post("/reviewer/logout")
        response = session_client.get(QUEUE, follow_redirects=False)
        assert response.status_code == 303
