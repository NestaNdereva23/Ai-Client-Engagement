"""Sending mail: one interface, an SMTP sender, and a recording no-op.

Everything that sends mail depends on the Mailer protocol, never on a
concrete class, so development can point at a local inbox and production at
a real server with no change to the caller. get_mailer is the one place an
implementation is chosen.

An environment with no SMTP host configured gets NullMailer, which records
what it was asked to send and sends nothing. A morning job that runs against
a half-configured environment should go quiet, not crash.
"""

from __future__ import annotations

import smtplib
import time
from dataclasses import dataclass, field
from email.message import EmailMessage as MimeMessage
from typing import Protocol, runtime_checkable

import httpx
import jwt
import structlog

from app.config import Settings, get_settings

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class EmailMessage:
    """One message to send. Plain text is required; HTML is optional and is
    added as an alternative part so a text-only reader still sees the body.
    """

    to: str
    subject: str
    text_body: str
    html_body: str | None = None


@dataclass(frozen=True)
class SendResult:
    """What happened to one send attempt.

    sent is False when nothing left the process, and reason says why. A
    caller that records or audits the attempt reads the same shape either
    way, so a recorded no-op is as visible as a real send.
    """

    sent: bool
    sender: str
    recipient: str
    subject: str
    reason: str = ""
    provider_message_id: str | None = None


@runtime_checkable
class Mailer(Protocol):
    def send(self, message: EmailMessage) -> SendResult: ...


def build_mime_message(sender: str, message: EmailMessage) -> MimeMessage:
    """Turn an EmailMessage into the MIME message that goes on the wire."""
    mime = MimeMessage()
    mime["From"] = sender
    mime["To"] = message.to
    mime["Subject"] = message.subject
    mime.set_content(message.text_body)
    if message.html_body is not None:
        mime.add_alternative(message.html_body, subtype="html")
    return mime


class SmtpMailer:
    """Sends over SMTP. The same class serves Mailpit in development (host
    and port, no credentials, no TLS) and a real server in production (the
    same fields, filled in).

    A batch send (a whole campaign, a whole digest run) calls send() many
    times on the same SmtpMailer instance, so the connection opened for the
    first message is kept open and reused for the rest instead of
    reconnecting per message -- opening a fresh TCP connection for every one
    of thousands of emails is what was timing out against Mailpit locally.
    If the server drops the reused connection (an idle timeout, a
    per-connection message cap), send() reconnects once and retries before
    giving up. Call close() once a batch is done.
    """

    def __init__(
        self,
        *,
        host: str,
        port: int,
        sender: str,
        username: str = "",
        password: str = "",
        starttls: bool = False,
        timeout: float = 30.0,
    ) -> None:
        self.host = host
        self.port = port
        self.sender = sender
        self.username = username
        self.password = password
        self.starttls = starttls
        self.timeout = timeout
        self._client: smtplib.SMTP | None = None

    def _open_connection(self) -> smtplib.SMTP:
        client = smtplib.SMTP(self.host, self.port, timeout=self.timeout)
        if self.starttls:
            client.starttls()
        if self.username and self.password:
            client.login(self.username, self.password)
        return client

    def send(self, message: EmailMessage) -> SendResult:
        mime = build_mime_message(self.sender, message)
        if self._client is None:
            self._client = self._open_connection()
        try:
            self._client.send_message(mime)
        except (smtplib.SMTPException, OSError):
            self._discard_connection()
            self._client = self._open_connection()
            self._client.send_message(mime)
        logger.info(
            "email_sent",
            recipient=message.to,
            subject=message.subject,
            smtp_host=self.host,
        )
        return SendResult(
            sent=True,
            sender=self.sender,
            recipient=message.to,
            subject=message.subject,
        )

    def close(self) -> None:
        """Close the connection reused across a batch of sends, if one is
        open. The next send() after this opens a fresh one.
        """
        self._discard_connection()

    def _discard_connection(self) -> None:
        client, self._client = self._client, None
        if client is None:
            return
        try:
            client.quit()
        except (smtplib.SMTPException, OSError):
            try:
                client.close()
            except OSError:
                pass


