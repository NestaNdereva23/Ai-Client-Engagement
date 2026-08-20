"""Tests for briefing/digest_email.py: the wording of the morning email.

Pure rendering, so none of this touches a database or a mail server.
"""

from __future__ import annotations

from app.briefing.digest_email import (
    DigestEmailSummary,
    compact_money,
    format_money,
    rank_signals,
    render_digest_email,
    render_subject,
)

CONSOLE = "http://console.example/"


def _summary(**overrides) -> DigestEmailSummary:
    base = {
        "clients_to_call": 2,
        "total_at_risk": 4_300_000.0,
        "new_or_escalated": 1,
        "watchlist_clients": 7,
        "watchlist_at_risk": 900_000.0,
        "signals": rank_signals([["dormant", "shrinking"], ["dormant"]]),
    }
    base.update(overrides)
    return DigestEmailSummary(**base)


def test_the_top_signal_is_the_most_repeated_tag() -> None:
    signals = rank_signals([["dormant", "shrinking"], ["dormant"], ["dormant", "never_repeated"]])

    assert signals[0].tag == "dormant"
    assert signals[0].clients == 3
    assert signals[0].label == "No deposit in 12 months"
    assert signals[1].clients == 1


def test_a_client_counts_once_per_tag_however_often_it_repeats() -> None:
    signals = rank_signals([["dormant", "dormant"]])

    assert [(s.tag, s.clients) for s in signals] == [("dormant", 1)]


def test_signals_with_the_same_count_are_ordered_by_tag() -> None:
    signals = rank_signals([["shrinking", "dormant"]])

    assert [s.tag for s in signals] == ["dormant", "shrinking"]


def test_money_formats_for_the_body_and_the_subject() -> None:
    assert format_money(4_312_400.0) == "KES 4,312,400"
    assert compact_money(4_312_400.0) == "KES 4.3M"
    assert compact_money(812_000.0) == "KES 812K"
    assert compact_money(400.0) == "KES 400"


def test_the_subject_carries_the_count_and_the_money() -> None:
    assert render_subject(_summary()) == "Morning Digest: 2 At-Risk Clients Holding KES 4.3M"


def test_the_subject_is_singular_for_one_client() -> None:
    summary = _summary(clients_to_call=1, total_at_risk=800_000.0)
    assert render_subject(summary) == "Morning Digest: 1 At-Risk Client Holding KES 800K"


def test_the_subject_says_so_when_nobody_is_at_risk() -> None:
    summary = _summary(clients_to_call=0, total_at_risk=0.0, signals=rank_signals([]))
    assert render_subject(summary) == "Morning Digest: No At-Risk Clients Today"


def test_the_body_names_the_top_signal_and_the_runner_up() -> None:
    email = render_digest_email(fa_id=81, advisor_name="Asha", summary=_summary())

    assert "Good morning Asha," in email.text_body
    assert "Clients to call today: 2" in email.text_body
    assert "Money at risk: KES 4,300,000" in email.text_body
    assert "Most common reason: No deposit in 12 months (2 clients)" in email.text_body
    assert "Next most common: Shrinking deposits (1 clients)" in email.text_body
    assert "New or worse since yesterday: 1" in email.text_body


def test_the_body_carries_the_average_at_risk_per_client() -> None:
    email = render_digest_email(
        fa_id=81, advisor_name="Asha", summary=_summary(clients_to_call=4, total_at_risk=800_000.0)
    )

    assert "Average at risk per client: KES 200,000" in email.text_body


def test_the_body_carries_no_client_or_fund_names() -> None:
    email = render_digest_email(fa_id=81, advisor_name="Asha", summary=_summary())

    assert "Call list" not in email.text_body
    assert "Fund" not in email.text_body


def test_the_watchlist_is_a_count_and_a_link() -> None:
    email = render_digest_email(
        fa_id=81, advisor_name="Asha", summary=_summary(), console_base_url=CONSOLE
    )

    assert (
        "Watchlist: 7 clients, KES 900,000 (http://console.example/digest/fa:81)" in email.text_body
    )


def test_a_call_list_link_points_to_the_console() -> None:
    email = render_digest_email(
        fa_id=81, advisor_name="Asha", summary=_summary(), console_base_url=CONSOLE
    )

    assert "See today's full call list: http://console.example/digest/fa:81" in email.text_body


def test_no_links_are_rendered_without_a_console_url() -> None:
    email = render_digest_email(fa_id=81, advisor_name="Asha", summary=_summary())

    assert "http" not in email.text_body


def test_an_empty_queue_renders_the_short_note_and_no_average_or_link() -> None:
    summary = _summary(
        clients_to_call=0, total_at_risk=0.0, new_or_escalated=0, signals=rank_signals([])
    )

    email = render_digest_email(
        fa_id=81, advisor_name="Asha", summary=summary, console_base_url=CONSOLE
    )

    assert "Nothing on your call list this morning." in email.text_body
    assert "Average at risk per client" not in email.text_body
    assert "full call list" not in email.text_body
    assert "Watchlist: 7 clients" in email.text_body
