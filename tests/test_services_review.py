"""Server-side re-attachment: placeholder substitution and outreach_message creation.

The substitution and name-fallback logic is pure and tested without a
database. create_outreach_message itself needs the restricted role from the
PII boundary migration, so those tests skip when it is absent, the same way
test_db_roles.py does.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import delete, select, text

from app.config import Settings
from app.db.models.audit import AuditLog
from app.db.models.llmops import GenerationRun
from app.db.models.models import Clients, Funds, PiiVault
from app.db.models.outreach import Campaign, OutreachMessage, ReviewAction
from app.db.session import SessionLocal
from app.llmops.versions import persist_generation_run
from app.services.review import (
    EditedContentRequired,
    InvalidOutcome,
    MessageAlreadyDecided,
    MessageNotFound,
    _fetch_client_name,
    _first_name_from_full_name,
    compute_edit_diff,
    create_outreach_message,
    decide,
    decide_batch,
    get_message,
    get_review_history,
    list_pending_messages,
    personalize_content,
    resolve_placeholders,
)


def test_first_name_from_full_name_uses_the_first_word() -> None:
    assert _first_name_from_full_name("Jane Doe") == "Jane"


def test_first_name_from_full_name_falls_back_when_none() -> None:
    assert _first_name_from_full_name(None) == "Valued Client"


def test_first_name_from_full_name_falls_back_when_blank() -> None:
    assert _first_name_from_full_name("   ") == "Valued Client"


def test_resolve_placeholders_substitutes_both_tokens() -> None:
    result = resolve_placeholders(
        "Dear {{first_name}}, your {{fund_name}} awaits.",
        first_name="Jane",
        fund_name="Money Market Fund",
    )
    assert result == "Dear Jane, your Money Market Fund awaits."


def test_resolve_placeholders_leaves_text_without_placeholders_unchanged() -> None:
    assert resolve_placeholders("no tokens here", first_name="Jane", fund_name="MMF") == (
        "no tokens here"
    )


def test_personalize_content_applies_to_subject_and_body() -> None:
    draft = {"subject": "Hi {{first_name}}", "body": "About {{fund_name}}, {{first_name}}."}
    result = personalize_content(draft, first_name="Jane", fund_name="MMF")
    assert result == {"subject": "Hi Jane", "body": "About MMF, Jane."}


def test_resolve_placeholders_substitutes_placeholder_filled_facts_when_given() -> None:
    result = resolve_placeholders(
        "Dear {{first_name}}, your typical contribution was {{typical_contribution}} "
        "and you left in {{month_they_left}}.",
        first_name="Jane",
        fund_name="MMF",
        typical_contribution="KES 5,000",
        month_they_left="March 2025",
    )
    assert result == (
        "Dear Jane, your typical contribution was KES 5,000 and you left in March 2025."
    )


def test_resolve_placeholders_leaves_an_unsupplied_placeholder_fact_untouched() -> None:
    """Not every draft uses all five new tokens; one left unsupplied stays
    literal rather than being blanked out, so a rendering gap is visible
    instead of silently disappearing."""
    result = resolve_placeholders(
        "Dear {{first_name}}, you last topped up {{days_held_after_last_topup}} days before.",
        first_name="Jane",
        fund_name="MMF",
    )
    assert result == "Dear Jane, you last topped up {{days_held_after_last_topup}} days before."


def test_resolve_placeholders_substitutes_the_cadence_interval() -> None:
    """back_on_schedule's own claim names the interval; a bucket draft stands
    it in as a token the same way it does the other five."""
    result = resolve_placeholders(
        "Dear {{first_name}}, resume your {{cadence_interval_days}}-day rhythm.",
        first_name="Jane",
        fund_name="MMF",
        cadence_interval_days="30",
    )
    assert result == "Dear Jane, resume your 30-day rhythm."


def test_personalize_content_applies_placeholder_filled_facts_to_subject_and_body() -> None:
    draft = {
        "subject": "Your fund, {{first_name}}",
        "body": "You held for {{years_since_exit}} years, largest contribution "
        "{{largest_contribution}}.",
    }
    result = personalize_content(
        draft,
        first_name="Jane",
        fund_name="MMF",
        years_since_exit="2.5",
        largest_contribution="KES 20,000",
    )
    assert result == {
        "subject": "Your fund, Jane",
        "body": "You held for 2.5 years, largest contribution KES 20,000.",
    }


def make_settings(**overrides) -> Settings:
    defaults = {
        "llm_provider": "anthropic",
        "anthropic_api_key": "test-key",
        "llm_model": "claude-opus-5",
        "llm_temperature": None,
        "llm_max_tokens": 1024,
    }
    defaults.update(overrides)
    return Settings(**defaults)


def accepted_state(client_id: int, **overrides) -> dict:
    state = {
        "run_id": str(uuid4()),
        "trace_id": uuid4().hex,
        "client_id": client_id,
        "product": "money market",
        "angle": "winback_habit",
        "priority_tier": "T2",
        "prompt_variant": "habit_premium",
        "status": "accepted",
        "attempts": 1,
        "failed_guardrail": None,
        "reason": None,
        "raw_structured_output": {
            "subject": "Come back to {{fund_name}}",
            "body": "Dear {{first_name}}, we miss you.",
        },
    }
    state.update(overrides)
    return state


@pytest.fixture
def roles(db: None):
    with SessionLocal() as session:
        exists = session.scalar(text("SELECT 1 FROM pg_roles WHERE rolname = 'ace_restricted'"))
    if not exists:
        pytest.skip("boundary roles not present; run alembic upgrade head")


@pytest.fixture
def scenario(roles):
    """A fund, a named client, a vault row, an accepted run, and a campaign."""
    fund_id = 970
    client_id = 97001
    with SessionLocal() as session:
        session.add(Funds(unit_fund_id=fund_id, unit_fund_name="Cytonn Money Market Fund"))
        session.commit()
        session.add(
            Clients(
                client_id=client_id,
                unit_fund_id=fund_id,
                n_purchases_returned=0,
                n_sales_returned=0,
            )
        )
        session.add(PiiVault(client_id=client_id, client_name="Jane Doe"))
        session.commit()
        run = persist_generation_run(session, accepted_state(client_id), make_settings())
        campaign = Campaign(name="test campaign")
        session.add(campaign)
        session.commit()
        campaign_id = campaign.campaign_id
        run_id = run.run_id

    yield client_id, run_id, campaign_id, fund_id

    with SessionLocal() as session:
        session.execute(delete(OutreachMessage).where(OutreachMessage.generation_run_id == run_id))
        session.execute(delete(GenerationRun).where(GenerationRun.run_id == run_id))
        session.execute(delete(Campaign).where(Campaign.campaign_id == campaign_id))
        session.execute(delete(PiiVault).where(PiiVault.client_id == client_id))
        session.execute(delete(Clients).where(Clients.client_id == client_id))
        session.execute(delete(Funds).where(Funds.unit_fund_id == fund_id))
        session.commit()


def test_fetch_client_name_reads_the_vault_under_the_restricted_role(scenario) -> None:
    client_id, *_ = scenario
    assert _fetch_client_name(client_id) == "Jane Doe"


def test_create_outreach_message_writes_personalized_content_with_real_values(scenario) -> None:
    client_id, run_id, campaign_id, _fund_id = scenario
    with SessionLocal() as session:
        run = session.get(GenerationRun, run_id)
        message = create_outreach_message(session, run, campaign_id=campaign_id)
        session.commit()
        message_id = message.message_id

    with SessionLocal() as session:
        stored = session.get(OutreachMessage, message_id)
        assert stored.personalized_content == {
            "subject": "Come back to Cytonn Money Market Fund",
            "body": "Dear Jane, we miss you.",
        }


def test_create_outreach_message_copies_ai_draft_content_unchanged(scenario) -> None:
    client_id, run_id, campaign_id, _fund_id = scenario
    with SessionLocal() as session:
        run = session.get(GenerationRun, run_id)
        message = create_outreach_message(session, run, campaign_id=campaign_id)
        session.commit()
        message_id = message.message_id

    with SessionLocal() as session:
        stored = session.get(OutreachMessage, message_id)
        assert stored.ai_draft_content == {
            "subject": "Come back to {{fund_name}}",
            "body": "Dear {{first_name}}, we miss you.",
        }


def test_create_outreach_message_stores_a_given_call_brief_unchanged(scenario) -> None:
    """render_call_brief's output carries no placeholder, so create_outreach_message
    stores it as-is, unlike ai_draft_content which personalize_content resolves."""
    client_id, run_id, campaign_id, _fund_id = scenario
    with SessionLocal() as session:
        run = session.get(GenerationRun, run_id)
        message = create_outreach_message(
            session, run, campaign_id=campaign_id, call_brief="Call brief: text"
        )
        session.commit()
        message_id = message.message_id

    with SessionLocal() as session:
        stored = session.get(OutreachMessage, message_id)
        assert stored.call_brief == "Call brief: text"


def test_create_outreach_message_leaves_call_brief_null_when_not_given(scenario) -> None:
    client_id, run_id, campaign_id, _fund_id = scenario
    with SessionLocal() as session:
        run = session.get(GenerationRun, run_id)
        message = create_outreach_message(session, run, campaign_id=campaign_id)
        session.commit()
        message_id = message.message_id

    with SessionLocal() as session:
        assert session.get(OutreachMessage, message_id).call_brief is None


def test_create_outreach_message_falls_back_when_the_vault_has_no_name(scenario) -> None:
    client_id, run_id, campaign_id, _fund_id = scenario
    with SessionLocal() as session:
        session.get(PiiVault, client_id).client_name = None
        session.commit()

    with SessionLocal() as session:
        run = session.get(GenerationRun, run_id)
        message = create_outreach_message(session, run, campaign_id=campaign_id)
        session.commit()
        message_id = message.message_id

    with SessionLocal() as session:
        stored = session.get(OutreachMessage, message_id)
        assert "Valued Client" in stored.personalized_content["body"]


@pytest.fixture
def message(scenario):
    """The scenario's run turned into a pending_review outreach_message."""
    _client_id, run_id, campaign_id, _fund_id = scenario
    with SessionLocal() as session:
        run = session.get(GenerationRun, run_id)
        created = create_outreach_message(session, run, campaign_id=campaign_id)
        session.commit()
        message_id = created.message_id

    yield message_id

    with SessionLocal() as session:
        session.execute(delete(AuditLog).where(AuditLog.entity_id == message_id))
        session.execute(delete(ReviewAction).where(ReviewAction.message_id == message_id))
        session.commit()


