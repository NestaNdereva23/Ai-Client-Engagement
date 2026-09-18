from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import delete

from app.agents.signal_config import active_threshold
from app.db.models.prompt_config import ActiveConfiguration
from app.db.models.signals import SignalThreshold
from app.db.session import SessionLocal
from app.rules import versioning

SIGNAL_CODE = "signal_config_test_signal"
THRESHOLD_NAME = "window_days"


@pytest.fixture
def cleanup_signal():
    yield SIGNAL_CODE
    with SessionLocal() as session:
        session.execute(delete(SignalThreshold).where(SignalThreshold.signal_code == SIGNAL_CODE))
        session.execute(
            delete(ActiveConfiguration).where(
                ActiveConfiguration.component_type == "signal_threshold",
                ActiveConfiguration.component_key == SIGNAL_CODE,
            )
        )
        session.commit()


def test_active_threshold_reads_the_number_live_on_the_given_date(
    db: None, cleanup_signal: str
) -> None:
    with SessionLocal() as session:
        v1 = versioning.save_draft(
            session,
            "signal_threshold",
            SIGNAL_CODE,
            [{"threshold_name": THRESHOLD_NAME, "value": 30}],
        )
        versioning.publish(session, "signal_threshold", SIGNAL_CODE, v1, at=date(2026, 9, 1))
        session.commit()

    with SessionLocal() as session:
        v2 = versioning.save_draft(
            session,
            "signal_threshold",
            SIGNAL_CODE,
            [{"threshold_name": THRESHOLD_NAME, "value": 45}],
        )
        versioning.publish(session, "signal_threshold", SIGNAL_CODE, v2, at=date(2026, 9, 10))
        session.commit()

    with SessionLocal() as session:
        assert active_threshold(session, SIGNAL_CODE, THRESHOLD_NAME, date(2026, 8, 15)) is None
        assert active_threshold(session, SIGNAL_CODE, THRESHOLD_NAME, date(2026, 9, 5)) == 30
        assert active_threshold(session, SIGNAL_CODE, THRESHOLD_NAME, date(2026, 9, 10)) == 45


def test_active_threshold_returns_none_when_nothing_is_published(db: None) -> None:
    with SessionLocal() as session:
        assert (
            active_threshold(session, "no_such_signal", "no_such_threshold", date.today()) is None
        )


def test_active_threshold_reads_the_seeded_first_deposit_window(db: None) -> None:
    with SessionLocal() as session:
        value = active_threshold(session, "first_deposit_recent", "window_days", date.today())
    assert value == 30
