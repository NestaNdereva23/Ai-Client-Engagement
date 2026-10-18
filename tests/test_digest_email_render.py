from __future__ import annotations

from datetime import date

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
from app.briefing.daily_brief import (
    BriefClient,
    BriefGroup,
    CalendarItem,
    DailyBrief,
    below_line_headline,
    calendar_headline,
    deposits_headline,
    half_balance_fact,
    large_withdrawals_headline,
    long_quiet_fact,
    overdue_calls_headline,
    relative_day,
    rhythm_phrase,
    time_span,
    warning_signs_fact,
)
from app.briefing.digest_email import render_digest_email
from app.privacy.fact_block import BOOK_TIERS
from app.privacy.llm_client import LLMClientError
from app.privacy.scanners import InboundLeak, scan_inbound
from app.routing.tiers import (
    ACTIVE_TIERS_IN_DISPLAY_ORDER,
    TIER_FREQUENT_DEPOSITOR,
    TIER_GRADUAL_DEPOSITOR,
)

TICKETING = "https://ticketing.example/"


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
    assert format_money(4_312_400.0) == "KSh 4,312,400"
    assert compact_money(4_312_400.0) == "KSh 4.3M"
    assert compact_money(812_000.0) == "KSh 812K"
    assert compact_money(400.0) == "KSh 400"


def _brief(**overrides) -> DailyBrief:
    base = {
        "fa_id": "81",
        "advisor_name": "Asha",
        "as_of": date(2026, 10, 14),
        "assigned_clients": 12,
        "worklist_clients": 8,
        "at_risk_kes": 4_300_000.0,
        "headlines": (
            large_withdrawals_headline(2, 1_860_000.0, "yesterday"),
            calendar_headline(
                [
                    CalendarItem(1, "call", "Call", "10:30"),
                    CalendarItem(2, "whatsapp", "WhatsApp follow-up", None),
                    CalendarItem(3, "reminder", "Reminder", None),
                ]
            ),
        ),
        "calendar": (),
        "groups": (
            BriefGroup(
                code="withdrawing",
                title="Clients taking money out while paying in less",
                clients=6,
                could_leave_kes=3_000_000.0,
                share=0.7,
                what_is_happening="x",
                what_to_do="y",
                worth_knowing="z",
                sharp_fact=(
                    "3 of them have taken out more than half of their balance "
                    "in the last 30 days, KSh 1.2M in total."
                ),
                examples=(BriefClient(777, 9992, "C777", 410_000.0, 90_000.0, "Took out a lot."),),
                warning_signs="Most common warning signs: heavy withdrawal (4 of 6).",
            ),
            BriefGroup(
                code="stopped",
                title="Clients who have stopped investing",
                clients=2,
                could_leave_kes=0.0,
                share=0.0,
                what_is_happening="x",
                what_to_do="y",
                worth_knowing="z",
                sharp_fact=None,
                examples=(),
            ),
        ),
        "rest_clients": 0,
        "rest_could_leave_kes": 0.0,
    }
    base.update(overrides)
    return DailyBrief(**base)


def test_the_subject_is_the_top_two_headlines() -> None:
    assert _brief().subject == "2 large withdrawals yesterday, 3 things on your calendar"
    assert _brief(headlines=()).subject == "Nothing new since yesterday"
    assert _brief(worklist_clients=0).subject == "Nothing on your worklist today"


def test_the_body_starts_with_what_happened_then_where_to_start() -> None:
    body = render_digest_email(brief=_brief()).text_body

    assert "Good morning Asha," in body
    assert "2 clients made large withdrawals yesterday. KSh 1.9M in total." in body
    assert (
        "You have 3 things on your calendar today. "
        "A call at 10:30, a WhatsApp follow-up and a reminder." in body
    )
    assert "1. Clients taking money out while paying in less" in body
    assert "   6 clients, KSh 3.0M could leave." in body
    assert "   Most common warning signs: heavy withdrawal (4 of 6)." in body
    assert "   3 of them have taken out more than half of their balance" in body
    assert "2. Clients who have stopped investing\n   2 clients.\n" in body
    assert "By tier" not in body


def test_a_quiet_morning_still_says_where_to_start() -> None:
    body = render_digest_email(brief=_brief(headlines=())).text_body

    assert "Nothing new since yesterday." in body
    assert "Where to start" in body


def test_the_link_shows_only_with_a_console_url_and_a_worklist() -> None:
    linked = render_digest_email(brief=_brief(), ticketing_base_url=TICKETING).text_body
    plain = render_digest_email(brief=_brief()).text_body
    empty = render_digest_email(
        brief=_brief(worklist_clients=0, headlines=(), groups=()),
        ticketing_base_url=TICKETING,
    ).text_body

    assert (
        "Open today's brief: https://ticketing.example/dashboard?view=ai-outreach&section=at-risk"
    ) in linked
    assert "http" not in plain
    assert "http" not in empty
    assert "Nothing on your worklist this morning." in empty


