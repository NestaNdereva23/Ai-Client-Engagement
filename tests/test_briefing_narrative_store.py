"""Tests for the stored narration: the facts fingerprint that decides when a
stored one is still usable, the two service read paths that use it, and the
nightly warm-up that fills it for the clients a digest surfaced.
"""

from __future__ import annotations

from datetime import date
from uuid import uuid4

import pytest
from sqlalchemy import delete, select

from app.briefing.store import (
    facts_fingerprint,
    read_narrative,
    save_narrative,
    stale_or_missing_keys,
)
from app.config import get_settings
from app.db.models.active_clients import ActiveClientFund
from app.db.models.audit import AuditLog
from app.db.models.briefing import BriefingNarrative
from app.db.models.digest import DigestLine, DigestRun
from app.db.models.models import PiiVault
from app.db.models.risk import ClientRiskFeatures, RiskRun
from app.db.session import SessionLocal
from app.privacy.fact_block import RiskFactBlock
from app.services.briefing import (
    gather_briefing_facts,
    get_briefing,
    get_narrative_briefing,
    to_risk_fact_block,
)
from app.workers.narrative import warm_digest_narratives

FUND_ID = 949
CLIENT_ID = 94901
OTHER_CLIENT_ID = 94902

_SIGNALS = {
    "sig_heavy_withdrawal": False,
    "sig_dormant": True,
    "sig_broken_pattern": True,
    "sig_shrinking": False,
    "sig_going_dormant": False,
    "sig_never_repeated": False,
}

NARRATION = "This client has been quiet for a long stretch and broke their own pattern."


class _ScriptedLLMClient:
    """Records every call, so a test can prove a stored narration meant the
    model was never asked again.
    """

    model = "store-stub"

    def __init__(self, response: str = NARRATION) -> None:
        self.response = response
        self.calls: list[dict[str, str]] = []

    def generate(self, *, system: str, user: str) -> str:
        self.calls.append({"system": system, "user": user})
        return self.response


def _seed_client(session, client_id: int) -> None:
    session.add(
        ClientRiskFeatures(
            client_id=client_id,
            unit_fund_id=FUND_ID,
            balance_tier="Core",
            recency_band="1-2y",
            value_tier="Medium",
            overdue_multiple=2.0,
            **_SIGNALS,
            risk_score=55,
            risk_band="Watch",
            risk_reasons="No deposit in 12 months; Broke their own pattern",
            fund_at_risk=500_000.0,
            config_version=1,
            route="fa_watchlist",
            queue_rank=None,
        )
    )
    session.add(
        ActiveClientFund(
            client_id=client_id,
            unit_fund_id=FUND_ID,
            client_code=f"C{client_id}",
            balance=500_000.0,
            n_deposits=3,
            n_withdrawals=0,
            last_deposit_date=date(2024, 1, 1),
            deposit_count_capped=False,
            withdrawal_history_hidden=False,
        )
    )
    session.add(PiiVault(client_id=client_id, client_name=f"Client {client_id}"))


@pytest.fixture
def seeded(db):
    client_ids = (CLIENT_ID, OTHER_CLIENT_ID)
    with SessionLocal() as session:
        for client_id in client_ids:
            _seed_client(session, client_id)
        session.commit()
    yield client_ids
    with SessionLocal() as session:
        session.execute(
            delete(BriefingNarrative).where(BriefingNarrative.client_id.in_(client_ids))
        )
        session.execute(
            delete(ClientRiskFeatures).where(ClientRiskFeatures.client_id.in_(client_ids))
        )
        session.execute(delete(ActiveClientFund).where(ActiveClientFund.client_id.in_(client_ids)))
        session.execute(delete(PiiVault).where(PiiVault.client_id.in_(client_ids)))
        session.execute(
            delete(AuditLog).where(
                AuditLog.entity_type.in_(("risk_briefing", "pii_vault")),
                AuditLog.entity_id.in_([str(client_id) for client_id in client_ids]),
            )
        )
        session.commit()


