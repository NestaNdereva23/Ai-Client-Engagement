from __future__ import annotations

from dataclasses import dataclass, field
from uuid import uuid4

from sqlalchemy import delete
from test_api_campaigns import accepted_state, make_settings

from app.config import get_settings
from app.db.models.campaigns import Enrollment, TouchLog
from app.db.models.llmops import GenerationRun
from app.db.models.models import Clients, Funds, PiiVault
from app.db.models.outreach import Campaign, OutreachMessage
from app.db.session import SessionLocal, restricted_session
from app.delivery.dispatch import dispatch_campaign
from app.delivery.mailer import EmailMessage, SendResult
from app.delivery.sms_gateway import SmsMessage, SmsSendResult
from app.llmops.versions import persist_generation_run

FUND_ID = 97706
CLIENT_IDS = (97950, 97951, 97952)


@dataclass
class FakeMailer:
    sent_messages: list[EmailMessage] = field(default_factory=list)

    def send(self, message: EmailMessage) -> SendResult:
        self.sent_messages.append(message)
        return SendResult(sent=True, sender="fake", recipient=message.to, subject=message.subject)

    def close(self) -> None:
        pass


@dataclass
class FakeSmsGateway:
    sent_messages: list[SmsMessage] = field(default_factory=list)

    def send(self, message: SmsMessage) -> SmsSendResult:
        self.sent_messages.append(message)
        return SmsSendResult(sent=True, sender="fake", recipient=message.to, body=message.body)


def _direct_send_settings(**overrides):
    return get_settings().model_copy(update={"dispatch_direct_send": True, **overrides})


def _make_touch(session, *, client_id: int, campaign_id: int, contact_email: str) -> int:
    session.add(
        Clients(
            client_id=client_id, unit_fund_id=FUND_ID, n_purchases_returned=0, n_sales_returned=0
        )
    )
    session.commit()

    with restricted_session() as vault_session:
        vault_session.add(
            PiiVault(client_id=client_id, client_name="Test Client", contact_email=contact_email)
        )
        vault_session.commit()

    enrollment = Enrollment(campaign_id=campaign_id, client_id=client_id)
    session.add(enrollment)
    session.commit()

    message_run = persist_generation_run(session, accepted_state(client_id), make_settings())
    message = OutreachMessage(
        message_id=uuid4().hex,
        campaign_id=campaign_id,
        generation_run_id=message_run.run_id,
        client_id=client_id,
        ai_draft_content={"subject": "Subject", "body": "Body"},
        personalized_content={"subject": "Subject", "body": "Body"},
        status="approved",
    )
    session.add(message)
    session.commit()

    touch = TouchLog(
        enrollment_id=enrollment.enrollment_id, step_no=1, message_id=message.message_id
    )
    session.add(touch)
    session.commit()
    return touch.touch_id


def _cleanup(client_ids: tuple[int, ...]) -> None:
    with SessionLocal() as session:
        for client_id in client_ids:
            session.execute(
                delete(TouchLog).where(
                    TouchLog.enrollment_id.in_(
                        session.query(Enrollment.enrollment_id).filter(
                            Enrollment.client_id == client_id
                        )
                    )
                )
            )
        session.execute(delete(OutreachMessage).where(OutreachMessage.client_id.in_(client_ids)))
        session.execute(delete(GenerationRun).where(GenerationRun.client_id.in_(client_ids)))
        session.execute(delete(Enrollment).where(Enrollment.client_id.in_(client_ids)))
        session.execute(delete(Clients).where(Clients.client_id.in_(client_ids)))
        session.execute(delete(Funds).where(Funds.unit_fund_id == FUND_ID))
        session.commit()
    with restricted_session() as session:
        session.execute(delete(PiiVault).where(PiiVault.client_id.in_(client_ids)))
        session.commit()


