"""Tests for the mailer contract, the factory's choice, and message building.

No test here opens a socket: SmtpMailer's send is exercised against a fake
smtplib.SMTP, and every other path runs through NullMailer.
"""

from __future__ import annotations

import smtplib

import pytest

from app.config import Settings
from app.delivery import mailer as mailer_module
from app.delivery.mailer import (
    EmailMessage,
    Mailer,
    NullMailer,
    SmtpMailer,
    build_mime_message,
    get_mailer,
)


class FakeSmtp:
    """Stands in for smtplib.SMTP and records what it was asked to do.

    fail_next_send makes exactly one send_message call raise, so tests can
    check that SmtpMailer reconnects and retries instead of giving up.
    """

    instances: list[FakeSmtp] = []

    def __init__(self, host, port, timeout=None):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.started_tls = False
        self.login_args = None
        self.messages = []
        self.closed = False
        self.fail_next_send = False
        FakeSmtp.instances.append(self)

    def starttls(self):
        self.started_tls = True

    def login(self, username, password):
        self.login_args = (username, password)

    def send_message(self, message):
        if self.fail_next_send:
            self.fail_next_send = False
            raise smtplib.SMTPServerDisconnected("Connection unexpectedly closed")
        self.messages.append(message)

    def quit(self):
        self.closed = True

    def close(self):
        self.closed = True


@pytest.fixture
def fake_smtp(monkeypatch):
    FakeSmtp.instances = []
    monkeypatch.setattr(mailer_module.smtplib, "SMTP", FakeSmtp)
    return FakeSmtp


def a_message() -> EmailMessage:
    return EmailMessage(
        to="fa@example.com",
        subject="Your morning call list",
        text_body="Six clients to call today.",
    )


def test_factory_returns_null_mailer_without_a_host():
    built = get_mailer(Settings(smtp_host="", email_sender="ace@example.com"))
    assert isinstance(built, NullMailer)


def test_factory_returns_null_mailer_without_a_sender():
    built = get_mailer(Settings(smtp_host="localhost", email_sender=""))
    assert isinstance(built, NullMailer)
    assert built.reason == "no sender address configured"


def test_factory_returns_smtp_mailer_when_configured():
    built = get_mailer(
        Settings(
            smtp_host="localhost",
            smtp_port=1025,
            email_sender="ace@example.com",
            smtp_username="user",
            smtp_password="secret",
            smtp_starttls=True,
        )
    )
    assert isinstance(built, SmtpMailer)
    assert (built.host, built.port) == ("localhost", 1025)
    assert built.sender == "ace@example.com"
    assert built.username == "user"
    assert built.password == "secret"
    assert built.starttls is True


def test_both_implementations_satisfy_the_protocol():
    assert isinstance(NullMailer(), Mailer)
    assert isinstance(SmtpMailer(host="h", port=25, sender="ace@example.com"), Mailer)


def test_null_mailer_records_instead_of_sending():
    null = NullMailer(sender="ace@example.com")
    result = null.send(a_message())

    assert result.sent is False
    assert result.reason == "no smtp host configured"
    assert result.recipient == "fa@example.com"
    assert result.subject == "Your morning call list"
    assert [m.subject for m in null.sent_messages] == ["Your morning call list"]


def test_null_mailer_close_is_a_harmless_no_op():
    """A caller closing whatever Mailer it got, without checking which kind,
    must not fail against the no-op.
    """
    NullMailer(sender="ace@example.com").close()


def test_a_misconfigured_environment_sends_nothing_and_still_records():
    """An unset SMTP_HOST must not raise on a scheduled run; it records."""
    built = get_mailer(Settings(smtp_host=""))
    result = built.send(a_message())

    assert result.sent is False
    assert built.sent_messages == [a_message()]


def test_build_mime_message_carries_sender_recipient_and_body():
    mime = build_mime_message("ace@example.com", a_message())

    assert mime["From"] == "ace@example.com"
    assert mime["To"] == "fa@example.com"
    assert mime["Subject"] == "Your morning call list"
    assert mime.get_content_type() == "text/plain"
    assert "Six clients to call today." in mime.get_content()


def test_build_mime_message_adds_html_as_an_alternative():
    mime = build_mime_message(
        "ace@example.com",
        EmailMessage(
            to="fa@example.com",
            subject="Your morning call list",
            text_body="Six clients to call today.",
            html_body="<p>Six clients to call today.</p>",
        ),
    )

    assert mime.get_content_type() == "multipart/alternative"
    subtypes = [part.get_content_subtype() for part in mime.iter_parts()]
    assert subtypes == ["plain", "html"]


def test_smtp_mailer_sends_the_message_it_was_asked_for(fake_smtp):
    sender = SmtpMailer(host="localhost", port=1025, sender="ace@example.com")
    result = sender.send(a_message())

    assert result.sent is True
    assert len(fake_smtp.instances) == 1
    client = fake_smtp.instances[0]
    assert (client.host, client.port) == ("localhost", 1025)
    assert client.started_tls is False
    assert client.login_args is None
    assert len(client.messages) == 1
    mime = client.messages[0]
    assert mime["To"] == "fa@example.com"
    assert mime["From"] == "ace@example.com"
    assert mime["Subject"] == "Your morning call list"


def test_smtp_mailer_reuses_one_connection_across_a_batch(fake_smtp):
    """A campaign send calls send() once per touch. Opening a fresh TCP
    connection for every one of thousands of emails is what was timing out
    against Mailpit; the connection opened for the first message must be
    reused for the rest.
    """
    sender = SmtpMailer(host="localhost", port=1025, sender="ace@example.com")
    for _ in range(5):
        sender.send(a_message())

    assert len(fake_smtp.instances) == 1
    assert len(fake_smtp.instances[0].messages) == 5
    assert fake_smtp.instances[0].closed is False


def test_smtp_mailer_close_ends_the_reused_connection(fake_smtp):
    sender = SmtpMailer(host="localhost", port=1025, sender="ace@example.com")
    sender.send(a_message())
    sender.close()

    assert fake_smtp.instances[0].closed is True

    # The next send after close() opens a fresh connection.
    sender.send(a_message())
    assert len(fake_smtp.instances) == 2


def test_smtp_mailer_reconnects_once_if_the_server_drops_the_connection(fake_smtp):
    """A server can drop a connection reused across a large batch (an idle
    timeout, a per-connection message cap). One retry on a fresh connection
    must recover the send rather than surfacing the disconnect.
    """
    sender = SmtpMailer(host="localhost", port=1025, sender="ace@example.com")
    sender.send(a_message())
    fake_smtp.instances[0].fail_next_send = True

    result = sender.send(a_message())

    assert result.sent is True
    assert len(fake_smtp.instances) == 2
    assert fake_smtp.instances[0].closed is True
    assert len(fake_smtp.instances[1].messages) == 1


def test_smtp_mailer_starts_tls_and_logs_in_when_configured(fake_smtp):
    sender = SmtpMailer(
        host="smtp.example.com",
        port=587,
        sender="ace@example.com",
        username="user",
        password="secret",
        starttls=True,
    )
    sender.send(a_message())

    client = fake_smtp.instances[0]
    assert client.started_tls is True
    assert client.login_args == ("user", "secret")


def test_smtp_mailer_skips_login_without_credentials(fake_smtp):
    """Mailpit takes no credentials, so a blank username must not try to log in."""
    sender = SmtpMailer(host="localhost", port=1025, sender="ace@example.com")
    sender.send(a_message())

    assert fake_smtp.instances[0].login_args is None
