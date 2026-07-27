"""Normalizing report text and pulling the issue, date, and title.

These run on strings, not PDFs, so they hold in CI: smart punctuation folds to
ASCII, lone page-number lines drop, the issue tag and its week date are read,
and the title is trimmed before the Cytonn Weekly tag.
"""

from __future__ import annotations

from datetime import date

from app.rag.loader import _clean_title, extract_issue, issue_to_date, normalize


def test_normalize_folds_smart_punctuation() -> None:
    out = normalize("Investors’ preference – the “short” paper")
    assert out == 'Investors\' preference - the "short" paper'


def test_normalize_drops_lone_page_numbers_and_collapses_space() -> None:
    out = normalize(" 1 \nFixed   Income:  T-bills\n12\nrose\n")
    assert "\n1\n" not in out and not out.startswith("1")
    assert "Fixed Income: T-bills" in out
    assert "\n12\n" not in out


def test_extract_issue_reads_the_weekly_tag() -> None:
    assert extract_issue("Some Title, &Cytonn Weekly #29/2026 Executive Summary") == "29/2026"
    assert extract_issue("no tag here") is None


def test_issue_to_date_is_the_week_monday() -> None:
    assert issue_to_date("29/2026") == date.fromisocalendar(2026, 29, 1)
    assert issue_to_date(None) is None
    assert issue_to_date("garbage") is None


def test_clean_title_trims_the_weekly_tag() -> None:
    assert (
        _clean_title("Kenya FY'2025 Listed Insurance Report, &Cytonn Weekly #27/2026 Executive")
        == "Kenya FY'2025 Listed Insurance Report"
    )
    assert _clean_title("Navigating Markets, &Cytonn Weekly #29/2026") == "Navigating Markets"
