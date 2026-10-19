from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select, update

from app.config import Settings
from app.db.models.active_clients import (
    ActiveClientFund,
    ActiveClientInteraction,
    ActiveClientTouchpoint,
)
from app.db.models.audit import AuditLog
from app.db.models.fa_assignment import FaAssignment
from app.db.models.routing import ClientSide
from app.db.models.user_access import (
    ROLE_ADMIN,
    ROLE_ADVISOR,
    SOURCE_AUTO,
    SOURCE_MANUAL,
    UserAccess,
)
from app.db.session import SessionLocal
from app.main import app
from app.services.user_access import advisor_fa_id, digest_recipients, link_advisors_from_feed

client = TestClient(app)

FUND = 9991
ASHA_CLIENT = 99910
BRIAN_CLIENT = 99911
CLIENTS = [ASHA_CLIENT, BRIAN_CLIENT]

ASHA = "access-asha@example.test"
BRIAN = "access-brian@example.test"
ADMIN = "access-admin@example.test"
GONE = "access-gone@example.test"
NEW_ADVISOR = "access-new@example.test"
STRANGER = "access-stranger@example.test"
ALL_EMAILS = [ASHA, BRIAN, ADMIN, GONE, NEW_ADVISOR, STRANGER]

ROSTER = "/api/v1/active-clients"


@pytest.fixture(autouse=True)
def _signed_tokens(configured_reviewers):
    yield


def _clear(session) -> None:
    session.execute(
        delete(ActiveClientTouchpoint).where(ActiveClientTouchpoint.client_id.in_(CLIENTS))
    )
    session.execute(
        delete(ActiveClientInteraction).where(ActiveClientInteraction.client_id.in_(CLIENTS))
    )
    session.execute(delete(FaAssignment).where(FaAssignment.client_id.in_(CLIENTS)))
    session.execute(delete(ClientSide).where(ClientSide.client_id.in_(CLIENTS)))
    session.execute(delete(ActiveClientFund).where(ActiveClientFund.client_id.in_(CLIENTS)))
    session.execute(delete(UserAccess).where(UserAccess.email.in_(ALL_EMAILS)))
    session.commit()


@pytest.fixture
def people(db):
    """Two advisors each with one client, an admin, and an advisor who has left."""
    with SessionLocal() as session:
        _clear(session)
        session.add_all(
            [
                UserAccess(
                    email=ASHA,
                    role=ROLE_ADVISOR,
                    fa_id="acc-fa-asha",
                    receives_digest=True,
                    ai_assistant=True,
                ),
                UserAccess(email=BRIAN, role=ROLE_ADVISOR, fa_id="acc-fa-brian"),
                UserAccess(email=ADMIN, role=ROLE_ADMIN),
                UserAccess(email=GONE, role=ROLE_ADVISOR, fa_id="acc-fa-gone", active=False),
            ]
        )
        for client_id, fa_id in ((ASHA_CLIENT, "acc-fa-asha"), (BRIAN_CLIENT, "acc-fa-brian")):
            session.add(
                ActiveClientFund(
                    client_id=client_id,
                    unit_fund_id=FUND,
                    client_code=f"C{client_id}",
                    balance=10_000.0,
                    n_deposits=1,
                    n_withdrawals=0,
                )
            )
            session.add(
                FaAssignment(client_id=client_id, unit_fund_id=FUND, fa_id=fa_id, source="feed")
            )
        session.commit()
    yield
    with SessionLocal() as session:
        _clear(session)


def _as(headers_for, email: str) -> dict[str, str]:
    return headers_for(email)


def _roster_clients(headers: dict[str, str], **params) -> set[int]:
    response = client.get(ROSTER, params={"limit": 200, **params}, headers=headers)
    assert response.status_code == 200, response.text
    return {row["client_id"] for row in response.json()["items"]} & set(CLIENTS)


def test_an_advisor_gets_their_own_book_whatever_the_request_says(people, headers_for) -> None:
    asha = _as(headers_for, ASHA)

    assert _roster_clients(asha) == {ASHA_CLIENT}
    assert _roster_clients(asha, fa_id="acc-fa-brian") == {ASHA_CLIENT}
    book = client.get("/api/v1/fa-action/acc-fa-brian", headers=asha)
    assert book.status_code == 200
    assert book.json()["fa_id"] == "acc-fa-asha"


