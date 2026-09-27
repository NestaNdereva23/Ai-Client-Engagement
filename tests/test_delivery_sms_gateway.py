from __future__ import annotations

from app.delivery.sms_gateway import RecordingSmsGateway, SmsMessage


def a_message() -> SmsMessage:
    return SmsMessage(to="+254712345678", body="Your fund is open again. Reply for details.")


def test_recording_gateway_records_instead_of_sending():
    gateway = RecordingSmsGateway(sender="ACE")
    result = gateway.send(a_message())

    assert result.sent is False
    assert result.reason == "no sms provider configured"
    assert result.recipient == "+254712345678"
    assert result.parts == 1
    assert gateway.sent_messages == [a_message()]
