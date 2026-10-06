from __future__ import annotations

from sqlalchemy import delete, func, select

from app.db.models.active_clients import ActiveClientFund
from app.db.models.models import ClientFund, Clients, Funds
from app.db.models.routing import ClientSide
from app.db.session import SessionLocal
from app.routing.sides import SIDE_ACTIVE, SIDE_INACTIVE, assign_sides

_FUND = 924900
_IDS = list(range(924000, 924020))


def _purge(session) -> None:
    session.execute(delete(ClientSide).where(ClientSide.client_id.in_(_IDS)))
    session.execute(delete(ClientFund).where(ClientFund.client_id.in_(_IDS)))
    session.execute(delete(ActiveClientFund).where(ActiveClientFund.client_id.in_(_IDS)))
    session.execute(delete(Clients).where(Clients.client_id.in_(_IDS)))
    session.execute(delete(Funds).where(Funds.unit_fund_id == _FUND))
    session.commit()


def _active(session, client_id: int, balance: float) -> None:
    session.add(
        ActiveClientFund(
            client_id=client_id,
            unit_fund_id=_FUND,
            balance=balance,
            n_deposits=0,
            n_withdrawals=0,
        )
    )


def _inactive(session, client_id: int, balance: float) -> None:
    if session.get(Funds, _FUND) is None:
        session.add(Funds(unit_fund_id=_FUND, unit_fund_name="Test Fund"))
        session.flush()
    if session.get(Clients, client_id) is None:
        session.add(
            Clients(
                client_id=client_id,
                unit_fund_id=_FUND,
                n_purchases_returned=0,
                n_sales_returned=0,
            )
        )
        session.flush()
    session.add(
        ClientFund(
            client_id=client_id, unit_fund_id=_FUND, balance=balance, n_purchases=0, n_sales=0
        )
    )


def _side(session, client_id: int) -> ClientSide | None:
    return session.get(ClientSide, client_id)


def test_balance_decides_the_side_around_the_threshold(db) -> None:
    with SessionLocal() as session:
        _purge(session)
        _active(session, 924000, 5999.99)
        _active(session, 924001, 6000.0)
        _active(session, 924002, 6000.01)
        _active(session, 924003, 0.0)
        session.commit()

        assign_sides(session)

        assert _side(session, 924000).side == SIDE_INACTIVE
        assert _side(session, 924001).side == SIDE_ACTIVE
        assert _side(session, 924002).side == SIDE_ACTIVE
        assert _side(session, 924003).side == SIDE_INACTIVE
        _purge(session)


def test_a_client_in_both_feeds_is_routed_on_its_active_feed_balance(db) -> None:
    with SessionLocal() as session:
        _purge(session)
        _active(session, 924010, 1000.0)
        _inactive(session, 924010, 999999.0)
        _active(session, 924011, 7000.0)
        _inactive(session, 924011, 0.0)
        session.commit()

        assign_sides(session)

        rows = session.scalars(select(ClientSide).where(ClientSide.client_id == 924010)).all()
        assert len(rows) == 1
        assert rows[0].side == SIDE_INACTIVE
        assert rows[0].balance == 1000.0
        assert rows[0].source_feed == "both"

        combined = _side(session, 924011)
        assert combined.side == SIDE_ACTIVE
        assert combined.balance == 7000.0
        assert combined.source_feed == "both"
        _purge(session)


def test_no_client_ever_lands_on_two_sides(db) -> None:
    with SessionLocal() as session:
        _purge(session)
        _active(session, 924000, 10.0)
        _active(session, 924001, 50000.0)
        _inactive(session, 924002, 0.0)
        _inactive(session, 924003, 100.0)
        _active(session, 924004, 2000.0)
        _inactive(session, 924004, 1000.0)
        session.commit()

        assign_sides(session)

        duplicated = session.execute(
            select(ClientSide.client_id)
            .where(ClientSide.client_id.in_(_IDS))
            .group_by(ClientSide.client_id)
            .having(func.count() > 1)
        ).all()
        assert duplicated == []

        active_ids = set(
            session.scalars(
                select(ClientSide.client_id).where(
                    ClientSide.client_id.in_(_IDS), ClientSide.side == SIDE_ACTIVE
                )
            )
        )
        inactive_ids = set(
            session.scalars(
                select(ClientSide.client_id).where(
                    ClientSide.client_id.in_(_IDS), ClientSide.side == SIDE_INACTIVE
                )
            )
        )
        assert active_ids.isdisjoint(inactive_ids)
        assert active_ids == {924001}
        assert inactive_ids == {924000, 924002, 924003, 924004}
        _purge(session)


def test_a_second_run_updates_the_side_without_duplicating(db) -> None:
    with SessionLocal() as session:
        _purge(session)
        _active(session, 924000, 100.0)
        session.commit()
        assign_sides(session)
        assert _side(session, 924000).side == SIDE_INACTIVE

        session.execute(
            ActiveClientFund.__table__.update()
            .where(ActiveClientFund.client_id == 924000)
            .values(balance=10000.0)
        )
        session.commit()

        assign_sides(session)
        rows = session.scalars(select(ClientSide).where(ClientSide.client_id == 924000)).all()
        assert len(rows) == 1
        assert rows[0].side == SIDE_ACTIVE
        assert rows[0].balance == 10000.0
        _purge(session)


def test_active_client_ids_returns_only_clients_at_or_above_the_line(db) -> None:
    from app.routing.sides import active_client_ids

    with SessionLocal() as session:
        _purge(session)
        _active(session, 924000, 5999.0)
        _active(session, 924001, 6000.0)
        _active(session, 924002, 20000.0)
        _inactive(session, 924003, 0.0)
        _active(session, 924004, 3000.0)
        _inactive(session, 924004, 4000.0)
        session.commit()

        ids = active_client_ids(session)

        assert 924001 in ids
        assert 924002 in ids
        assert 924000 not in ids
        assert 924003 not in ids
        assert 924004 not in ids
        _purge(session)
