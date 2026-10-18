from __future__ import annotations

import pytest

from app.briefing.off_limits import find_off_limits
from app.briefing.tone_cue import tone_cue
from app.privacy.fact_block import AssistantFactBlock

CASES = [
    ({"sig_heavy_withdrawal": True}, "Do not ask them to add money"),
    (
        {"largest_withdrawal_size_band": "large", "last_withdrawal_recency_band": "this_week"},
        "Do not ask them to add money",
    ),
    ({"sig_shrinking": True}, "not about adding money"),
    ({"deposit_trend_band": "falling"}, "not about adding money"),
    ({"sig_dormant": True}, "getting back in touch"),
    ({"recency_band": "1-2y"}, "getting back in touch"),
    ({"sig_broken_pattern": True}, "One short, easy question"),
    ({"near_preservation_line": True}, "calm and reassuring"),
    ({"deposit_rhythm_band": "weekly", "deposit_trend_band": "flat"}, "upbeat and appreciative"),
]


@pytest.mark.parametrize(("bands", "expected"), CASES)
def test_the_bands_pick_the_tone(bands, expected) -> None:
    cue = tone_cue(AssistantFactBlock(**bands))

    assert expected in cue
    assert find_off_limits(cue) is None


def test_a_heavy_withdrawer_is_not_asked_for_money_even_if_they_save_regularly() -> None:
    facts = AssistantFactBlock(
        sig_heavy_withdrawal=True, deposit_rhythm_band="weekly", deposit_trend_band="rising"
    )

    assert "Do not ask them to add money" in tone_cue(facts)


def test_with_nothing_to_go_on_there_is_no_cue() -> None:
    assert tone_cue(AssistantFactBlock()) is None
