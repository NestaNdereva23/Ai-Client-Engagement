"""Regression test mirroring scripts/inactive/tests/test_transform.py.

_latest_active_run_id must only ever consider active-clients runs, however
recent a run on the inactive-clients feed is.
"""

from __future__ import annotations

import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from app.db.models.models import IngestionStatus
from app.db.session import SessionLocal

_MODULE_PATH = Path(__file__).resolve().parent.parent / "transform.py"


def _load_transform_module():
    spec = importlib.util.spec_from_file_location("scripts_active_transform", _MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_latest_active_run_id_ignores_a_more_recent_inactive_clients_run(db, cleanup_runs):
    transform = _load_transform_module()
    older_active = uuid4().hex
    newer_inactive = uuid4().hex
    cleanup_runs.extend([older_active, newer_inactive])

    now = datetime.now(UTC)
    with SessionLocal() as session:
        session.add(IngestionStatus(run_id=older_active, endpoint="active-clients", started_at=now))
        session.add(
            IngestionStatus(
                run_id=newer_inactive,
                endpoint="inactive-clients",
                started_at=now + timedelta(minutes=5),
            )
        )
        session.commit()

        picked = transform._latest_active_run_id(session)

    assert picked == older_active


def test_latest_active_run_id_is_none_with_no_active_runs_at_all(db, cleanup_runs):
    transform = _load_transform_module()
    inactive_only = uuid4().hex
    cleanup_runs.append(inactive_only)

    with SessionLocal() as session:
        session.add(IngestionStatus(run_id=inactive_only, endpoint="inactive-clients"))
        session.commit()

        picked = transform._latest_active_run_id(session)

    assert picked is None
