"""LLM-as-judge: scoring a placeholder-only draft against the fixed rubric.

These prove the rubric text is stable (what llmops.versions hashes to
register a rubric_versions row), the judge prompt renders retrieved facts (or
says none were retrieved), judge_draft sends the rubric as system and the
draft as user and returns validated scores, a malformed judge response
propagates as EvaluationParseError, and a judge response that echoes a
contact channel is blocked the same way an EmailAgent draft would be.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from app.llmops.judge import build_judge_prompt, judge_draft, rubric_text
from app.privacy.scanners import OutboundLeak
from app.schemas.evaluation import EvaluationParseError


@dataclass(frozen=True)
class FakeChunk:
    chunk_id: int
    text: str


class ScriptedLLMClient:
    model = "judge-stub"

    def __init__(self, response: str) -> None:
        self._response = response
        self.calls: list[dict] = []

    def generate(self, *, system: str, user: str) -> str:
        self.calls.append({"system": system, "user": user})
        return self._response


def score_json(**overrides) -> str:
    defaults = {"tone": 4, "compliance": 5, "grounding": 5, "personalization": 3, "notes": "fine"}
    defaults.update(overrides)
    return json.dumps(defaults)


def test_rubric_text_is_stable_across_calls() -> None:
    assert rubric_text() == rubric_text()


def test_build_judge_prompt_renders_retrieved_facts() -> None:
    chunks = [FakeChunk(chunk_id=1, text="the fund yielded 11.35% this week")]
    prompt = build_judge_prompt(chunks=chunks)
    assert rubric_text() in prompt
    assert "11.35%" in prompt


def test_build_judge_prompt_notes_when_no_facts_were_retrieved() -> None:
    prompt = build_judge_prompt(chunks=())
    assert "no facts were retrieved" in prompt


def test_judge_draft_sends_the_rubric_as_system_and_the_draft_as_user() -> None:
    llm = ScriptedLLMClient(score_json())
    draft = "Dear {{first_name}}, your {{fund_name}} awaits."

    result = judge_draft(llm, draft=draft, chunks=())

    assert llm.calls[0]["system"] == build_judge_prompt(chunks=())
    assert llm.calls[0]["user"] == draft
    assert (result.tone, result.compliance, result.grounding, result.personalization) == (
        4,
        5,
        5,
        3,
    )


def test_judge_draft_raises_on_malformed_output() -> None:
    llm = ScriptedLLMClient("not json at all")
    with pytest.raises(EvaluationParseError):
        judge_draft(llm, draft="Dear {{first_name}}, welcome back.", chunks=())


def test_judge_draft_blocks_a_response_that_echoes_a_contact_channel() -> None:
    leaking = json.dumps(
        {
            "tone": 4,
            "compliance": 5,
            "grounding": 5,
            "personalization": 3,
            "notes": "reach out at winback@example.com",
        }
    )
    llm = ScriptedLLMClient(leaking)
    with pytest.raises(OutboundLeak):
        judge_draft(llm, draft="Dear {{first_name}}, welcome back.", chunks=())
