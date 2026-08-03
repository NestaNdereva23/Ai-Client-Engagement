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
from app.db.models.views import llm_client_context, llm_client_numeric_facts
from app.db.session import SessionLocal
from app.privacy.boundary import run_model_boundary, to_model_context
from app.privacy.fact_block import FUND_DISPLAY_NAMES, ModelFactBlock
from app.privacy.scanners import InboundLeak, OutboundLeak

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
    context = {"archetype": "One-and-done", "client_name": "Jane Wanjiru"}
    with pytest.raises(InboundLeak):
        run_model_boundary(context, _placeholder_draft)


def test_a_name_seeded_into_an_allowlisted_value_is_blocked_inbound() -> None:
    context = {"archetype": "One-and-done", "value_tier_label": "High, Jane Wanjiru"}
    called = False

    def model_call(payload: dict) -> str:
        nonlocal called
        called = True
        return "draft"

    with pytest.raises(InboundLeak):
        run_model_boundary(context, model_call, identifiers=["Jane Wanjiru"])
    assert called is False


def test_a_name_seeded_into_a_draft_is_blocked_outbound() -> None:
    context = {"archetype": "One-and-done", "value_tier_label": "High"}
    with pytest.raises(OutboundLeak):
        run_model_boundary(
            context,
            lambda payload: "Dear Jane Wanjiru, come back soon.",
            identifiers=["Jane Wanjiru"],
        )


def test_a_contact_channel_seeded_into_a_draft_is_blocked_outbound() -> None:
    context = {"archetype": "One-and-done", "value_tier_label": "High"}
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

_BAND_KEYS = (
    "recency_band",
    "value_band",
    "cadence_band",
    "hold_band",
    "purchase_depth",
    "trend_band",
    "exit_reason",
    "fund_type",
    "in_wave",
    "has_depth",
    "staged_exit",
    "stale_contact",
)
_EXACT_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _fact_block_payload(bands_row: dict, numeric_row: dict) -> dict[str, Any]:
    """Combine the two allow-listed views into one client's fact-block payload.

    Stands in for the real prompt-assembly wiring, which does not exist yet;
    this is exactly the combination that wiring will eventually automate.
    Routing through ModelFactBlock itself, not just its output shape, is what
    also rounds the amounts and drops a cadence fact with no real cadence.
    """
    facts = {key: bands_row[key] for key in _BAND_KEYS if bands_row.get(key) is not None}
    for key in (
        "years_since_exit",
        "typical_contribution_kes",
        "largest_contribution_kes",
        "invested_every_n_days",
        "days_held_after_last_topup",
        "month_they_left",
    ):
        value = numeric_row.get(key)
        if value is not None:
            facts[key] = value
    fund_name = FUND_DISPLAY_NAMES.get(bands_row.get("fund_type"))
    if fund_name is not None:
        facts["fund_name"] = fund_name
    return ModelFactBlock(**facts).to_dict()


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
        numeric_rows = {
            row["client_id"]: dict(row)
            for row in session.execute(
                select(llm_client_numeric_facts).where(
                    llm_client_numeric_facts.c.client_id.in_(list(names))
                )
            ).mappings()
        }
    assert set(band_rows) == set(names)
    assert set(numeric_rows) == set(names)

    sent: list[dict[str, Any]] = []

    def capturing_model_call(payload: dict[str, Any]) -> str:
        sent.append(payload)
        return _placeholder_draft(payload)

    for client_id in names:
        payload = _fact_block_payload(band_rows[client_id], numeric_rows[client_id])
        draft = run_model_boundary(
            payload,
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
