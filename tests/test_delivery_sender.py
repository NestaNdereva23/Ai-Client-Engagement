"""Tests for delivery.sender.build_email_sender: the one real SenderFn,
addressing an approved outreach_message and handing it to a Mailer.

No test here opens a real socket: the Mailer is always either NullMailer or
a small fake standing in for a configured provider.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from sqlalchemy import delete

from app.campaigns.touch import SendBlocked
from app.db.models.active_clients import ActiveClientFund
from app.db.models.models import ClientFund, Clients, Funds, PiiVault
from app.db.models.outreach import OutreachMessage
from app.db.session import SessionLocal, restricted_session
from app.delivery.mailer import EmailMessage, NullMailer
from app.delivery.mailer import SendResult as MailerSendResult
from app.delivery.sender import build_email_sender

CLIENT_ID = 9977801
INACTIVE_FUND_IDS = (9977811, 9977812)


def a_message(**overrides) -> OutreachMessage:
    defaults = dict(
        message_id="msg-1",
        campaign_id=1,
        generation_run_id="run-1",
        client_id=CLIENT_ID,
        ai_draft_content={"subject": "{{first_name}}, draft subject", "body": "draft body"},
        personalized_content={"subject": "Real subject", "body": "Real body"},
    )
    defaults.update(overrides)
    return OutreachMessage(**defaults)


@dataclass
class FakeMailer:
    """Stands in for a configured provider: records what it sent and reports success."""

    sent_messages: list[EmailMessage] = field(default_factory=list)

    def send(self, message: EmailMessage) -> MailerSendResult:
        self.sent_messages.append(message)
        return MailerSendResult(
            sent=True, sender="ace@example.com", recipient=message.to, subject=message.subject
        )


@pytest.fixture
def client_with_contact(db: None):
    """One client with a contact_email on file in pii_vault."""
    with restricted_session() as session:
        session.add(PiiVault(client_id=CLIENT_ID, contact_email="client@example.com"))
        session.commit()
    yield
    with restricted_session() as session:
        session.execute(delete(PiiVault).where(PiiVault.client_id == CLIENT_ID))
        session.commit()


def test_sends_the_personalized_content_to_the_vault_contact(client_with_contact: None):
    mailer = FakeMailer()
    sender = build_email_sender(mailer)

    result = sender(a_message())

    assert result.delivery_status == "sent"
    assert len(mailer.sent_messages) == 1
    sent = mailer.sent_messages[0]
    assert sent.to == "client@example.com"
    assert sent.subject == "Real subject"
    assert sent.text_body == "Real body"
    assert sent.cc == ()


def _clear_active_funds() -> None:
    with SessionLocal() as session:
        session.execute(delete(ActiveClientFund).where(ActiveClientFund.client_id == CLIENT_ID))
        session.commit()


@pytest.fixture
def active_funds(client_with_contact: None):
    """Adds active fund rows for the client, one per (fund id, fa_email) pair given."""
    _clear_active_funds()

    def add(*funds: tuple[int, str | None]) -> None:
        with SessionLocal() as session:
            session.add_all(
                ActiveClientFund(
                    client_id=CLIENT_ID,
                    unit_fund_id=unit_fund_id,
                    n_deposits=0,
                    n_withdrawals=0,
                    fa_email=fa_email,
                )
                for unit_fund_id, fa_email in funds
            )
            session.commit()

    yield add
    _clear_active_funds()


def test_copies_the_account_manager_on_a_live_email(active_funds):
    active_funds((1, "fa.one@example.com"))
    mailer = FakeMailer()

    build_email_sender(mailer)(a_message())

    assert mailer.sent_messages[0].to == "client@example.com"
    assert mailer.sent_messages[0].cc == ("fa.one@example.com",)


def test_copies_each_account_manager_once_across_funds(active_funds):
    active_funds(
        (1, "fa.two@example.com"),
        (2, "fa.one@example.com"),
        (3, "fa.two@example.com"),
        (4, None),
    )
    mailer = FakeMailer()

    build_email_sender(mailer)(a_message())

    assert mailer.sent_messages[0].cc == ("fa.one@example.com", "fa.two@example.com")


def _clear_inactive_funds() -> None:
    with SessionLocal() as session:
        session.execute(delete(ClientFund).where(ClientFund.client_id == CLIENT_ID))
        session.execute(delete(Clients).where(Clients.client_id == CLIENT_ID))
        session.execute(delete(Funds).where(Funds.unit_fund_id.in_(INACTIVE_FUND_IDS)))
        session.commit()


@pytest.fixture
def inactive_funds(client_with_contact: None):
    """Adds inactive fund rows for the client, one per (fund id, fa_email) pair given."""
    _clear_inactive_funds()

    def add(*funds: tuple[int, str | None]) -> None:
        with SessionLocal() as session:
            session.add_all(
                Funds(unit_fund_id=fund_id, unit_fund_name="Fund") for fund_id, _ in funds
            )
            session.commit()
            session.add(
                Clients(
                    client_id=CLIENT_ID,
                    unit_fund_id=funds[0][0],
                    n_purchases_returned=0,
                    n_sales_returned=0,
                )
            )
            session.commit()
            session.add_all(
                ClientFund(
                    client_id=CLIENT_ID,
                    unit_fund_id=fund_id,
                    n_purchases=0,
                    n_sales=0,
                    fa_email=fa_email,
                )
                for fund_id, fa_email in funds
            )
            session.commit()

    yield add
    _clear_inactive_funds()


def test_copies_the_account_manager_of_an_inactive_client(inactive_funds):
    inactive_funds((INACTIVE_FUND_IDS[0], "fa.inactive@example.com"))
    mailer = FakeMailer()

    build_email_sender(mailer)(a_message())

    assert mailer.sent_messages[0].cc == ("fa.inactive@example.com",)


def test_copies_each_account_manager_once_across_both_books(inactive_funds, active_funds):
    inactive_funds(
        (INACTIVE_FUND_IDS[0], "fa.two@example.com"),
        (INACTIVE_FUND_IDS[1], "fa.one@example.com"),
    )
    active_funds((1, "fa.two@example.com"), (2, "fa.three@example.com"))
    mailer = FakeMailer()

    build_email_sender(mailer)(a_message())

    assert mailer.sent_messages[0].cc == (
        "fa.one@example.com",
        "fa.three@example.com",
        "fa.two@example.com",
    )


def test_leaves_out_an_account_manager_address_that_is_not_an_email(inactive_funds):
    inactive_funds(
        (INACTIVE_FUND_IDS[0], "gracemwende2010@gmail"),
        (INACTIVE_FUND_IDS[1], "fa.one@example.com"),
    )
    mailer = FakeMailer()

    build_email_sender(mailer)(a_message())

    assert mailer.sent_messages[0].cc == ("fa.one@example.com",)


def test_reports_recorded_when_the_mailer_is_the_recording_no_op(client_with_contact: None):
    """A no-op mailer (no SMTP host configured) still reports the touch as
    handled -- the same "recorded, not sent" status the digest email uses.
    """
    sender = build_email_sender(NullMailer(sender="ace@example.com"))

    result = sender(a_message())

    assert result.delivery_status == "recorded"


def test_blocks_a_message_with_no_personalized_content(client_with_contact: None):
    """ai_draft_content still carries placeholders like {{first_name}}; a
    message that never got personalized must never reach the mailer.
    """
    sender = build_email_sender(FakeMailer())

    with pytest.raises(SendBlocked, match="no_personalized_content"):
        sender(a_message(personalized_content=None))


def test_blocks_a_client_with_no_contact_email_on_file(db: None):
    sender = build_email_sender(FakeMailer())

    with pytest.raises(SendBlocked, match="no_deliverable_contact"):
        sender(a_message(client_id=CLIENT_ID))
