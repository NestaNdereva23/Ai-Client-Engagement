"""A short tone cue for the email writer, picked by plain rules from the fact bands.
The cue names no fact, so the writer learns how to sound without learning why."""

from __future__ import annotations

from app.privacy.fact_block import AssistantFactBlock

BIG_WITHDRAWALS = ("large", "nearly_all")
RECENT_WITHDRAWALS = ("this_week", "this_month")
LONG_QUIET = ("3-6m", "6-12m", "1-2y", "2y+")
REGULAR_RHYTHMS = ("weekly", "monthly")
STEADY_TRENDS = ("flat", "rising")


def tone_cue(facts: AssistantFactBlock) -> str | None:
    if facts.sig_heavy_withdrawal or (
        facts.largest_withdrawal_size_band in BIG_WITHDRAWALS
        and facts.last_withdrawal_recency_band in RECENT_WITHDRAWALS
    ):
        return "Be gentle and unhurried. Do not ask them to add money."
    if facts.sig_shrinking or facts.deposit_trend_band == "falling":
        return "Be supportive and unhurried. Talk about their plans, not about adding money."
    if facts.sig_dormant or facts.sig_never_repeated or facts.recency_band in LONG_QUIET:
        return "Be warm and welcoming, like getting back in touch with someone you value."
    if facts.sig_broken_pattern:
        return "Be warm and light. One short, easy question is enough."
    if facts.near_preservation_line:
        return "Be calm and reassuring."
    if facts.deposit_rhythm_band in REGULAR_RHYTHMS and facts.deposit_trend_band in STEADY_TRENDS:
        return "Be upbeat and appreciative. A light touch is enough."
    return None
