from __future__ import annotations

import json

import httpx
import jwt
import pytest

from app.config import get_settings
from app.delivery.mailer import TicketingSendError
from app.delivery.sms_gateway import (
    RecordingSmsGateway,
    SmsMessage,
    TicketingSmsGateway,
    get_sms_gateway,
)

SECRET = "shared-secret-for-ticketing-tests"
MESSAGE = SmsMessage(to="+254700000001", body="Hello from Cytonn")


def _gateway(handler) -> TicketingSmsGateway:
    return TicketingSmsGateway(
        base_url="https://ticketing.test", secret=SECRET, transport=httpx.MockTransport(handler)
    )


def test_posts_the_sms_with_a_send_token():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"sent": True})

    result = _gateway(handler).send(MESSAGE)

    assert result.sent is True
    assert result.parts == 1
    request = seen[0]
    assert request.url == "https://ticketing.test/api/ai-outreach/send-sms"
    assert json.loads(request.content) == {"to": "+254700000001", "message": "Hello from Cytonn"}
    token = request.headers["Authorization"].removeprefix("Bearer ")
    claims = jwt.decode(token, SECRET, algorithms=["HS256"], audience="ticketing-send")
    assert claims["purpose"] == "send_sms"


@pytest.mark.parametrize("status", [401, 422, 502])
def test_a_refusal_raises_rather_than_reporting_a_send(status: int):
    gateway = _gateway(lambda _: httpx.Response(status, json={"sent": False}))
    with pytest.raises(TicketingSendError):
        gateway.send(MESSAGE)


def test_an_unreachable_ticketing_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    with pytest.raises(TicketingSendError, match="unreachable"):
        _gateway(handler).send(MESSAGE)


def test_get_sms_gateway_picks_ticketing_when_configured():
    settings = get_settings().model_copy(
        update={
            "sms_transport": "ticketing",
            "ticketing_base_url": "https://ticketing.test",
            "ai_outreach_jwt_secret": SECRET,
        }
    )
    assert isinstance(get_sms_gateway(settings), TicketingSmsGateway)


@pytest.mark.parametrize(
    ("url", "secret", "reason"),
    [
        ("", SECRET, "no ticketing url configured"),
        ("https://t.test", "", "no ticketing secret configured"),
    ],
)
def test_get_sms_gateway_stays_quiet_when_ticketing_is_half_configured(url, secret, reason):
    settings = get_settings().model_copy(
        update={
            "sms_transport": "ticketing",
            "ticketing_base_url": url,
            "ai_outreach_jwt_secret": secret,
        }
    )
    gateway = get_sms_gateway(settings)
    assert isinstance(gateway, RecordingSmsGateway)
    assert gateway.reason == reason