def test_an_advisor_cannot_open_another_advisors_client(people, headers_for) -> None:
    asha = _as(headers_for, ASHA)

    own = client.get(f"{ROSTER}/{ASHA_CLIENT}/{FUND}/transactions", headers=asha)
    other = client.get(f"{ROSTER}/{BRIAN_CLIENT}/{FUND}/transactions", headers=asha)
    logged = client.post(
        f"{ROSTER}/{BRIAN_CLIENT}/{FUND}/interactions",
        json={"type": "call_logged", "outcome": "connected"},
        headers=asha,
    )

    assert own.status_code == 200
    assert other.status_code == 403
    assert logged.status_code == 403


def test_an_unknown_or_inactive_account_sees_no_client_data(people, headers_for) -> None:
    for email in (STRANGER, GONE):
        headers = _as(headers_for, email)
        assert client.get(ROSTER, headers=headers).status_code == 403
        assert client.get("/api/v1/fa-action/acc-fa-asha", headers=headers).status_code == 403
        assert client.get("/api/v1/access/me", headers=headers).status_code == 403


def test_the_ai_assistant_needs_an_admin_or_the_box(people, headers_for) -> None:
    for email, open_to_them in ((ADMIN, True), (ASHA, True), (BRIAN, False)):
        headers = _as(headers_for, email)

        me = client.get("/api/v1/access/me", headers=headers)
        insights = client.get("/api/v1/agent/insights", headers=headers)
        runs = client.get("/api/v1/agent/runs", headers=headers)

        expected = 200 if open_to_them else 403
        assert me.json()["ai_assistant"] is open_to_them
        assert (insights.status_code, runs.status_code) == (expected, expected)


def test_an_admin_with_no_view_as_sees_every_book(people, headers_for) -> None:
    admin = _as(headers_for, ADMIN)

    assert _roster_clients(admin) == set(CLIENTS)
    assert _roster_clients(admin, fa_id="acc-fa-brian") == {BRIAN_CLIENT}


def test_only_an_admin_can_set_view_as(people, headers_for) -> None:
    response = client.put(
        "/api/v1/access/view-as", json={"email": BRIAN}, headers=_as(headers_for, ASHA)
    )

    assert response.status_code == 403
    with SessionLocal() as session:
        assert session.get(UserAccess, ADMIN).view_as_email is None


def test_view_as_shows_that_advisors_book_and_every_swap_is_audited(people, headers_for) -> None:
    admin = _as(headers_for, ADMIN)
    with SessionLocal() as session:
        last_log_id = session.scalar(select(func.coalesce(func.max(AuditLog.log_id), 0)))

    swapped = client.put("/api/v1/access/view-as", json={"email": BRIAN.upper()}, headers=admin)

    assert swapped.status_code == 200
    assert swapped.json()["viewing_as"] == BRIAN
    assert _roster_clients(admin) == {BRIAN_CLIENT}
    assert (
        client.get(f"{ROSTER}/{ASHA_CLIENT}/{FUND}/transactions", headers=admin).status_code == 403
    )
    assert (
        client.get("/api/v1/fa-action/acc-fa-asha", headers=admin).json()["fa_id"] == "acc-fa-brian"
    )

    cleared = client.delete("/api/v1/access/view-as", headers=admin)

    assert cleared.json()["viewing_as"] is None
    assert _roster_clients(admin) == set(CLIENTS)
    with SessionLocal() as session:
        swaps = session.scalars(
            select(AuditLog)
            .where(
                AuditLog.entity_type == "user_access",
                AuditLog.actor_id == ADMIN,
                AuditLog.log_id > last_log_id,
            )
            .order_by(AuditLog.log_id)
        ).all()
    assert [(row.action, row.detail["to"]) for row in swaps] == [
        ("view_as_set", BRIAN),
        ("view_as_cleared", None),
    ]


def test_the_advisor_list_is_admin_only_and_holds_active_advisors(people, headers_for) -> None:
    refused = client.get("/api/v1/access/advisors", headers=_as(headers_for, ASHA))
    listed = client.get("/api/v1/access/advisors", headers=_as(headers_for, ADMIN))

    assert refused.status_code == 403
    emails = {row["email"] for row in listed.json()} & set(ALL_EMAILS)
    assert emails == {ASHA, BRIAN}


def test_view_as_only_accepts_an_active_advisor(people, headers_for) -> None:
    admin = _as(headers_for, ADMIN)

    for target in (GONE, ADMIN, STRANGER):
        response = client.put("/api/v1/access/view-as", json={"email": target}, headers=admin)
        assert response.status_code == 404