def test_list_pending_messages_returns_the_new_message(message) -> None:
    with SessionLocal() as session:
        pending, _next_cursor = list_pending_messages(session)
    assert message in [m.message_id for m in pending]


def test_list_pending_messages_filters_by_campaign(message, scenario) -> None:
    _client_id, _run_id, campaign_id, _fund_id = scenario
    with SessionLocal() as session:
        matched, _cursor = list_pending_messages(session, campaign_id=campaign_id)
        unmatched, _cursor = list_pending_messages(session, campaign_id=campaign_id + 1)
    assert message in [m.message_id for m in matched]
    assert message not in [m.message_id for m in unmatched]


def test_list_pending_messages_filters_by_status(message) -> None:
    with SessionLocal() as session:
        approved, _next_cursor = list_pending_messages(session, status="approved")
    assert message not in [m.message_id for m in approved]


def test_list_pending_messages_paginates_across_two_pages(scenario) -> None:
    """A page smaller than the result set returns a cursor that reaches the rest."""
    client_id, run_id, campaign_id, _fund_id = scenario
    with SessionLocal() as session:
        run = session.get(GenerationRun, run_id)
        first = create_outreach_message(session, run, campaign_id=campaign_id)
        session.commit()
        first_id = first.message_id

    with SessionLocal() as session:
        second_run = persist_generation_run(session, accepted_state(client_id), make_settings())
        second = create_outreach_message(session, second_run, campaign_id=campaign_id)
        session.commit()
        second_run_id, second_id = second_run.run_id, second.message_id

    try:
        with SessionLocal() as session:
            page_one, cursor_one = list_pending_messages(session, campaign_id=campaign_id, limit=1)
            assert [m.message_id for m in page_one] == [first_id]
            assert cursor_one is not None

            page_two, cursor_two = list_pending_messages(
                session, campaign_id=campaign_id, limit=1, cursor=cursor_one
            )
            assert [m.message_id for m in page_two] == [second_id]
            assert cursor_two is None
    finally:
        with SessionLocal() as session:
            session.execute(
                delete(ReviewAction).where(ReviewAction.message_id.in_([first_id, second_id]))
            )
            session.execute(
                delete(OutreachMessage).where(OutreachMessage.message_id.in_([first_id, second_id]))
            )
            session.execute(delete(GenerationRun).where(GenerationRun.run_id == second_run_id))
            session.commit()


