from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from sqlalchemy import delete

from app.campaigns.eligibility import _vault_signals
from app.campaigns.estimation import _bulk_vault_signals
from app.campaigns.touch import SendBlocked
from app.config import get_settings
from app.db.models.models import PiiVault
from app.db.models.outreach import OutreachMessage
from app.db.models.test_recipient import TestRecipient
from app.db.session import SessionLocal, restricted_session
from app.delivery.mailer import EmailMessage
from app.delivery.mailer import SendResult as MailerSendResult
from app.delivery.sender import build_email_sender
from app.delivery.sms_gateway import SmsMessage, SmsSendResult
from app.delivery.sms_sender import build_sms_sender
from app.delivery.test_recipients import ensure_test_recipient, pick_test_recipient
from app.services.campaigns import create_campaign
from app.services.review import _resolve_first_name

CLIENT_ID = 9977951
REAL_EMAIL = "real.client@example.com"
REAL_PHONE = "+254700000951"
TESTER_EMAIL = "tester@example.com"
TESTER_PHONE = "+254711000951"


def _test_mode_settings():
    return get_settings().model_copy(update={"delivery_mode": "test"})


def a_message() -> OutreachMessage:
    return OutreachMessage(
        message_id="msg-test-mode",
        campaign_id=1,
        generation_run_id="run-1",
        client_id=CLIENT_ID,
        ai_draft_content={"subject": "s", "body": "b"},
        personalized_content={"subject": "Hello", "body": "Body"},
    )


@dataclass
class FakeMailer:
    sent_messages: list[EmailMessage] = field(default_factory=list)

    def send(self, message: EmailMessage) -> MailerSendResult:
        self.sent_messages.append(message)
        return MailerSendResult(sent=True, sender="a@b.c", recipient=message.to, subject="")


@dataclass
class FakeGateway:
    sent_messages: list[SmsMessage] = field(default_factory=list)

    def send(self, message: SmsMessage) -> SmsSendResult:
        self.sent_messages.append(message)
        return SmsSendResult(
            sent=True, sender="ACE", recipient=message.to, body=message.body, parts=1
        )


def _clear() -> None:
    with restricted_session() as session:
        session.execute(delete(TestRecipient))
        session.execute(delete(PiiVault).where(PiiVault.client_id == CLIENT_ID))
        session.commit()


@pytest.fixture
def real_client(db: None):
    _clear()
    with restricted_session() as session:
        session.add(
            PiiVault(
                client_id=CLIENT_ID,
                client_name="Real Person",
                contact_email=REAL_EMAIL,
                contact_phone=REAL_PHONE,
            )
        )
        session.commit()
    yield
    _clear()


@pytest.fixture
def tester(real_client: None):
    with restricted_session() as session:
        session.add(TestRecipient(name="Tester", email=TESTER_EMAIL, phone=TESTER_PHONE))
        session.commit()


def test_email_goes_to_the_tester_not_the_client(tester: None):
    mailer = FakeMailer()
    build_email_sender(mailer, settings=_test_mode_settings())(a_message())

    sent = mailer.sent_messages[0]
    assert sent.to == TESTER_EMAIL
    assert sent.subject == "[TEST] Hello"


def test_test_mode_never_looks_up_or_copies_an_account_manager(tester: None, monkeypatch):
    def no_advisor_lookup(client_id: int):
        raise AssertionError("test mode looked up a real account manager")

    monkeypatch.setattr("app.delivery.sender._advisor_emails", no_advisor_lookup)
    mailer = FakeMailer()
    build_email_sender(mailer, settings=_test_mode_settings())(a_message())

    assert mailer.sent_messages[0].cc == ()


def test_sms_goes_to_the_tester_not_the_client(tester: None):
    gateway = FakeGateway()
    build_sms_sender(gateway, settings=_test_mode_settings())(a_message())

    assert gateway.sent_messages[0].to == TESTER_PHONE


def test_nothing_sends_when_no_tester_is_active(real_client: None):
    mailer = FakeMailer()
    with pytest.raises(SendBlocked, match="no_test_recipient"):
        build_email_sender(mailer, settings=_test_mode_settings())(a_message())
    assert mailer.sent_messages == []


def test_an_inactive_tester_gets_nothing(real_client: None):
    with restricted_session() as session:
        session.add(TestRecipient(name="Gone", email=TESTER_EMAIL, active=False))
        session.commit()
    with pytest.raises(SendBlocked, match="no_test_recipient"):
        build_email_sender(FakeMailer(), settings=_test_mode_settings())(a_message())


def _add_three_testers() -> list[str]:
    phones = ["+254711000961", "+254711000962", "+254711000963"]
    with restricted_session() as session:
        session.add_all(TestRecipient(name=f"Tester {n}", phone=p) for n, p in enumerate(phones))
        session.commit()
    return phones