def test_a_write_while_viewing_as_keeps_both_emails(people, headers_for) -> None:
    admin = _as(headers_for, ADMIN)
    client.put("/api/v1/access/view-as", json={"email": BRIAN}, headers=admin)

    logged = client.post(
        f"{ROSTER}/{BRIAN_CLIENT}/{FUND}/interactions",
        json={"type": "call_logged", "outcome": "follow_up", "follow_up_on": "2999-01-01"},
        headers=admin,
    )

    assert logged.status_code == 201
    with SessionLocal() as session:
        interaction = session.scalar(
            select(ActiveClientInteraction).where(ActiveClientInteraction.client_id == BRIAN_CLIENT)
        )
        follow_up = session.scalar(
            select(ActiveClientTouchpoint).where(ActiveClientTouchpoint.client_id == BRIAN_CLIENT)
        )
        audit = session.scalar(
            select(AuditLog)
            .where(
                AuditLog.entity_type == "active_client_interaction",
                AuditLog.entity_id == f"{BRIAN_CLIENT}/{FUND}",
            )
            .order_by(AuditLog.log_id.desc())
            .limit(1)
        )
    assert (interaction.reviewer_id, interaction.on_behalf_of) == (ADMIN, BRIAN)
    assert (follow_up.created_by, follow_up.on_behalf_of) == (ADMIN, BRIAN)
    assert (audit.actor_id, audit.detail["on_behalf_of"]) == (ADMIN, BRIAN)


def test_an_advisors_own_write_has_no_on_behalf_of(people, headers_for) -> None:
    logged = client.post(
        f"{ROSTER}/{ASHA_CLIENT}/{FUND}/interactions",
        json={"type": "call_logged", "outcome": "connected"},
        headers=_as(headers_for, ASHA),
    )

    assert logged.status_code == 201
    with SessionLocal() as session:
        interaction = session.scalar(
            select(ActiveClientInteraction).where(ActiveClientInteraction.client_id == ASHA_CLIENT)
        )
    assert (interaction.reviewer_id, interaction.on_behalf_of) == (ASHA, None)


def test_the_feed_adds_advisors_and_leaves_admin_and_manual_rows_alone(people) -> None:
    with SessionLocal() as session:
        for client_id, email in ((ASHA_CLIENT, NEW_ADVISOR.upper()), (BRIAN_CLIENT, ADMIN)):
            session.add(
                ClientSide(
                    client_id=client_id,
                    side="active",
                    balance=10_000.0,
                    threshold_kes=6_000.0,
                    source_feed="active",
                )
            )
            session.execute(
                ActiveClientFund.__table__.update()
                .where(ActiveClientFund.client_id == client_id)
                .values(fa_email=email, fa_name="New Person" if client_id == ASHA_CLIENT else None)
            )
        session.add(
            ActiveClientFund(
                client_id=ASHA_CLIENT,
                unit_fund_id=FUND + 1,
                client_code=f"C{ASHA_CLIENT}",
                balance=1.0,
                n_deposits=1,
                n_withdrawals=0,
                fa_email=BRIAN,
            )
        )
        session.commit()

        added = link_advisors_from_feed(session)
        session.commit()

        new = session.get(UserAccess, NEW_ADVISOR)
        assert added == 1
        assert (new.role, new.source, new.name, new.receives_digest) == (
            ROLE_ADVISOR,
            SOURCE_AUTO,
            "New Person",
            False,
        )
        assert advisor_fa_id(new) == NEW_ADVISOR
        admin = session.get(UserAccess, ADMIN)
        assert (admin.role, admin.name) == (ROLE_ADMIN, None)
        brian = session.get(UserAccess, BRIAN)
        assert (brian.source, brian.fa_id, brian.name) == (SOURCE_MANUAL, "acc-fa-brian", None)


def test_an_advisor_with_no_advisor_id_is_filed_under_their_email(people, headers_for) -> None:
    with SessionLocal() as session:
        session.execute(update(UserAccess).where(UserAccess.email == BRIAN).values(fa_id=None))
        session.execute(
            update(FaAssignment).where(FaAssignment.client_id == BRIAN_CLIENT).values(fa_id=BRIAN)
        )
        session.commit()

    assert _roster_clients(_as(headers_for, BRIAN)) == {BRIAN_CLIENT}


def test_only_switched_on_advisors_are_in_the_digest(people) -> None:
    def chosen(mode: str) -> dict[str, str]:
        with SessionLocal() as session:
            recipients = digest_recipients(session, Settings(digest_mode=mode))
        return {r.email: r.fa_id for r in recipients if r.email in ALL_EMAILS}

    assert chosen("selected") == {ASHA: "acc-fa-asha"}
    assert chosen("all") == {ASHA: "acc-fa-asha", BRIAN: "acc-fa-brian"}
    assert chosen("off") == {}