def test_get_message_raises_when_not_found() -> None:
    with SessionLocal() as session, pytest.raises(MessageNotFound):
        get_message(session, "not-a-real-id")


def test_decide_approve_moves_status_to_approved(message) -> None:
    with SessionLocal() as session:
        decide(session, message, outcome="approve", reviewer_id="fa-1")
        session.commit()

    with SessionLocal() as session:
        assert session.get(OutreachMessage, message).status == "approved"


def test_decide_edit_approve_requires_edited_content(message) -> None:
    with SessionLocal() as session, pytest.raises(EditedContentRequired):
        decide(session, message, outcome="edit_approve", reviewer_id="fa-1")


def test_decide_edit_approve_stores_the_edited_content_and_approves(message) -> None:
    edited = {"subject": "Edited subject", "body": "Edited body"}
    with SessionLocal() as session:
        action = decide(
            session, message, outcome="edit_approve", reviewer_id="fa-1", edited_content=edited
        )
        session.commit()
        action_id = action.review_action_id

    with SessionLocal() as session:
        assert session.get(ReviewAction, action_id).edited_content == edited
        assert session.get(OutreachMessage, message).status == "approved"


def test_decide_reject_moves_status_to_rejected(message) -> None:
    with SessionLocal() as session:
        decide(session, message, outcome="reject", reviewer_id="fa-1", reason="not on brand")
        session.commit()

    with SessionLocal() as session:
        assert session.get(OutreachMessage, message).status == "rejected"


