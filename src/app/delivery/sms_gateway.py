from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

import httpx
import structlog

from app.agents.guardrails import sms_part_count
from app.config import Settings, get_settings

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class SmsMessage:
    to: str
    body: str


@dataclass(frozen=True)
class SmsSendResult:
    sent: bool
    sender: str
    recipient: str
    body: str
    reason: str = ""
    provider_message_id: str | None = None
    provider_status: str | None = None
    parts: int = 0
    cost: float | None = None


@runtime_checkable
class SmsGateway(Protocol):
    def send(self, message: SmsMessage) -> SmsSendResult: ...


def _parse_cost(raw: str | None) -> float | None:
    # Africa's Talking returns cost as "KES 1.0000"; keep only the number.
    if not raw:
        return None
    digits = "".join(ch for ch in raw if ch.isdigit() or ch == ".")
    return float(digits) if digits else None


class AfricasTalkingGateway:
    # Sends over Africa's Talking, the bulk SMS provider used for Kenyan numbers.

    def __init__(
        self,
        *,
        api_key: str,
        username: str,
        sender_id: str,
        base_url: str,
        timeout: float = 30.0,
    ) -> None:
        self.api_key = api_key
        self.username = username
        self.sender_id = sender_id
        self.base_url = base_url
        self.timeout = timeout
        self._client: httpx.Client | None = None

    def _open_client(self) -> httpx.Client:
        return httpx.Client(
            timeout=self.timeout,
            headers={"apiKey": self.api_key, "Accept": "application/json"},
        )

    def send(self, message: SmsMessage) -> SmsSendResult:
        if self._client is None:
            self._client = self._open_client()
        response = self._client.post(
            self.base_url,
            data={
                "username": self.username,
                "to": message.to,
                "message": message.body,
                "from": self.sender_id,
            },
        )
        response.raise_for_status()
        recipient = response.json()["SMSMessageData"]["Recipients"][0]
        status = str(recipient.get("status", ""))
        sent = status.lower() == "success"
        logger.info(
            "sms_sent",
            recipient=message.to,
            status=status,
            provider_message_id=recipient.get("messageId"),
        )
        return SmsSendResult(
            sent=sent,
            sender=self.sender_id,
            recipient=message.to,
            body=message.body,
            reason="" if sent else status,
            provider_message_id=recipient.get("messageId"),
            provider_status=status,
            parts=int(recipient.get("messageParts") or sms_part_count(message.body)),
            cost=_parse_cost(recipient.get("cost")),
        )

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None


@dataclass
class RecordingSmsGateway:
    reason: str = "no sms provider configured"
    sender: str = ""
    sent_messages: list[SmsMessage] = field(default_factory=list)

    def send(self, message: SmsMessage) -> SmsSendResult:
        self.sent_messages.append(message)
        logger.info("sms_not_sent", recipient=message.to, reason=self.reason)
        return SmsSendResult(
            sent=False,
            sender=self.sender,
            recipient=message.to,
            body=message.body,
            reason=self.reason,
            parts=sms_part_count(message.body),
        )

    def close(self) -> None:
        pass  # present so a caller can close() any SmsGateway without checking which kind it got


def get_sms_gateway(settings: Settings | None = None) -> SmsGateway:
    settings = settings or get_settings()
    if not settings.sms_provider_api_key or not settings.sms_provider_username:
        return RecordingSmsGateway(sender=settings.sms_sender_id)
    if not settings.sms_sender_id:
        return RecordingSmsGateway(reason="no sender id configured", sender="")
    return AfricasTalkingGateway(
        api_key=settings.sms_provider_api_key,
        username=settings.sms_provider_username,
        sender_id=settings.sms_sender_id,
        base_url=settings.sms_provider_base_url,
        timeout=settings.sms_timeout_seconds,
    )
