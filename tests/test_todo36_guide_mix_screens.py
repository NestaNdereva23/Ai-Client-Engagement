from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import delete

from app.agents.results_summary import read_results_by_mix
from app.api.routers.action_catalog import list_action_mixes
from app.db.models.action_performance import ActionPerformance
from app.db.models.agent import CONTENT_MIXES
from app.db.session import SessionLocal

PERF_ACTION = "todo36_mix_test_action"
NOW = datetime(2031, 2, 1, 12, 0, tzinfo=UTC)
PERIOD_START = datetime(2031, 1, 20, 0, 0, tzinfo=UTC)
PERIOD_HOURS = 168
WINDOW = 30


def _perf(session, *, mix, sent, replied, opted_out, edited, deposited, money):
    session.add(
        ActionPerformance(
            period_start=PERIOD_START,
            period_end=PERIOD_START,
            period_hours=PERIOD_HOURS,
            window_days=WINDOW,
            action_code=PERF_ACTION,
            angle="pick_up_again",
            priority_tier="T3",
            risk_band="unknown",
            content_mix=mix,
            variant="none",
            sent_count=sent,
            replied_count=replied,
            opted_out_count=opted_out,
            edited_count=edited,
            deposited_count=deposited,
            reply_rate=0.0,
            opt_out_rate=0.0,
            edit_rate=0.0,
            deposit_rate=0.0,
            money_in_kes=money,
            computed_at=NOW,
        )
    )


@pytest.fixture
def performance_rows(db: None):
    with SessionLocal() as session:
        session.execute(
            delete(ActionPerformance).where(ActionPerformance.action_code == PERF_ACTION)
        )
        _perf(
            session,
            mix="balanced",
            sent=100,
            replied=10,
            opted_out=1,
            edited=14,
            deposited=7,
            money=5000,
        )
        _perf(
            session,
            mix="learning_only",
            sent=50,
            replied=4,
            opted_out=0,
            edited=3,
            deposited=3,
            money=2000,
        )
        _perf(
            session,
            mix="mostly_ask",
            sent=3,
            replied=1,
            opted_out=0,
            edited=1,
            deposited=0,
            money=0,
        )
        session.commit()
    yield
    with SessionLocal() as session:
        session.execute(
            delete(ActionPerformance).where(ActionPerformance.action_code == PERF_ACTION)
        )
        session.commit()


def _pin_min_group_size(monkeypatch, size):
    from app.agents import results_summary

    real = results_summary.get_settings()

    class Pinned:
        def __getattr__(self, name):
            return getattr(real, name)

        agent_query_min_group_size = size

    monkeypatch.setattr(results_summary, "get_settings", lambda: Pinned())


def test_results_by_mix_aggregates_each_mix(monkeypatch, performance_rows):
    _pin_min_group_size(monkeypatch, 1)
    with SessionLocal() as session:
        results = read_results_by_mix(session, lookback_hours=PERIOD_HOURS * 4, now=NOW)

    by_mix = {line.guide_mix: line for line in results.lines}
    assert {"balanced", "learning_only", "mostly_ask"} <= set(by_mix)

    balanced = by_mix["balanced"]
    assert balanced.sent_count == 100
    assert balanced.reply_percent == pytest.approx(10.0)
    assert balanced.opt_out_percent == pytest.approx(1.0)
    assert balanced.edit_percent == pytest.approx(14.0)
    assert balanced.deposit_percent == pytest.approx(7.0)
    assert balanced.money_in_kes == pytest.approx(5000)

    learning = by_mix["learning_only"]
    assert learning.sent_count == 50
    assert learning.deposit_percent == pytest.approx(6.0)


def test_results_by_mix_honours_min_group_size(monkeypatch, performance_rows):
    _pin_min_group_size(monkeypatch, 5)
    with SessionLocal() as session:
        results = read_results_by_mix(session, lookback_hours=PERIOD_HOURS * 4, now=NOW)

    assert {line.guide_mix for line in results.lines} == {"balanced", "learning_only"}
    assert results.withheld_small_groups == 1


def test_action_mix_list_reads_the_live_catalogue():
    with SessionLocal() as session:
        out = list_action_mixes(session)

    assert out.actions, "the seeded catalogue should list some actions"
    codes = {action.action_code for action in out.actions}
    assert "welcome_and_top_up" in codes
    for action in out.actions:
        assert action.content_mix in CONTENT_MIXES


def test_todo36_endpoints_are_registered():
    from app.main import app

    paths = set(app.openapi().get("paths", {}))
    assert "/api/v1/agent/actions" in paths
    assert "/api/v1/agent/actions/{action_code}/content-mix" in paths
    assert "/api/v1/agent/metrics/guide-mix" in paths
