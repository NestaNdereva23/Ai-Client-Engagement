from __future__ import annotations

import json

import httpx
import jwt
import pytest

from app.config import get_settings
from app.delivery.mailer import (
    EmailMessage,
    NullMailer,
    TicketingMailer,
    TicketingSendError,
    get_mailer,
)

SECRET = "shared-secret-for-ticketing-tests"
MESSAGE = EmailMessage(to="tester@example.com", subject="[TEST] Hello", text_body="Body")


def _mailer(handler) -> TicketingMailer:
    return TicketingMailer(
        base_url="https://ticketing.test", secret=SECRET, transport=httpx.MockTransport(handler)
    )


def test_posts_the_email_with_a_send_token():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"sent": True, "thread_id": "thread-9"})

    result = _mailer(handler).send(MESSAGE)

    assert result.sent is True
    assert result.provider_message_id == "thread-9"
    request = seen[0]
    assert request.url == "https://ticketing.test/api/ai-outreach/send-email"
    assert json.loads(request.content) == {
        "to": "tester@example.com",
        "subject": "[TEST] Hello",
        "body": "Body",
    }
    token = request.headers["Authorization"].removeprefix("Bearer ")
    claims = jwt.decode(token, SECRET, algorithms=["HS256"], audience="ticketing-send")
    assert claims["purpose"] == "send_email"
    assert claims["exp"] - claims["iat"] <= 60


@pytest.mark.parametrize("status", [401, 422, 502, 503])
def test_a_refusal_raises_rather_than_reporting_a_send(status: int):
    mailer = _mailer(lambda _: httpx.Response(status, json={"sent": False}))
    with pytest.raises(TicketingSendError):
        mailer.send(MESSAGE)


def test_an_unreachable_ticketing_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    with pytest.raises(TicketingSendError, match="unreachable"):
        _mailer(handler).send(MESSAGE)


def test_get_mailer_picks_ticketing_when_configured():
    settings = get_settings().model_copy(
        update={
            "mail_transport": "ticketing",
            "ticketing_base_url": "https://ticketing.test",
            "ai_outreach_jwt_secret": SECRET,
        }
    )
    assert isinstance(get_mailer(settings), TicketingMailer)


@pytest.mark.parametrize(
    ("url", "secret", "reason"),
    [
        ("", SECRET, "no ticketing url configured"),
        ("https://t.test", "", "no ticketing secret configured"),
    ],
)
def test_get_mailer_stays_quiet_when_ticketing_is_half_configured(url, secret, reason):
    settings = get_settings().model_copy(
        update={
            "mail_transport": "ticketing",
            "ticketing_base_url": url,
            "ai_outreach_jwt_secret": secret,
        }
    )
    mailer = get_mailer(settings)
    assert isinstance(mailer, NullMailer)
    assert mailer.reason == reason
