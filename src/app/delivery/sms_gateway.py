from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

import httpx
import structlog

from app.agents.guardrails import sms_part_count
from app.config import Settings, get_settings
from app.delivery.mailer import TicketingSendError, ticketing_send_token

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


TICKETING_SMS_PATH = "/api/ai-outreach/send-sms"


class TicketingSmsGateway:
    # Sends through the Ticketing app, which hands the text to its own SMS provider.

    def __init__(
        self,
        *,
        base_url: str,
        secret: str,
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.secret = secret
        self.timeout = timeout
        self.transport = transport
        self._client: httpx.Client | None = None

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                base_url=self.base_url, timeout=self.timeout, transport=self.transport
            )
        return self._client

    def send(self, message: SmsMessage) -> SmsSendResult:
        token = ticketing_send_token(self.secret, "send_sms")
        try:
            response = self._http().post(
                TICKETING_SMS_PATH,
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                json={"to": message.to, "message": message.body},
            )
        except httpx.HTTPError as exc:
            raise TicketingSendError(f"ticketing unreachable: {exc}") from exc
        if response.status_code != 200:
            raise TicketingSendError(f"ticketing refused the sms: {response.status_code}")
        logger.info("sms_sent", recipient=message.to, via="ticketing")
        return SmsSendResult(
            sent=True,
            sender="ticketing",
            recipient=message.to,
            body=message.body,
            parts=sms_part_count(message.body),
        )

    def close(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            client.close()


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
    return _ticketing_gateway(settings)


def _ticketing_gateway(settings: Settings) -> SmsGateway:
    if not settings.ticketing_base_url:
        return RecordingSmsGateway(reason="no ticketing url configured")
    if not settings.ai_outreach_jwt_secret:
        return RecordingSmsGateway(reason="no ticketing secret configured")
    return TicketingSmsGateway(
        base_url=settings.ticketing_base_url,
        secret=settings.ai_outreach_jwt_secret,
        timeout=settings.ticketing_timeout_seconds,
    )