def test_testers_take_turns_so_everyone_gets_one_before_anyone_gets_two(
    real_client: None, monkeypatch
):
    phones = _add_three_testers()
    sends_so_far = iter(range(4))
    monkeypatch.setattr("app.delivery.test_recipients._sends_so_far", lambda *_: next(sends_so_far))

    picked = [pick_test_recipient(CLIENT_ID, "sms", 7) for _ in range(4)]

    assert sorted(picked[:3]) == sorted(phones)
    assert picked[3] == picked[0]


def test_a_new_campaign_does_not_always_start_at_the_same_tester(real_client: None, monkeypatch):
    _add_three_testers()
    monkeypatch.setattr("app.delivery.test_recipients._sends_so_far", lambda *_: 0)

    first_picks = {pick_test_recipient(CLIENT_ID, "sms", campaign_id) for campaign_id in (1, 2, 3)}

    assert len(first_picks) == 3


def test_final_check_refuses_an_address_off_the_list(tester: None):
    with pytest.raises(SendBlocked, match="recipient_not_on_test_list"):
        ensure_test_recipient(REAL_EMAIL, "email")
    with pytest.raises(SendBlocked, match="recipient_not_on_test_list"):
        ensure_test_recipient(REAL_PHONE, "sms")


def test_live_mode_still_uses_the_client_contact(tester: None):
    mailer = FakeMailer()
    live = get_settings().model_copy(update={"delivery_mode": "live"})
    build_email_sender(mailer, settings=live)(a_message())

    assert mailer.sent_messages[0].to == REAL_EMAIL
    assert mailer.sent_messages[0].subject == "Hello"


def _live_sms_to_test_list_settings():
    return get_settings().model_copy(
        update={"delivery_mode": "live", "live_sms_to_test_list": True}
    )


def test_live_sms_goes_to_the_tester_with_the_body_unchanged(tester: None, monkeypatch):
    def no_vault_contact(client_id: int):
        raise AssertionError("live sms read a real phone while redirected to the test list")

    monkeypatch.setattr("app.delivery.sms_sender._contact_phone", no_vault_contact)
    gateway = FakeGateway()
    result = build_sms_sender(gateway, settings=_live_sms_to_test_list_settings())(a_message())

    assert gateway.sent_messages[0].to == TESTER_PHONE
    assert gateway.sent_messages[0].body == "Body"
    assert result.recipient == TESTER_PHONE


def test_live_email_is_not_redirected_by_the_sms_switch(tester: None):
    mailer = FakeMailer()
    build_email_sender(mailer, settings=_live_sms_to_test_list_settings())(a_message())

    assert mailer.sent_messages[0].to == REAL_EMAIL


def test_live_sms_to_test_list_is_on_by_default(monkeypatch):
    monkeypatch.delenv("LIVE_SMS_TO_TEST_LIST", raising=False)
    from app.config import Settings

    assert Settings(_env_file=None).live_sms_to_test_list is True


def test_test_mode_hides_the_real_first_name(real_client: None, monkeypatch):
    monkeypatch.setenv("DELIVERY_MODE", "test")
    get_settings.cache_clear()
    try:
        assert _resolve_first_name(CLIENT_ID) == "Test Client"
    finally:
        monkeypatch.setenv("DELIVERY_MODE", "live")
        get_settings.cache_clear()
    assert _resolve_first_name(CLIENT_ID) == "Real"


def test_settings_default_to_test_mode(monkeypatch):
    monkeypatch.delenv("DELIVERY_MODE", raising=False)
    from app.config import Settings

    assert Settings(_env_file=None).delivery_mode == "test"


def test_email_final_check_blocks_an_address_that_slipped_past_the_picker(
    tester: None, monkeypatch
):
    monkeypatch.setattr("app.delivery.sender.pick_test_recipient", lambda *_: REAL_EMAIL)
    mailer = FakeMailer()
    with pytest.raises(SendBlocked, match="recipient_not_on_test_list"):
        build_email_sender(mailer, settings=_test_mode_settings())(a_message())
    assert mailer.sent_messages == []


def test_sms_final_check_blocks_a_number_that_slipped_past_the_picker(tester: None, monkeypatch):
    monkeypatch.setattr("app.delivery.sms_sender.pick_test_recipient", lambda *_: REAL_PHONE)
    gateway = FakeGateway()
    with pytest.raises(SendBlocked, match="recipient_not_on_test_list"):
        build_sms_sender(gateway, settings=_test_mode_settings())(a_message())
    assert gateway.sent_messages == []