class TicketingSendError(RuntimeError):
    pass


TICKETING_SEND_AUDIENCE = "ticketing-send"
TICKETING_EMAIL_PATH = "/api/ai-outreach/send-email"
TICKETING_TOKEN_TTL_SECONDS = 60


def ticketing_send_token(secret: str, purpose: str) -> str:
    """A short lived token that lets Ticketing send one kind of message for ACE."""
    now = int(time.time())
    claims = {
        "iss": "ace",
        "aud": TICKETING_SEND_AUDIENCE,
        "purpose": purpose,
        "iat": now,
        "exp": now + TICKETING_TOKEN_TTL_SECONDS,
    }
    return jwt.encode(claims, secret, algorithm="HS256")


class TicketingMailer:
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

    def send(self, message: EmailMessage) -> SendResult:
        try:
            response = self._http().post(
                TICKETING_EMAIL_PATH,
                headers={
                    "Authorization": f"Bearer {ticketing_send_token(self.secret, 'send_email')}",
                    "Accept": "application/json",
                },
                json={"to": message.to, "subject": message.subject, "body": message.text_body},
            )
        except httpx.HTTPError as exc:
            raise TicketingSendError(f"ticketing unreachable: {exc}") from exc
        if response.status_code != 200:
            raise TicketingSendError(f"ticketing refused the email: {response.status_code}")
        thread_id = response.json().get("thread_id")
        logger.info("email_sent", recipient=message.to, subject=message.subject, via="ticketing")
        return SendResult(
            sent=True,
            sender="ticketing",
            recipient=message.to,
            subject=message.subject,
            provider_message_id=thread_id,
        )

    def close(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            client.close()


@dataclass
class NullMailer:
    """Records every message and sends none.

    Used wherever no SMTP host is configured, and by tests, which must never
    open a socket. The recorded messages are kept in memory so a caller (or a
    test) can see exactly what would have gone out.
    """

    reason: str = "no smtp host configured"
    sender: str = ""
    sent_messages: list[EmailMessage] = field(default_factory=list)

    def send(self, message: EmailMessage) -> SendResult:
        self.sent_messages.append(message)
        logger.info(
            "email_not_sent",
            recipient=message.to,
            subject=message.subject,
            reason=self.reason,
        )
        return SendResult(
            sent=False,
            sender=self.sender,
            recipient=message.to,
            subject=message.subject,
            reason=self.reason,
        )

    def close(self) -> None:
        """No connection to close; present so callers can close() any Mailer
        without checking which kind they got."""


def get_mailer(settings: Settings | None = None) -> Mailer:
    """Build the configured Mailer. The one place an implementation is
    chosen; downstream code only ever depends on the protocol.

    No SMTP host, or a host with no sender address, gives NullMailer: an
    environment that cannot send should record and stay quiet rather than
    raise in the middle of a scheduled run.
    """
    settings = settings or get_settings()
    if settings.mail_transport == "ticketing":
        return _ticketing_mailer(settings)
    if not settings.smtp_host:
        return NullMailer(sender=settings.email_sender)
    if not settings.email_sender:
        return NullMailer(
            reason="no sender address configured",
            sender="",
        )
    return SmtpMailer(
        host=settings.smtp_host,
        port=settings.smtp_port,
        sender=settings.email_sender,
        username=settings.smtp_username,
        password=settings.smtp_password,
        starttls=settings.smtp_starttls,
        timeout=settings.smtp_timeout_seconds,
    )


def _ticketing_mailer(settings: Settings) -> Mailer:
    if not settings.ticketing_base_url:
        return NullMailer(reason="no ticketing url configured")
    if not settings.ai_outreach_jwt_secret:
        return NullMailer(reason="no ticketing secret configured")
    return TicketingMailer(
        base_url=settings.ticketing_base_url,
        secret=settings.ai_outreach_jwt_secret,
        timeout=settings.ticketing_timeout_seconds,
    )
