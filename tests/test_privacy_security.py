"""PII-leakage safety tests over the model boundary.

These are the belt over the whole path: capture every payload a cohort would
send to the model and assert none carries PII, and prove a seeded name or
contact channel is blocked both inbound and outbound. The PII check here is
deliberately independent of the scanners' own patterns, so a blind spot in one
cannot hide in the other.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import delete, select

from app.agents.graph import load_client_facts
from app.briefing.render import BriefingFacts
from app.db.models.audit import AuditLog
from app.db.models.models import (
    ClientFeatures,
    ClientFund,
    Clients,
    Funds,
    IngestionStatus,
    PiiVault,
    RawStaging,
    Transactions,
)
from app.db.models.views import llm_client_context
from app.db.session import SessionLocal
from app.privacy.boundary import run_model_boundary, to_model_context
from app.privacy.fact_block import ModelFactBlock
from app.privacy.scanners import InboundLeak, OutboundLeak
from app.services.briefing import to_risk_fact_block

EAT = timezone(timedelta(hours=3))
ANCHOR = datetime(2026, 7, 23, 9, 0, tzinfo=EAT)

# Independent PII detectors, kept separate from the scanner implementation.
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_LONG_DIGITS = re.compile(r"\d{7,}")


def _carries_pii(text: str, forbidden: list[str]) -> bool:
    if _EMAIL.search(text) or _LONG_DIGITS.search(text):
        return True
    lowered = text.lower()
    return any(value and value.lower() in lowered for value in forbidden)


def _placeholder_draft(payload: dict[str, Any]) -> str:
    return "Dear {{first_name}}, your {{fund_name}} is waiting. Warm regards."


# --- Seeded-leak blocking (pure) --------------------------------------------


def test_a_name_seeded_under_a_disallowed_key_is_blocked_inbound() -> None:
    context = {"value_band": "High", "client_name": "Jane Wanjiru"}
    with pytest.raises(InboundLeak):
        run_model_boundary(context, _placeholder_draft)


def test_a_name_seeded_into_an_allowlisted_value_is_blocked_inbound() -> None:
    context = {"value_band": "High, Jane Wanjiru"}
    called = False

    def model_call(payload: dict) -> str:
        nonlocal called
        called = True
        return "draft"

    with pytest.raises(InboundLeak):
        run_model_boundary(context, model_call, identifiers=["Jane Wanjiru"])
    assert called is False


def test_a_name_seeded_into_a_draft_is_blocked_outbound() -> None:
    context = {"value_band": "High"}
    with pytest.raises(OutboundLeak):
        run_model_boundary(
            context,
            lambda payload: "Dear Jane Wanjiru, come back soon.",
            identifiers=["Jane Wanjiru"],
        )


def test_a_contact_channel_seeded_into_a_draft_is_blocked_outbound() -> None:
    context = {"value_band": "High"}
    with pytest.raises(OutboundLeak):
        run_model_boundary(context, lambda payload: "Reply to jane@example.com to return.")


# --- Whole-cohort capture (database) ----------------------------------------


def _client(cid: int, name: str, txn_id: int) -> dict[str, Any]:
    return {
        "client_id": cid,
        "client_code": f"C-{cid}",
        "client_name": name,
        "balance": 0,
        "computed_at": "2026-07-20T08:00:00",
        "last_5_purchases": [
            {"id": txn_id, "date": "2024-07-01T00:00:00", "number": "500000", "unit_fund_id": 700}
        ],
        "last_2_sales": [],
    }


@pytest.fixture
def seeded_cohort(db: None, cleanup_runs: list[str]):
    """Seed a small cohort through the transform and clean it up afterwards."""
    from app.transform.load import transform_run

    run_id = uuid4().hex
    cleanup_runs.append(run_id)
    fund_id = 700
    names = {70001: "Jane Wanjiru", 70002: "Otieno Odhiambo", 70003: "Mary-Anne Kamau"}
    payload = {
        "data": [
            {
                "unit_fund_id": fund_id,
                "unit_fund_name": "Money Market Fund",
                "inactive_client_count": len(names),
                "clients": [_client(cid, name, 75000 + cid) for cid, name in names.items()],
            }
        ]
    }
    with SessionLocal() as session:
        session.add(
            IngestionStatus(run_id=run_id, endpoint="inactive-clients", reference_ts=ANCHOR)
        )
        session.add(
            RawStaging(run_id=run_id, endpoint="inactive-clients", natural_key="1", payload=payload)
        )
        session.commit()
        transform_run(session, run_id)

    yield fund_id, names

    ids = list(names)
    with SessionLocal() as session:
        session.execute(delete(Transactions).where(Transactions.client_id.in_(ids)))
        session.execute(delete(ClientFeatures).where(ClientFeatures.client_id.in_(ids)))
        session.execute(delete(ClientFund).where(ClientFund.client_id.in_(ids)))
        session.execute(delete(PiiVault).where(PiiVault.client_id.in_(ids)))
        session.execute(delete(Clients).where(Clients.client_id.in_(ids)))
        session.execute(delete(Funds).where(Funds.unit_fund_id == fund_id))
        session.execute(delete(AuditLog).where(AuditLog.run_id == run_id))
        session.commit()


def test_every_outbound_payload_in_a_cohort_carries_no_pii(seeded_cohort) -> None:
    fund_id, names = seeded_cohort
    real_values = list(names.values())

    with SessionLocal() as session:
        rows = (
            session.execute(
                select(llm_client_context).where(llm_client_context.c.client_id.in_(list(names)))
            )
            .mappings()
            .all()
        )
    assert len(rows) == len(names)

    sent: list[dict[str, Any]] = []

    def capturing_model_call(payload: dict[str, Any]) -> str:
        sent.append(payload)
        return _placeholder_draft(payload)

    for row in rows:
        context = to_model_context(row)
        draft = run_model_boundary(
            context,
            capturing_model_call,
            identifiers=real_values,
            entity_id=str(row["client_id"]),
        )
        assert not _carries_pii(draft, real_values)

    assert len(sent) == len(names)
    for payload in sent:
        assert "client_id" not in payload
        serialized = " ".join(str(value) for value in payload.values())
        assert not _carries_pii(serialized, real_values)


# --- the fact-block path: same guarantee, wider vocabulary -------------------

_EXACT_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _fact_block_payload(session, client_id: int, bands_row: dict) -> dict[str, Any]:
    """One client's fact-block payload, through the real assembly the graph uses.

    Calls production's own loader rather than a copy of it, so this cannot
    keep passing against a stand-in after the real one drifts.
    """
    return load_client_facts(session, client_id, bands_row)


def test_every_outbound_fact_block_payload_in_a_cohort_carries_no_pii(seeded_cohort) -> None:
    fund_id, names = seeded_cohort
    codes = [f"C-{cid}" for cid in names]
    real_values = [*names.values(), *codes]

    with SessionLocal() as session:
        band_rows = {
            row["client_id"]: dict(row)
            for row in session.execute(
                select(llm_client_context).where(llm_client_context.c.client_id.in_(list(names)))
            ).mappings()
        }
        assert set(band_rows) == set(names)
        payloads = {
            client_id: _fact_block_payload(session, client_id, band_rows[client_id])
            for client_id in names
        }
    assert all(payload for payload in payloads.values())

    sent: list[dict[str, Any]] = []

    def capturing_model_call(payload: dict[str, Any]) -> str:
        sent.append(payload)
        return _placeholder_draft(payload)

    for client_id in names:
        draft = run_model_boundary(
            payloads[client_id],
            capturing_model_call,
            identifiers=real_values,
            entity_id=str(client_id),
        )
        assert not _carries_pii(draft, real_values)

    assert len(sent) == len(names)
    for payload in sent:
        # No name, code, or id: structurally, since none of these are keys
        # ModelFactBlock declares, and by content, via the literal check above.
        assert "client_id" not in payload
        assert "client_code" not in payload
        assert "client_name" not in payload
        serialized = " ".join(str(value) for value in payload.values())
        assert not _carries_pii(serialized, real_values)
        # month_they_left (YYYY-MM) is fine; a full calendar date is not.
        assert not _EXACT_DATE.search(serialized)


def test_an_unrounded_amount_never_reaches_the_model_call() -> None:
    called = False

    def model_call(payload: dict) -> str:
        nonlocal called
        called = True
        return "draft"

    with pytest.raises(InboundLeak, match="would have corrected"):
        run_model_boundary({"typical_contribution_kes": 4_466_000}, model_call)
    assert called is False


def test_a_cadence_fact_for_a_client_with_no_cadence_never_reaches_the_model_call() -> None:
    called = False

    def model_call(payload: dict) -> str:
        nonlocal called
        called = True
        return "draft"

    with pytest.raises(InboundLeak, match="would have corrected"):
        run_model_boundary({"cadence_band": "None", "invested_every_n_days": 30}, model_call)
    assert called is False


def test_a_cadence_fact_built_through_modelfactblock_is_simply_absent() -> None:
    """The well-behaved path: no cadence means the fact is never assembled."""
    payload = ModelFactBlock(cadence_band="None", invested_every_n_days=30).to_dict()
    assert "invested_every_n_days" not in payload
    assert run_model_boundary(payload, lambda p: "draft") == "draft"


# --- the risk fact-block path (AM15): same guarantee, active-book vocabulary -


def _briefing_facts(**overrides) -> BriefingFacts:
    """One realistic client-fund's gathered facts, standing in for what
    services.briefing.gather_briefing_facts would actually return -- carrying the
    exact figures and the pseudonymous client_code a real row would, so the
    projection below has something real to fail to leak.
    """
    defaults = dict(
        client_code="C-94001",
        fund_name="Cytonn Money Market Fund",
        risk_score=87,
        risk_band="Critical",
        route="fa_call_priority",
        balance=4_466_000.0,
        balance_tier="Institutional",
        days_since_deposit=410,
        last_deposit_amount=250_000.0,
        typical_gap_days=30.0,
        overdue_multiple=13.7,
        typical_deposit_amount=180_000.0,
        largest_deposit_amount=500_000.0,
        deposit_trend=-0.42,
        largest_withdrawal=900_000.0,
        withdrawal_pct=0.65,
        days_since_withdrawal=90,
        signals={
            "sig_broken_pattern": True,
            "sig_dormant": True,
            "sig_heavy_withdrawal": True,
            "sig_shrinking": True,
            "sig_going_dormant": False,
            "sig_never_repeated": False,
        },
        deposit_count_capped=True,
        withdrawal_history_hidden=True,
        holds_both_funds=True,
        months_until_empty=2.0,
        months_until_empty_threshold=6.0,
        has_open_complaint=True,
        recency_band="1-2y",
        value_tier="High",
    )
    defaults.update(overrides)
    return BriefingFacts(**defaults)


def test_to_risk_fact_block_payload_carries_no_pii_or_exact_figure() -> None:
    real_values = ["Jane Wanjiru", "C-94001"]
    facts = _briefing_facts()

    risk_fact_block = to_risk_fact_block(facts)
    payload = risk_fact_block.to_dict()

    assert "client_code" not in payload
    assert "client_id" not in payload
    assert "risk_score" not in payload
    assert "balance" not in payload

    sent: list[dict[str, Any]] = []

    def capturing_model_call(sent_payload: dict[str, Any]) -> str:
        sent.append(sent_payload)
        return "This client has gone quiet and broke their own pattern."

    draft = run_model_boundary(payload, capturing_model_call, identifiers=real_values)

    assert not _carries_pii(draft, real_values)
    assert len(sent) == 1
    serialized = " ".join(str(value) for value in sent[0].values())
    assert not _carries_pii(serialized, real_values)
    # Every exact figure facts carried -- the score, the balance, every KES
    # amount, every ratio, every days-since-X count -- stays out, not just
    # rounded: RiskFactBlock declares no field that could carry any of them.
    for exact in (
        "87",
        "4466000",
        "410",
        "250000",
        "13.7",
        "180000",
        "500000",
        "900000",
        "90",
        "2.0",
    ):
        assert exact not in serialized
    assert not _EXACT_DATE.search(serialized)
