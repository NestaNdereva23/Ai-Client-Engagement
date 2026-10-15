from __future__ import annotations

import pytest

from app.briefing.book import (
    BookFacts,
    MonthEndPattern,
    TierLine,
    compact_money,
    format_money,
    render_book_briefing,
)
from app.briefing.book_narrative import draft_book_narrative, to_book_fact_block
from app.briefing.digest_email import render_digest_email, render_subject
from app.privacy.fact_block import BOOK_TIERS
from app.privacy.llm_client import LLMClientError
from app.privacy.scanners import InboundLeak, scan_inbound
from app.routing.tiers import (
    ACTIVE_TIERS_IN_DISPLAY_ORDER,
    TIER_FREQUENT_DEPOSITOR,
    TIER_GRADUAL_DEPOSITOR,
)

CONSOLE = "http://console.example/"


def _tier(tier, segment, label, clients, balance, at_risk) -> TierLine:
    return TierLine(
        tier=tier,
        segment=segment,
        label=label,
        goal="A goal",
        clients=clients,
        balance_kes=balance,
        at_risk_kes=at_risk,
    )


def _facts(**overrides) -> BookFacts:
    base = {
        "advisor_name": "Asha",
        "assigned_clients": 12,
        "tiers": [
            _tier(
                TIER_FREQUENT_DEPOSITOR,
                "active_active",
                "Frequent Depositors",
                2,
                9_000_000.0,
                3_000_000.0,
            ),
            _tier(
                TIER_GRADUAL_DEPOSITOR,
                "active_dormant",
                "Gradual Depositors",
                6,
                4_000_000.0,
                1_300_000.0,
            ),
        ],
        "held_out_clients": 4,
    }
    base.update(overrides)
    return BookFacts(**base)


def test_money_formats_for_the_body_and_the_subject() -> None:
    assert format_money(4_312_400.0) == "KES 4,312,400"
    assert compact_money(4_312_400.0) == "KES 4.3M"
    assert compact_money(812_000.0) == "KES 812K"
    assert compact_money(400.0) == "KES 400"


def test_the_subject_carries_the_worklist_count_and_the_money_at_risk() -> None:
    assert (
        render_subject(_facts()) == "Morning Digest: 8 Clients On Your Worklist, KES 4.3M At Risk"
    )


def test_the_body_lists_each_tier_and_the_clients_held_out() -> None:
    body = render_digest_email(fa_id="81", facts=_facts()).text_body

    assert "Good morning Asha," in body
    assert "Clients on your worklist: 8 of 12" in body
    assert "Frequent Depositors: 2 clients, KES 9,000,000 held, KES 3,000,000 at risk" in body
    assert "Not on the worklist: 4 clients" in body


def test_the_link_shows_only_with_a_console_url_and_a_worklist() -> None:
    linked = render_digest_email(fa_id="81", facts=_facts(), console_base_url=CONSOLE).text_body
    plain = render_digest_email(fa_id="81", facts=_facts()).text_body
    empty = render_digest_email(
        fa_id="81", facts=_facts(tiers=[]), console_base_url=CONSOLE
    ).text_body

    assert "See your clients in order: http://console.example/digest/fa:81" in linked
    assert "http" not in plain
    assert "http" not in empty
    assert "Nothing on your worklist this morning." in empty


def test_the_month_end_line_shows_only_when_there_is_a_pattern() -> None:
    pattern = MonthEndPattern(usual_clients=40, not_yet_clients=12)

    with_pattern = render_digest_email(fa_id="81", facts=_facts(month_end=pattern)).text_body
    without = render_digest_email(fa_id="81", facts=_facts()).text_body

    assert "Month end: 40 clients usually top up in the last week of the month." in with_pattern
    assert "12 have not yet this month." in with_pattern
    assert "Month end" not in without


def test_the_body_carries_no_client_names_or_codes() -> None:
    body = render_digest_email(fa_id="81", facts=_facts(), console_base_url=CONSOLE).text_body

    assert "Fund" not in body
    assert "Client C" not in body


def test_the_book_briefing_names_the_biggest_risk_and_keeps_its_limits() -> None:
    text = render_book_briefing(_facts(month_end=MonthEndPattern(10, 3)))

    assert "BOOK BRIEFING  |  Asha" in text
    assert "ACTIVE ACTIVE  |  Deepen and diversify" in text
    assert "Frequent Depositors hold the most at risk: KES 3,000,000 (70% of the total)." in text
    assert "6 of 8 clients (75%) are in Active Dormant" in text
    assert "10 clients usually top up in the last week of the month." in text
    assert "! Not on the worklist: 4 clients" in text


class _ScriptedClient:
    model = "scripted"

    def __init__(self, reply: str | Exception) -> None:
        self.reply = reply
        self.seen: list[tuple[str, str]] = []

    def generate(self, *, system: str, user: str) -> str:
        self.seen.append((system, user))
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def _precise_facts() -> BookFacts:
    return _facts(
        tiers=[
            _tier(
                TIER_FREQUENT_DEPOSITOR,
                "active_active",
                "Frequent Depositors",
                2,
                9_123_456.0,
                3_049_999.0,
            )
        ],
        month_end=MonthEndPattern(usual_clients=40, not_yet_clients=12),
    )


def test_the_model_payload_holds_only_counts_and_rounded_totals() -> None:
    payload = to_book_fact_block(_precise_facts()).to_dict()

    assert payload["tiers"] == [
        {
            "tier": TIER_FREQUENT_DEPOSITOR,
            "clients": 2,
            "balance_kes": 9_100_000,
            "at_risk_kes": 3_000_000,
        }
    ]
    assert "Asha" not in str(payload)
    scan_inbound(payload)


def test_the_scanner_refuses_a_book_payload_with_an_exact_amount_or_a_name() -> None:
    payload = to_book_fact_block(_precise_facts()).to_dict()
    exact = {**payload, "tiers": [{**payload["tiers"][0], "balance_kes": 9_123_456}]}
    named = {**payload, "advisor_name": "Asha"}

    with pytest.raises(InboundLeak):
        scan_inbound(exact)
    with pytest.raises(InboundLeak):
        scan_inbound(named)


def test_the_book_summary_comes_back_as_narrative_when_every_figure_is_in_the_facts() -> None:
    client = _ScriptedClient(
        "You hold 2 clients on the worklist, and 12 of the month end group are yet to top up."
    )

    result = draft_book_narrative(_precise_facts(), client, fallback_text="plain")

    assert result.mode == "narrative"
    system, user = client.seen[0]
    assert "Asha" not in system + user
    assert "about KES 9.1 million held" in system


def test_the_book_summary_falls_back_on_a_figure_that_is_not_in_the_facts() -> None:
    client = _ScriptedClient("About 73 clients are at risk.")

    result = draft_book_narrative(_precise_facts(), client, fallback_text="plain")

    assert (result.text, result.mode) == ("plain", "deterministic_fallback")


def test_the_book_summary_falls_back_when_the_model_fails_or_answers_with_data() -> None:
    failed = draft_book_narrative(
        _precise_facts(), _ScriptedClient(LLMClientError("down")), fallback_text="plain"
    )
    as_data = draft_book_narrative(
        _precise_facts(), _ScriptedClient('{"summary": "2 clients"}'), fallback_text="plain"
    )

    assert failed.mode == as_data.mode == "deterministic_fallback"


def test_the_book_tiers_the_model_may_see_match_the_real_worklist_tiers() -> None:
    assert BOOK_TIERS == ACTIVE_TIERS_IN_DISPLAY_ORDER
