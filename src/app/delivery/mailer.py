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
from dataclasses import dataclass, field
from email.message import EmailMessage as MimeMessage
from typing import Protocol, runtime_checkable

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

    def send(self, message: EmailMessage) -> SendResult:
        mime = build_mime_message(self.sender, message)
        with smtplib.SMTP(self.host, self.port, timeout=self.timeout) as client:
            if self.starttls:
                client.starttls()
            if self.username and self.password:
                client.login(self.username, self.password)
            client.send_message(mime)
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


def get_mailer(settings: Settings | None = None) -> Mailer:
    """Build the configured Mailer. The one place an implementation is
    chosen; downstream code only ever depends on the protocol.

    No SMTP host, or a host with no sender address, gives NullMailer: an
    environment that cannot send should record and stay quiet rather than
    raise in the middle of a scheduled run.
    """
    settings = settings or get_settings()
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
