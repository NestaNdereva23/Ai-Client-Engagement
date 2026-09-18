from __future__ import annotations

import httpx
import pytest

from app.config import Settings
from app.delivery.sms_gateway import (
    AfricasTalkingGateway,
    RecordingSmsGateway,
    SmsGateway,
    SmsMessage,
    get_sms_gateway,
)


def a_message() -> SmsMessage:
    return SmsMessage(to="+254712345678", body="Your fund is open again. Reply for details.")


def test_factory_returns_recording_gateway_without_credentials():
    built = get_sms_gateway(
        Settings(sms_transport="africastalking", sms_provider_api_key="", sms_provider_username="")
    )
    assert isinstance(built, RecordingSmsGateway)


def test_factory_returns_recording_gateway_without_a_sender_id():
    built = get_sms_gateway(
        Settings(
            sms_transport="africastalking",
            sms_provider_api_key="key",
            sms_provider_username="user",
            sms_sender_id="",
        )
    )
    assert isinstance(built, RecordingSmsGateway)
    assert built.reason == "no sender id configured"


def test_factory_returns_africas_talking_gateway_when_configured():
    built = get_sms_gateway(
        Settings(
            sms_transport="africastalking",
            sms_provider_api_key="key",
            sms_provider_username="user",
            sms_sender_id="ACE",
        )
    )
    assert isinstance(built, AfricasTalkingGateway)
    assert built.username == "user"
    assert built.sender_id == "ACE"


def test_both_implementations_satisfy_the_protocol():
    assert isinstance(RecordingSmsGateway(), SmsGateway)
    assert isinstance(
        AfricasTalkingGateway(api_key="k", username="u", sender_id="ACE", base_url="http://x"),
        SmsGateway,
    )


def test_recording_gateway_records_instead_of_sending():
    gateway = RecordingSmsGateway(sender="ACE")
    result = gateway.send(a_message())

    assert result.sent is False
    assert result.reason == "no sms provider configured"
    assert result.recipient == "+254712345678"
    assert result.parts == 1
    assert gateway.sent_messages == [a_message()]


def test_recording_gateway_close_is_a_harmless_no_op():
    RecordingSmsGateway().close()


@pytest.fixture
def fake_transport():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "SMSMessageData": {
                    "Message": "Sent",
                    "Recipients": [
                        {
                            "number": "+254712345678",
                            "status": "Success",
                            "messageId": "ATXid_123",
                            "cost": "KES 0.8000",
                            "messageParts": 1,
                        }
                    ],
                }
            },
        )

    return httpx.MockTransport(handler)


def test_africas_talking_gateway_sends_and_parses_the_provider_result(fake_transport, monkeypatch):
    gateway = AfricasTalkingGateway(
        api_key="key", username="user", sender_id="ACE", base_url="http://sms.example.com/send"
    )
    monkeypatch.setattr(
        gateway,
        "_open_client",
        lambda: httpx.Client(transport=fake_transport, headers={"apiKey": "key"}),
    )

    result = gateway.send(a_message())

    assert result.sent is True
    assert result.provider_status == "Success"
    assert result.provider_message_id == "ATXid_123"
    assert result.parts == 1
    assert result.cost == 0.8


def test_africas_talking_gateway_reuses_one_client_across_a_batch(fake_transport, monkeypatch):
    gateway = AfricasTalkingGateway(
        api_key="key", username="user", sender_id="ACE", base_url="http://sms.example.com/send"
    )
    opened = []

    def open_client():
        client = httpx.Client(transport=fake_transport, headers={"apiKey": "key"})
        opened.append(client)
        return client

    monkeypatch.setattr(gateway, "_open_client", open_client)

    gateway.send(a_message())
    gateway.send(a_message())

    assert len(opened) == 1


def test_africas_talking_gateway_close_ends_the_reused_client(fake_transport, monkeypatch):
    gateway = AfricasTalkingGateway(
        api_key="key", username="user", sender_id="ACE", base_url="http://sms.example.com/send"
    )
    monkeypatch.setattr(
        gateway,
        "_open_client",
        lambda: httpx.Client(transport=fake_transport, headers={"apiKey": "key"}),
    )
    gateway.send(a_message())
    gateway.close()
    assert gateway._client is None
