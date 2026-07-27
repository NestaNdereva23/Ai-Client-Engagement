"""Chunking a weekly report into section-tagged, retrieval-sized pieces.

The reports are topic-sectioned prose whose sections open with inline labels
like "Fixed Income:" and "Investment Updates:". These pure tests use synthetic
text following that convention, so they do not depend on any PDF, and check the
running section tag, the issue and page metadata, and the target chunk size.
"""

from __future__ import annotations

from app.rag.chunking import TARGET_CHARS, chunk_report

# A long Fixed Income section (spills into several chunks), then the money-market
# update carrying the fund yield, then a short Equities note. Two pages.
_FIXED_INCOME = "Fixed Income: " + (
    "T-bills were oversubscribed this week at a rate of 137.5 percent. " * 45
)
_PAGE_2 = (
    "Investment Updates: Cytonn Money Market Fund closed the week at a yield of "
    "11.35 percent per annum. To invest dial the short code. "
    "Equities: The NASI index gained modestly over the week on foreign inflows."
)
PAGES = [_FIXED_INCOME, _PAGE_2]


def test_chunks_carry_a_running_section_tag() -> None:
    chunks = chunk_report(PAGES, issue="29/2026")
    sections = {c.metadata.get("section") for c in chunks}
    assert {"Fixed Income", "Investment Updates", "Equities"} <= sections


def test_the_fund_yield_lands_in_its_section() -> None:
    chunks = chunk_report(PAGES, issue="29/2026")
    hit = next(c for c in chunks if "closed the week at a yield" in c.text)
    assert hit.metadata["section"] == "Investment Updates"
    assert hit.metadata["page"] == 2


def test_every_chunk_records_issue_and_page() -> None:
    chunks = chunk_report(PAGES, issue="29/2026")
    assert chunks
    for c in chunks:
        assert c.metadata["issue"] == "29/2026"
        assert c.metadata["page"] in (1, 2)


def test_a_long_section_spills_into_several_chunks() -> None:
    chunks = chunk_report(PAGES, issue="29/2026")
    fixed = [c for c in chunks if c.metadata.get("section") == "Fixed Income"]
    assert len(fixed) >= 2


def test_chunks_stay_near_the_target_size() -> None:
    chunks = chunk_report(PAGES, issue="29/2026")
    # A chunk stops once it crosses the target; allow one trailing sentence.
    assert all(len(c.text) <= TARGET_CHARS + 120 for c in chunks)


def test_ordinals_are_sequential_and_chunking_is_deterministic() -> None:
    chunks = chunk_report(PAGES, issue="29/2026")
    assert [c.ordinal for c in chunks] == list(range(len(chunks)))
    assert chunk_report(PAGES, issue="29/2026") == chunks


def test_money_market_fund_label_maps_to_money_markets() -> None:
    pages = ["Money Market Fund: the fund returned steadily through the quarter."]
    chunks = chunk_report(pages, issue="30/2026")
    assert chunks[0].metadata["section"] == "Money Markets"


def test_empty_input_yields_no_chunks() -> None:
    assert chunk_report([], issue="29/2026") == []
    assert chunk_report(["   \n  \n"], issue="29/2026") == []
