from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.agents.channels import channel_spec
from app.agents.email_agent import build_system_prompt, build_system_prompt_blocks
from app.campaigns import generation
from app.campaigns.batch_generation import _prompt_blocks_for_channel
from app.campaigns.follow_up import BODY_LIMIT, follow_up_note
from app.rag.grounding import merge_chunks

NOTE = "FOLLOW UP NOTE"


class FakeSession:
    def __init__(self, rows):
        self.rows = rows

    def execute(self, statement):
        return SimpleNamespace(all=lambda: self.rows)


def earlier(step_no, subject, body, days_ago=7):
    return SimpleNamespace(
        step_no=step_no,
        sent_at=datetime.now(UTC) - timedelta(days=days_ago),
        ai_draft_content={"subject": subject, "body": body},
    )


def test_the_first_touch_has_no_note() -> None:
    assert follow_up_note(FakeSession([]), 1, 1) is None


def test_the_note_quotes_every_earlier_email_and_asks_for_the_rate() -> None:
    rows = [
        earlier(1, "First subject", "First body", 14),
        earlier(2, "Second subject", "Second body"),
    ]

    note = follow_up_note(FakeSession(rows), 1, 3)

    assert "follow-up email 3" in note
    assert "went out 7 days ago" in note
    assert "Subject: First subject" in note
    assert "Body: Second body" in note
    assert "current published rate" in note


def test_the_note_without_an_earlier_draft_skips_the_repeat_guidance() -> None:
    note = follow_up_note(FakeSession([]), 1, 2)

    assert "current published rate" in note
    assert "earlier emails" not in note


def test_the_note_trims_a_long_earlier_body() -> None:
    note = follow_up_note(FakeSession([earlier(1, "s", "x" * (BODY_LIMIT + 50))]), 1, 2)

    assert "x" * BODY_LIMIT in note
    assert "x" * (BODY_LIMIT + 1) not in note


def test_the_note_sits_before_the_facts_in_the_synchronous_prompt() -> None:
    prompt = build_system_prompt(angle="winback", prompt_variant=None, touch_note=NOTE)

    assert prompt.index(NOTE) < prompt.index("Facts you may cite")
    assert NOTE not in build_system_prompt(angle="winback", prompt_variant=None)


def test_the_note_stays_out_of_the_cached_block() -> None:
    blocks = build_system_prompt_blocks(angle="winback", prompt_variant=None, touch_note=NOTE)

    assert NOTE in blocks.dynamic
    assert NOTE not in blocks.cached


def test_a_non_email_channel_ignores_the_note() -> None:
    blocks = _prompt_blocks_for_channel(
        "sms", channel_spec("sms"), touch_note=NOTE, angle="winback", prompt_variant=None
    )

    assert NOTE not in blocks.cached + blocks.dynamic


def test_merge_chunks_drops_a_chunk_already_present() -> None:
    first = SimpleNamespace(chunk_id=1, text="a")
    second = SimpleNamespace(chunk_id=2, text="b")

    assert merge_chunks((first,), [first, second]) == (first, second)


def test_generation_keeps_the_given_orchestrator_unless_it_is_a_follow_up(monkeypatch) -> None:
    given, replacement, used = object(), object(), []
    enrollment = SimpleNamespace(campaign_id=1, client_id=2, enrollment_id=3)
    monkeypatch.setattr(generation, "_touch_plan_for", lambda *args: None)
    monkeypatch.setattr(generation, "_follow_up_orchestrator", lambda *args, **kwargs: replacement)
    monkeypatch.setattr(
        generation,
        "_generate_and_persist",
        lambda *args, **kwargs: used.append(kwargs["orchestrator"]),
    )

    def draft(step_no, **extra):
        generation.generate_for_enrollment(
            None, enrollment, step_no, orchestrator=given, channel="email", settings=None, **extra
        )

    draft(1)
    draft(2, follow_up=False)
    draft(2)

    assert used == [given, given, replacement]


def test_the_follow_up_agent_gets_the_note_and_the_rate_chunks(monkeypatch) -> None:
    captured = {}
    rate = SimpleNamespace(chunk_id=9, text="Yield 15.2%")

    def fake_agent(session, settings, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(channel="email")

    monkeypatch.setattr(generation, "build_default_agent", fake_agent)
    monkeypatch.setattr(generation, "follow_up_note", lambda session, enrollment_id, step_no: NOTE)
    monkeypatch.setattr(generation, "follow_up_rate_chunks", lambda session, product: [rate])
    monkeypatch.setattr(generation, "resolve_product", lambda session, client_id: "money market")
    monkeypatch.setattr(generation, "model_boundary_audit_sink", lambda session: None)

    orchestrator = generation._follow_up_orchestrator(
        None, SimpleNamespace(enrollment_id=1, client_id=2), 2, settings=None, tracer=None
    )

    assert orchestrator.channels() == ("email",)
    assert captured["extra_chunks"] == [rate]
    assert NOTE in captured["prompt_builder"](angle="winback", prompt_variant=None)