def test_decide_escalate_then_approve_is_allowed(message) -> None:
    """escalate and hold are waypoints, not terminal states."""
    with SessionLocal() as session:
        decide(session, message, outcome="escalate", reviewer_id="fa-1")
        session.commit()
        assert session.get(OutreachMessage, message).status == "escalated"

    with SessionLocal() as session:
        decide(session, message, outcome="approve", reviewer_id="lead-1")
        session.commit()
        assert session.get(OutreachMessage, message).status == "approved"

    with SessionLocal() as session:
        history = get_review_history(session, message)
    assert [a.outcome for a in history] == ["escalate", "approve"]


def test_decide_on_an_already_approved_message_raises(message) -> None:
    with SessionLocal() as session:
        decide(session, message, outcome="approve", reviewer_id="fa-1")
        session.commit()

    with SessionLocal() as session, pytest.raises(MessageAlreadyDecided):
        decide(session, message, outcome="reject", reviewer_id="fa-2")


def test_decide_with_an_invalid_outcome_raises(message) -> None:
    with SessionLocal() as session, pytest.raises(InvalidOutcome):
        decide(session, message, outcome="not_a_real_outcome", reviewer_id="fa-1")


def test_decide_edit_approve_keeps_both_the_ai_and_edited_versions(message) -> None:
    """The AI draft is never overwritten; the edit lives alongside it, not in place of it."""
    edited = {"subject": "Edited subject", "body": "Edited body"}
    with SessionLocal() as session:
        decide(session, message, outcome="edit_approve", reviewer_id="fa-1", edited_content=edited)
        session.commit()

    with SessionLocal() as session:
        stored_message = session.get(OutreachMessage, message)
        history = get_review_history(session, message)
    assert stored_message.ai_draft_content == {
        "subject": "Come back to {{fund_name}}",
        "body": "Dear {{first_name}}, we miss you.",
    }
    assert history[-1].edited_content == edited


def test_compute_edit_diff_only_includes_changed_fields() -> None:
    ai_draft = {"subject": "Come back", "body": "Dear you, we miss you."}
    edited = {"subject": "Come back", "body": "Dear Jane, we miss you."}
    diff = compute_edit_diff(ai_draft, edited)
    assert "subject" not in diff
    assert "body" in diff
    assert any("Jane" in line for line in diff["body"])


def test_decide_stamps_message_angle_and_priority_tier_from_the_generation_run(message) -> None:
    """The label describes the draft as generated, from the run, not from
    whatever the client's indicators currently resolve to."""
    with SessionLocal() as session:
        action = decide(session, message, outcome="approve", reviewer_id="fa-1")
        session.commit()
        action_id = action.review_action_id

    with SessionLocal() as session:
        stored = session.get(ReviewAction, action_id)
    assert stored.message_angle == "winback_habit"
    assert stored.priority_tier == "T2"


def test_decide_edit_approve_stores_a_per_field_edit_diff(message) -> None:
    edited = {"subject": "Come back to {{fund_name}}", "body": "Dear Jane, we miss you dearly."}
    with SessionLocal() as session:
        action = decide(
            session, message, outcome="edit_approve", reviewer_id="fa-1", edited_content=edited
        )
        session.commit()
        action_id = action.review_action_id

    with SessionLocal() as session:
        stored = session.get(ReviewAction, action_id)
    assert "subject" not in stored.edit_diff
    assert "body" in stored.edit_diff