def test_sms_in_test_mode_never_reads_the_vault_phone(tester: None, monkeypatch):
    def no_vault_contact(client_id: int):
        raise AssertionError("test mode read a real phone from the vault")

    monkeypatch.setattr("app.delivery.sms_sender._contact_phone", no_vault_contact)
    gateway = FakeGateway()
    build_sms_sender(gateway, settings=_test_mode_settings())(a_message())
    assert gateway.sent_messages[0].body == f"[TEST] Body (client {CLIENT_ID})"


@pytest.fixture
def client_without_contact(db: None):
    _clear()
    with restricted_session() as session:
        session.add(PiiVault(client_id=CLIENT_ID, client_name="Real Person"))
        session.commit()
    yield
    _clear()


def _use_mode(monkeypatch, mode: str) -> None:
    monkeypatch.setenv("DELIVERY_MODE", mode)
    get_settings.cache_clear()


def test_eligibility_uses_the_test_list_for_the_contact_check(
    client_without_contact: None, monkeypatch
):
    _use_mode(monkeypatch, "live")
    assert _vault_signals(CLIENT_ID, "email") == (False, False)

    _use_mode(monkeypatch, "test")
    assert _vault_signals(CLIENT_ID, "email") == (False, False)
    with restricted_session() as session:
        session.add(TestRecipient(name="Tester", email=TESTER_EMAIL))
        session.commit()
    assert _vault_signals(CLIENT_ID, "email") == (False, True)
    get_settings.cache_clear()


def test_eligibility_still_respects_a_real_opt_out_in_test_mode(
    client_without_contact: None, monkeypatch
):
    with restricted_session() as session:
        session.get(PiiVault, CLIENT_ID).opt_out_flag = True
        session.add(TestRecipient(name="Tester", email=TESTER_EMAIL))
        session.commit()
    _use_mode(monkeypatch, "test")
    assert _vault_signals(CLIENT_ID, "email") == (True, True)
    get_settings.cache_clear()


def test_estimate_uses_the_test_list_and_never_the_vault_contact(real_client: None, monkeypatch):
    with restricted_session() as session:
        session.add(TestRecipient(name="Tester", email=TESTER_EMAIL, phone=TESTER_PHONE))
        session.commit()
    _use_mode(monkeypatch, "test")
    assert _bulk_vault_signals([CLIENT_ID]) == {CLIENT_ID: (False, TESTER_EMAIL, TESTER_PHONE)}
    get_settings.cache_clear()


def test_a_test_campaign_enrolls_at_most_the_cap(db: None, monkeypatch):
    enrolled: list[int] = []
    monkeypatch.setenv("TEST_CAMPAIGN_MAX_CLIENTS", "2")
    get_settings.cache_clear()
    monkeypatch.setattr(
        "app.services.campaigns.resolve_cohort_client_ids", lambda *_, **__: [5, 4, 3, 2, 1]
    )
    monkeypatch.setattr(
        "app.services.campaigns.enroll_cohort",
        lambda _session, *, campaign_id, client_ids: enrolled.extend(client_ids),
    )
    with SessionLocal() as session:
        campaign, count, _ = create_campaign(
            session,
            name="cap check",
            campaign_type="dormant_reengagement",
            cohort_filters={},
            is_test=True,
        )
        assert campaign.is_test is True
        assert count == 2
        assert enrolled == [1, 2]
        session.rollback()
    get_settings.cache_clear()


def _create_with_cap(monkeypatch, env: dict[str, str], *, is_test: bool) -> tuple[int, list[int]]:
    enrolled: list[int] = []
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    monkeypatch.setattr(
        "app.services.campaigns.resolve_cohort_client_ids", lambda *_, **__: [5, 4, 3, 2, 1]
    )
    monkeypatch.setattr(
        "app.services.campaigns.enroll_cohort",
        lambda _session, *, campaign_id, client_ids: enrolled.extend(client_ids),
    )
    with SessionLocal() as session:
        _, count, _ = create_campaign(
            session,
            name="live cap check",
            campaign_type="dormant_reengagement",
            cohort_filters={},
            is_test=is_test,
        )
        session.rollback()
    get_settings.cache_clear()
    return count, enrolled


def test_a_live_campaign_enrolls_at_most_the_live_cap(db: None, monkeypatch):
    count, enrolled = _create_with_cap(
        monkeypatch, {"LIVE_CAMPAIGN_MAX_CLIENTS": "3"}, is_test=False
    )
    assert count == 3
    assert enrolled == [1, 2, 3]


def test_a_live_campaign_enrolls_everyone_when_the_live_cap_is_zero(db: None, monkeypatch):
    count, enrolled = _create_with_cap(
        monkeypatch, {"LIVE_CAMPAIGN_MAX_CLIENTS": "0"}, is_test=False
    )
    assert count == 5
    assert sorted(enrolled) == [1, 2, 3, 4, 5]