def test_direct_send_delivers_immediately_and_returns_no_deliveries(db: None):
    _cleanup(CLIENT_IDS)
    with SessionLocal() as session:
        campaign = Campaign(name="direct send test")
        session.add(campaign)
        session.add(Funds(unit_fund_id=FUND_ID, unit_fund_name="Test Fund"))
        session.commit()
        campaign_id = campaign.campaign_id
        _make_touch(
            session, client_id=CLIENT_IDS[0], campaign_id=campaign_id, contact_email="a@example.com"
        )

    try:
        mailer = FakeMailer()
        outcomes, deliveries = dispatch_campaign(
            SessionLocal(), campaign_id, settings=_direct_send_settings(), mailer=mailer
        )

        assert [o.sent for o in outcomes] == [True]
        assert deliveries == []
        assert len(mailer.sent_messages) == 1
        assert mailer.sent_messages[0].to == "a@example.com"
    finally:
        _cleanup((CLIENT_IDS[0],))


def test_direct_send_clamps_to_the_configured_batch_size(db: None):
    _cleanup(CLIENT_IDS)
    with SessionLocal() as session:
        campaign = Campaign(name="direct send clamp test")
        session.add(campaign)
        session.add(Funds(unit_fund_id=FUND_ID, unit_fund_name="Test Fund"))
        session.commit()
        campaign_id = campaign.campaign_id
        for i, client_id in enumerate(CLIENT_IDS):
            _make_touch(
                session,
                client_id=client_id,
                campaign_id=campaign_id,
                contact_email=f"c{i}@example.com",
            )

    try:
        mailer = FakeMailer()
        outcomes, deliveries = dispatch_campaign(
            SessionLocal(),
            campaign_id,
            limit=len(CLIENT_IDS),
            settings=_direct_send_settings(dispatch_direct_send_max_batch=2),
            mailer=mailer,
        )

        assert len(outcomes) == 2
        assert deliveries == []
        assert len(mailer.sent_messages) == 2
    finally:
        _cleanup(CLIENT_IDS)


def test_default_dispatch_still_captures_instead_of_sending(db: None):
    _cleanup((CLIENT_IDS[0],))
    with SessionLocal() as session:
        campaign = Campaign(name="captured dispatch test")
        session.add(campaign)
        session.add(Funds(unit_fund_id=FUND_ID, unit_fund_name="Test Fund"))
        session.commit()
        campaign_id = campaign.campaign_id
        _make_touch(
            session, client_id=CLIENT_IDS[0], campaign_id=campaign_id, contact_email="b@example.com"
        )

    try:
        settings = get_settings().model_copy(update={"dispatch_direct_send": False})
        outcomes, deliveries = dispatch_campaign(SessionLocal(), campaign_id, settings=settings)

        assert [o.sent for o in outcomes] == [True]
        assert [(d.channel, d.to) for d in deliveries] == [("email", "b@example.com")]
        assert deliveries[0].cc == ()
    finally:
        _cleanup((CLIENT_IDS[0],))


def test_dispatch_hands_back_the_account_manager_to_copy(db: None, monkeypatch):
    _cleanup((CLIENT_IDS[0],))
    with SessionLocal() as session:
        campaign = Campaign(name="account manager copy test")
        session.add(campaign)
        session.add(Funds(unit_fund_id=FUND_ID, unit_fund_name="Test Fund"))
        session.commit()
        campaign_id = campaign.campaign_id
        _make_touch(
            session, client_id=CLIENT_IDS[0], campaign_id=campaign_id, contact_email="c@example.com"
        )
    monkeypatch.setattr(
        "app.delivery.sender._advisor_emails", lambda client_id: ("fa@example.com",)
    )

    try:
        settings = get_settings().model_copy(update={"dispatch_direct_send": False})
        _, deliveries = dispatch_campaign(SessionLocal(), campaign_id, settings=settings)

        assert [(d.to, d.cc) for d in deliveries] == [("c@example.com", ("fa@example.com",))]
    finally:
        _cleanup((CLIENT_IDS[0],))