def test_the_month_end_line_shows_only_when_there_is_a_pattern() -> None:
    pattern = MonthEndPattern(usual_clients=40, not_yet_clients=12)

    with_pattern = render_digest_email(brief=_brief(month_end=pattern)).text_body
    without = render_digest_email(brief=_brief()).text_body

    assert "Month end: 40 clients usually top up in the last week of the month." in with_pattern
    assert "12 have not yet this month." in with_pattern
    assert "Month end" not in without


def test_the_body_carries_no_client_names_or_codes() -> None:
    body = render_digest_email(brief=_brief(), ticketing_base_url=TICKETING).text_body

    assert "C777" not in body
    assert "777" not in body
    assert "Took out a lot" not in body


def test_headlines_read_correctly_for_one_and_for_many() -> None:
    one = large_withdrawals_headline(1, 150_000.0, "yesterday")
    many = large_withdrawals_headline(4, 2_500_000.0, "since 12 October")

    assert one.lead == "1 client made a large withdrawal yesterday."
    assert one.text == "KSh 150K in total."
    assert many.lead == "4 clients made large withdrawals since 12 October."
    assert one.short == "1 large withdrawal yesterday"
    assert below_line_headline(1).lead == (
        "1 client's balance fell below the capital preservation line."
    )
    assert below_line_headline(3).lead == (
        "3 clients' balances fell below the capital preservation line."
    )
    assert overdue_calls_headline(1).lead == "1 planned call is overdue."
    assert overdue_calls_headline(2).lead == "2 planned calls are overdue."
    assert deposits_headline(3, 540_000.0, "yesterday").text == (
        "3 clients made a deposit yesterday, KSh 540K in total."
    )


def test_the_calendar_headline_names_up_to_three_things_and_counts_the_rest() -> None:
    items = [
        CalendarItem(1, "call", "Call", "10:30"),
        CalendarItem(2, "meeting", "Meeting", "14:00"),
        CalendarItem(3, "whatsapp", "WhatsApp follow-up", None),
        CalendarItem(4, "reminder", "Reminder", None),
        CalendarItem(5, "email", "Email", None),
    ]

    headline = calendar_headline(items)

    assert headline.lead == "You have 5 things on your calendar today."
    assert headline.text == "A call at 10:30, a meeting at 14:00, a WhatsApp follow-up and 2 more."
    assert calendar_headline(items[:1]).lead == "You have 1 thing on your calendar today."


def test_warning_signs_name_the_signals_behind_the_amount() -> None:
    assert warning_signs_fact({}, 3) is None
    assert warning_signs_fact({"sig_broken_pattern": 1}, 1) == (
        "Warning signs: broke their own pattern."
    )
    assert warning_signs_fact({"sig_heavy_withdrawal": 1, "sig_broken_pattern": 1}, 1) == (
        "Warning signs: broke their own pattern and heavy withdrawal."
    )
    assert warning_signs_fact(
        {"sig_shrinking": 2, "sig_heavy_withdrawal": 5, "sig_dormant": 1}, 6
    ) == ("Most common warning signs: heavy withdrawal (5 of 6), shrinking deposits (2 of 6).")


def test_the_sharp_facts_say_nothing_when_there_is_nothing_to_say() -> None:
    assert half_balance_fact(0, 0.0, 0, "yesterday") is None
    assert long_quiet_fact(0) is None
    assert half_balance_fact(1, 800_000.0, 1, "yesterday") == (
        "1 of them has taken out more than half of their balance in the last 30 days, "
        "KSh 800K in total. 1 of them did it yesterday."
    )


def test_days_and_rhythms_read_in_plain_words() -> None:
    today = date(2026, 10, 15)

    assert relative_day(date(2026, 10, 14), today) == "yesterday"
    assert relative_day(date(2026, 10, 9), today) == "6 days ago"
    assert relative_day(date(2026, 9, 3), today) == "on 3 September"
    assert time_span(5) == "5 days"
    assert time_span(98) == "3 months"
    assert rhythm_phrase(30) == "about every month"


def test_the_book_briefing_names_the_biggest_risk_and_keeps_its_limits() -> None:
    text = render_book_briefing(_facts(month_end=MonthEndPattern(10, 3)))

    assert "BOOK BRIEFING  |  Asha" in text
    assert "ACTIVE ACTIVE  |  Deepen and diversify" in text
    assert "Frequent Depositors hold the most at risk: KSh 3,000,000 (70% of the total)." in text
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


def test_the_studio_instructions_open_the_book_summary_prompt() -> None:
    client = _ScriptedClient("You hold 2 clients on the worklist.")

    draft_book_narrative(
        _precise_facts(), client, fallback_text="plain", instructions="STUDIO RULES"
    )

    assert client.seen[0][0].startswith("STUDIO RULES")


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