@pytest.fixture
def narratives_on(monkeypatch):
    monkeypatch.setenv("AI_BRIEFING_ENABLED", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _facts(session, client_id: int = CLIENT_ID) -> RiskFactBlock:
    gathered = gather_briefing_facts(session, client_id, FUND_ID, date(2026, 8, 19))
    assert gathered is not None
    return to_risk_fact_block(gathered)


# --- the fingerprint ---------------------------------------------------------


def test_fingerprint_is_the_same_for_two_equal_fact_blocks() -> None:
    left = RiskFactBlock(risk_band="Watch", route="fa_watchlist", sig_dormant=True)
    right = RiskFactBlock(sig_dormant=True, route="fa_watchlist", risk_band="Watch")
    assert facts_fingerprint(left) == facts_fingerprint(right)


def test_fingerprint_changes_when_any_fact_changes() -> None:
    before = RiskFactBlock(risk_band="Watch", sig_dormant=True)
    after = RiskFactBlock(risk_band="High", sig_dormant=True)
    assert facts_fingerprint(before) != facts_fingerprint(after)


def test_fingerprint_changes_when_a_signal_stops_firing() -> None:
    """The case that makes a stored narration dangerous: the text still says
    they broke their pattern after they have stopped doing so.
    """
    before = RiskFactBlock(risk_band="Watch", sig_broken_pattern=True)
    after = RiskFactBlock(risk_band="Watch", sig_broken_pattern=False)
    assert facts_fingerprint(before) != facts_fingerprint(after)


# --- the store ---------------------------------------------------------------


def test_save_then_read_returns_the_stored_text(seeded) -> None:
    with SessionLocal() as session:
        facts = _facts(session)
        save_narrative(session, CLIENT_ID, FUND_ID, facts, text=NARRATION, model="stub")
        session.commit()
        assert read_narrative(session, CLIENT_ID, FUND_ID, facts) == NARRATION


def test_read_returns_nothing_once_the_facts_have_moved(seeded) -> None:
    with SessionLocal() as session:
        facts = _facts(session)
        save_narrative(session, CLIENT_ID, FUND_ID, facts, text=NARRATION, model="stub")
        session.commit()

        moved = facts.model_copy(update={"risk_band": "High"})
        assert read_narrative(session, CLIENT_ID, FUND_ID, moved) is None


def test_saving_twice_replaces_rather_than_duplicates(seeded) -> None:
    with SessionLocal() as session:
        facts = _facts(session)
        save_narrative(session, CLIENT_ID, FUND_ID, facts, text="first", model="stub")
        save_narrative(session, CLIENT_ID, FUND_ID, facts, text="second", model="stub")
        session.commit()

        rows = session.scalars(
            select(BriefingNarrative).where(BriefingNarrative.client_id == CLIENT_ID)
        ).all()
        assert len(rows) == 1
        assert rows[0].narrative_text == "second"


def test_stale_or_missing_keys_returns_only_the_ones_needing_work(seeded) -> None:
    with SessionLocal() as session:
        facts = _facts(session)
        save_narrative(session, CLIENT_ID, FUND_ID, facts, text=NARRATION, model="stub")
        session.commit()

        needing_work = stale_or_missing_keys(
            session,
            {
                (CLIENT_ID, FUND_ID): facts_fingerprint(facts),
                (OTHER_CLIENT_ID, FUND_ID): facts_fingerprint(_facts(session, OTHER_CLIENT_ID)),
            },
        )
        assert needing_work == [(OTHER_CLIENT_ID, FUND_ID)]


# --- the deterministic read path ---------------------------------------------


def test_get_briefing_serves_a_stored_narration(seeded, narratives_on) -> None:
    with SessionLocal() as session:
        save_narrative(session, CLIENT_ID, FUND_ID, _facts(session), text=NARRATION, model="stub")
        session.commit()

    with SessionLocal() as session:
        view = get_briefing(
            session, CLIENT_ID, FUND_ID, viewing_fa_id="fa-1", reference_date=date(2026, 8, 19)
        )
    assert view.mode == "narrative"
    assert view.text == NARRATION


def test_get_briefing_ignores_a_stored_narration_whose_facts_moved(seeded, narratives_on) -> None:
    with SessionLocal() as session:
        stale_facts = _facts(session).model_copy(update={"risk_band": "High"})
        save_narrative(session, CLIENT_ID, FUND_ID, stale_facts, text=NARRATION, model="stub")
        session.commit()

    with SessionLocal() as session:
        view = get_briefing(
            session, CLIENT_ID, FUND_ID, viewing_fa_id="fa-1", reference_date=date(2026, 8, 19)
        )
    assert view.mode == "deterministic"
    assert "CLIENT BRIEFING" in view.text


def test_get_briefing_stays_deterministic_when_the_feature_is_off(seeded, monkeypatch) -> None:
    """A stored narration from a time the feature was on is not served once
    it is switched off.
    """
    monkeypatch.setenv("AI_BRIEFING_ENABLED", "false")
    get_settings.cache_clear()
    with SessionLocal() as session:
        save_narrative(session, CLIENT_ID, FUND_ID, _facts(session), text=NARRATION, model="stub")
        session.commit()

    with SessionLocal() as session:
        view = get_briefing(
            session, CLIENT_ID, FUND_ID, viewing_fa_id="fa-1", reference_date=date(2026, 8, 19)
        )
    get_settings.cache_clear()
    assert view.mode == "deterministic"


# --- the on-demand path ------------------------------------------------------


def test_on_demand_narration_is_stored_and_not_redrafted(seeded, narratives_on) -> None:
    llm = _ScriptedLLMClient()
    with SessionLocal() as session:
        first = get_narrative_briefing(
            session,
            CLIENT_ID,
            FUND_ID,
            viewing_fa_id="fa-1",
            llm_client=llm,
            reference_date=date(2026, 8, 19),
        )
    assert first.mode == "narrative"
    assert len(llm.calls) == 1

    with SessionLocal() as session:
        second = get_narrative_briefing(
            session,
            CLIENT_ID,
            FUND_ID,
            viewing_fa_id="fa-1",
            llm_client=llm,
            reference_date=date(2026, 8, 19),
        )
    assert second.mode == "narrative"
    assert second.text == first.text
    assert len(llm.calls) == 1, "the second look should not have asked the model again"


def test_a_fallback_is_never_stored(seeded, narratives_on) -> None:
    """An ungrounded reply falls back, and nothing is kept: storing it would
    serve the deterministic text under a narrative label forever after.
    """
    llm = _ScriptedLLMClient("Their balance fell 42% last quarter.")
    with SessionLocal() as session:
        view = get_narrative_briefing(
            session,
            CLIENT_ID,
            FUND_ID,
            viewing_fa_id="fa-1",
            llm_client=llm,
            reference_date=date(2026, 8, 19),
        )
        assert view.mode == "deterministic_fallback"
        assert session.get(BriefingNarrative, (CLIENT_ID, FUND_ID)) is None


# --- the nightly warm-up -----------------------------------------------------


@pytest.fixture
def digest_run(seeded):
    run_id = uuid4().hex
    with SessionLocal() as session:
        session.add(RiskRun(run_id=run_id, state="completed", config_version=1))
        session.flush()
        run = DigestRun(risk_run_id=run_id)
        session.add(run)
        session.flush()
        for rank, client_id in enumerate(seeded, start=1):
            session.add(
                DigestLine(
                    digest_run_id=run.digest_run_id,
                    group_key=f"fund:{FUND_ID}",
                    group_total=len(seeded),
                    rank=rank,
                    client_id=client_id,
                    unit_fund_id=FUND_ID,
                    risk_score=55,
                    risk_band="Watch",
                    risk_reasons="No deposit in 12 months",
                    fund_at_risk=500_000.0,
                    score_delta=None,
                    route="fa_watchlist",
                    in_call_queue=False,
                    complaint_caveat=False,
                )
            )
        digest_run_id = run.digest_run_id
        session.commit()
    yield digest_run_id
    with SessionLocal() as session:
        session.execute(delete(DigestLine).where(DigestLine.digest_run_id == digest_run_id))
        session.execute(delete(DigestRun).where(DigestRun.digest_run_id == digest_run_id))
        session.execute(delete(RiskRun).where(RiskRun.run_id == run_id))
        session.commit()


def test_warm_up_drafts_a_narration_for_every_client_on_the_digest(
    digest_run, narratives_on
) -> None:
    llm = _ScriptedLLMClient()
    with SessionLocal() as session:
        result = warm_digest_narratives(
            session, digest_run, llm, limit=50, reference_date=date(2026, 8, 19)
        )

    assert result.drafted == 2
    assert result.already_fresh == 0
    with SessionLocal() as session:
        view = get_briefing(
            session, CLIENT_ID, FUND_ID, viewing_fa_id="fa-1", reference_date=date(2026, 8, 19)
        )
    assert view.mode == "narrative"
    assert view.text == NARRATION


def test_warm_up_skips_the_clients_already_drafted_for_these_facts(
    digest_run, narratives_on
) -> None:
    llm = _ScriptedLLMClient()
    with SessionLocal() as session:
        warm_digest_narratives(session, digest_run, llm, limit=50, reference_date=date(2026, 8, 19))
    with SessionLocal() as session:
        second = warm_digest_narratives(
            session, digest_run, llm, limit=50, reference_date=date(2026, 8, 19)
        )

    assert second.drafted == 0
    assert second.already_fresh == 2
    assert len(llm.calls) == 2, "the second pass should not have asked the model at all"


def test_warm_up_honours_the_limit(digest_run, narratives_on) -> None:
    llm = _ScriptedLLMClient()
    with SessionLocal() as session:
        result = warm_digest_narratives(
            session, digest_run, llm, limit=1, reference_date=date(2026, 8, 19)
        )
    assert result.considered == 1
    assert result.drafted == 1


def test_warm_up_keeps_nothing_for_a_client_whose_narration_was_rejected(
    digest_run, narratives_on
) -> None:
    llm = _ScriptedLLMClient("Their balance fell 42% last quarter.")
    with SessionLocal() as session:
        result = warm_digest_narratives(
            session, digest_run, llm, limit=50, reference_date=date(2026, 8, 19)
        )
        assert result.drafted == 0
        assert result.skipped == 2
        assert session.get(BriefingNarrative, (CLIENT_ID, FUND_ID)) is None
