"""Regression test for the wrong-endpoint bug this split exists to prevent.

The single shared scripts/transform.py this replaced picked "the latest
ingestion run", full stop, across both feeds. A more recent active-clients
run then got silently flattened through the dormant pipeline, loading
active-client rows into clients/client_fund/client_features. _latest_run_id
here must only ever consider inactive-clients runs, however recent a run on
the other feed is.
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
    spec = importlib.util.spec_from_file_location("scripts_inactive_transform", _MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_latest_run_id_ignores_a_more_recent_active_clients_run(db, cleanup_runs):
    transform = _load_transform_module()
    older_inactive = uuid4().hex
    newer_active = uuid4().hex
    cleanup_runs.extend([older_inactive, newer_active])

    now = datetime.now(UTC)
    with SessionLocal() as session:
        session.add(
            IngestionStatus(run_id=older_inactive, endpoint="inactive-clients", started_at=now)
        )
        session.add(
            IngestionStatus(
                run_id=newer_active,
                endpoint="active-clients",
                started_at=now + timedelta(minutes=5),
            )
        )
        session.commit()

        picked = transform._latest_run_id(session)

    assert picked == older_inactive


def test_latest_run_id_picks_the_most_recent_among_several_inactive_runs(db, cleanup_runs):
    transform = _load_transform_module()
    older = uuid4().hex
    newer = uuid4().hex
    cleanup_runs.extend([older, newer])

    now = datetime.now(UTC)
    with SessionLocal() as session:
        session.add(IngestionStatus(run_id=older, endpoint="inactive-clients", started_at=now))
        session.add(
            IngestionStatus(
                run_id=newer, endpoint="inactive-clients", started_at=now + timedelta(minutes=5)
            )
        )
        session.commit()

        picked = transform._latest_run_id(session)

    assert picked == newer


def test_latest_run_id_is_none_with_no_inactive_runs_at_all(db, cleanup_runs):
    transform = _load_transform_module()
    active_only = uuid4().hex
    cleanup_runs.append(active_only)

    with SessionLocal() as session:
        session.add(IngestionStatus(run_id=active_only, endpoint="active-clients"))
        session.commit()

        picked = transform._latest_run_id(session)

    assert picked is None