def test_decide_approve_stores_no_edit_diff(message) -> None:
    with SessionLocal() as session:
        action = decide(session, message, outcome="approve", reviewer_id="fa-1")
        session.commit()
        action_id = action.review_action_id

    with SessionLocal() as session:
        stored = session.get(ReviewAction, action_id)
    assert stored.edit_diff is None


def test_decide_batch_approves_every_message_and_writes_one_action_each(scenario) -> None:
    client_id, run_id, campaign_id, _fund_id = scenario
    with SessionLocal() as session:
        run = session.get(GenerationRun, run_id)
        first = create_outreach_message(session, run, campaign_id=campaign_id)
        second_run = persist_generation_run(session, accepted_state(client_id), make_settings())
        second = create_outreach_message(session, second_run, campaign_id=campaign_id)
        session.commit()
        message_ids = [first.message_id, second.message_id]

    try:
        with SessionLocal() as session:
            result = decide_batch(session, message_ids, outcome="approve", reviewer_id="fa-1")
            session.commit()
        assert [a.message_id for a in result.decided] == message_ids
        assert result.failed == []

        with SessionLocal() as session:
            statuses = [session.get(OutreachMessage, mid).status for mid in message_ids]
        assert statuses == ["approved", "approved"]
    finally:
        with SessionLocal() as session:
            session.execute(delete(ReviewAction).where(ReviewAction.message_id.in_(message_ids)))
            session.execute(
                delete(OutreachMessage).where(OutreachMessage.message_id.in_(message_ids))
            )
            session.execute(delete(GenerationRun).where(GenerationRun.run_id == second_run.run_id))
            session.commit()


def test_decide_batch_reports_a_missing_message_without_failing_the_rest(message) -> None:
    with SessionLocal() as session:
        result = decide_batch(
            session, [message, "not-a-real-id"], outcome="approve", reviewer_id="fa-1"
        )
        session.commit()

    assert [a.message_id for a in result.decided] == [message]
    assert [f.message_id for f in result.failed] == ["not-a-real-id"]
    assert result.failed[0].error == "not_found"

    with SessionLocal() as session:
        assert session.get(OutreachMessage, message).status == "approved"


def test_decide_batch_reports_an_already_decided_message_without_failing_the_rest(
    scenario,
) -> None:
    client_id, run_id, campaign_id, _fund_id = scenario
    with SessionLocal() as session:
        run = session.get(GenerationRun, run_id)
        first = create_outreach_message(session, run, campaign_id=campaign_id)
        second_run = persist_generation_run(session, accepted_state(client_id), make_settings())
        second = create_outreach_message(session, second_run, campaign_id=campaign_id)
        session.commit()
        message_ids = [first.message_id, second.message_id]

    try:
        with SessionLocal() as session:
            decide(session, first.message_id, outcome="approve", reviewer_id="fa-1")
            session.commit()

        with SessionLocal() as session:
            result = decide_batch(session, message_ids, outcome="reject", reviewer_id="fa-2")
            session.commit()

        assert [a.message_id for a in result.decided] == [second.message_id]
        assert [f.message_id for f in result.failed] == [first.message_id]

        with SessionLocal() as session:
            assert session.get(OutreachMessage, first.message_id).status == "approved"
            assert session.get(OutreachMessage, second.message_id).status == "rejected"
    finally:
        with SessionLocal() as session:
            session.execute(delete(ReviewAction).where(ReviewAction.message_id.in_(message_ids)))
            session.execute(
                delete(OutreachMessage).where(OutreachMessage.message_id.in_(message_ids))
            )
            session.execute(delete(GenerationRun).where(GenerationRun.run_id == second_run.run_id))
            session.commit()


def test_decide_batch_rejects_edit_approve() -> None:
    with SessionLocal() as session, pytest.raises(EditedContentRequired):
        decide_batch(session, ["irrelevant"], outcome="edit_approve", reviewer_id="fa-1")


@pytest.mark.parametrize("outcome", ["approve", "edit_approve", "reject", "escalate", "hold"])
def test_decide_writes_a_matching_audit_row_for_every_outcome(message, outcome) -> None:
    edited_content = {"subject": "s", "body": "b"} if outcome == "edit_approve" else None
    with SessionLocal() as session:
        decide(
            session,
            message,
            outcome=outcome,
            reviewer_id="fa-1",
            edited_content=edited_content,
        )
        session.commit()

    with SessionLocal() as session:
        row = session.scalar(
            select(AuditLog).where(AuditLog.entity_id == message, AuditLog.action == outcome)
        )
    assert row is not None
    assert row.entity_type == "review_action"
    assert row.actor_id == "fa-1"
    assert row.detail["outcome"] == outcome
