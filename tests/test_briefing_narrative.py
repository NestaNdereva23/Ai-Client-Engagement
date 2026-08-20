"""briefing.narrative (AM15): the one place a briefing crosses the model
boundary. These prove the prompt renders the given RiskFactBlock's own
facts, draft_narrative calls run_model_boundary the same way agents.graph's
generate() node does, a clean narrative passes through, and a boundary leak,
an ungrounded digit, or the provider simply failing all fall back to the
caller's deterministic text rather than raising -- the AM15.5 grounding
test lives here (test_an_untraceable_digit_falls_back_to_the_deterministic_text).
"""

from __future__ import annotations

import pytest

from app.briefing.narrative import (
    MalformedNarrative,
    NarrativeResult,
    UngroundedNarrative,
    draft_narrative,
    narrative_prose_check,
    narrative_traceable_digits_check,
)
from app.briefing.narrative_prompt import build_narrative_prompt, narrative_facts
from app.privacy.fact_block import RiskFactBlock
from app.privacy.llm_client import LLMClientError

FALLBACK_TEXT = "CLIENT BRIEFING (deterministic fallback text)"


class ScriptedLLMClient:
    model = "briefing-stub"

    def __init__(self, response: str | Exception) -> None:
        self._response = response
        self.calls: list[dict] = []

    def generate(self, *, system: str, user: str) -> str:
        self.calls.append({"system": system, "user": user})
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


def facts(**overrides) -> RiskFactBlock:
    defaults = {
        "risk_band": "Watch",
        "route": "fa_watchlist",
        "recency_band": "1-3m",
        "sig_dormant": True,
        "sig_heavy_withdrawal": False,
    }
    defaults.update(overrides)
    return RiskFactBlock(**defaults)


def test_build_narrative_prompt_lists_the_given_facts_in_plain_wording() -> None:
    prompt = build_narrative_prompt(facts())
    assert "Overall attention level: Watch" in prompt
    assert "Time since their last deposit: 1-3m" in prompt
    # The route reads as an instruction, not as an internal route code.
    assert "keep this client on the watchlist" in prompt
    assert "fa_watchlist" not in prompt
    # A fired signal is described; one that did not fire is simply absent.
    assert "they have not deposited for about a year or more" in prompt
    assert "sig_dormant" not in prompt
    assert "large share of what they held" not in prompt


def test_narrative_facts_drops_the_signals_that_did_not_fire() -> None:
    payload = narrative_facts(facts())
    assert payload["sig_dormant"] is True
    assert "sig_heavy_withdrawal" not in payload


def test_narrative_prose_check_rejects_a_reply_that_is_not_prose() -> None:
    narrative_prose_check("This client has been quiet for a while now.")
    with pytest.raises(MalformedNarrative):
        narrative_prose_check('{"client_balance_status": "Institutional"}')
    with pytest.raises(MalformedNarrative):
        narrative_prose_check("   ")


def test_draft_narrative_falls_back_when_the_model_returns_json() -> None:
    llm = ScriptedLLMClient('{"client_balance_status": "Institutional"}')

    result = draft_narrative(facts(), llm, fallback_text=FALLBACK_TEXT, entity_id="94001")

    assert result == NarrativeResult(text=FALLBACK_TEXT, mode="deterministic_fallback")


def test_build_narrative_prompt_says_when_there_are_no_facts() -> None:
    prompt = build_narrative_prompt(RiskFactBlock())
    assert "no facts are available" in prompt


def test_narrative_traceable_digits_check_passes_a_digit_from_an_allowed_band() -> None:
    # "1" and "3" both appear inside the given recency_band, "1-3m".
    narrative_traceable_digits_check("Their deposits have gone quiet recently.", facts())
    narrative_traceable_digits_check("Roughly in the 1-3m window.", facts())


def test_narrative_traceable_digits_check_rejects_an_untraceable_digit() -> None:
    with pytest.raises(UngroundedNarrative):
        narrative_traceable_digits_check("Deposits are up 42% this quarter.", facts())


def test_draft_narrative_returns_the_model_text_when_it_passes_every_check() -> None:
    llm = ScriptedLLMClient("This client has gone quiet recently and broke their own pattern.")

    result = draft_narrative(facts(), llm, fallback_text=FALLBACK_TEXT, entity_id="94001")

    assert result == NarrativeResult(
        text="This client has gone quiet recently and broke their own pattern.",
        mode="narrative",
    )
    assert llm.calls[0]["system"] == build_narrative_prompt(facts())


def test_draft_narrative_falls_back_when_the_model_echoes_a_contact_channel() -> None:
    llm = ScriptedLLMClient("Reach out at winback@example.com about this one.")

    result = draft_narrative(facts(), llm, fallback_text=FALLBACK_TEXT, entity_id="94001")

    assert result == NarrativeResult(text=FALLBACK_TEXT, mode="deterministic_fallback")


def test_draft_narrative_falls_back_on_an_untraceable_digit() -> None:
    """The AM15.5 grounding test: a narrative asserting a fact (here, a
    number) absent from the RiskFactBlock it was generated from is never
    returned as-is -- it falls back to the deterministic text instead.
    """
    llm = ScriptedLLMClient("This client's balance is down 42% since last quarter.")

    result = draft_narrative(facts(), llm, fallback_text=FALLBACK_TEXT, entity_id="94001")

    assert result == NarrativeResult(text=FALLBACK_TEXT, mode="deterministic_fallback")


def test_draft_narrative_falls_back_when_the_model_call_fails() -> None:
    llm = ScriptedLLMClient(LLMClientError("model request failed: timeout"))

    result = draft_narrative(facts(), llm, fallback_text=FALLBACK_TEXT, entity_id="94001")

    assert result == NarrativeResult(text=FALLBACK_TEXT, mode="deterministic_fallback")


def test_draft_narrative_sends_the_facts_as_the_scanned_payload() -> None:
    llm = ScriptedLLMClient("A short clean narrative with no figures at all.")

    draft_narrative(facts(), llm, fallback_text=FALLBACK_TEXT, entity_id="94001")

    # The user turn is the rendered, already-scanned fact payload -- the
    # same discipline as every other run_model_boundary call in this
    # codebase, never the raw dataclass or a hand-built string.
    assert "risk_band: Watch" in llm.calls[0]["user"]
    assert "recency_band: 1-3m" in llm.calls[0]["user"]
    # Only what actually holds for this client crosses; a signal that did
    # not fire is not sent as "false" for the model to narrate.
    assert "sig_heavy_withdrawal" not in llm.calls[0]["user"]


def test_draft_narrative_audits_every_crossing() -> None:
    records = []
    llm = ScriptedLLMClient("A short clean narrative with no figures at all.")

    draft_narrative(
        facts(), llm, fallback_text=FALLBACK_TEXT, entity_id="94001", audit=records.append
    )

    assert len(records) == 1
    assert records[0].entity_id == "94001"
    assert records[0].inbound == "pass"
    assert records[0].outbound == "pass"
