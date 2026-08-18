"""Load a weekly report PDF into normalized per-page text.

The reports are long prose research notes, not rate sheets, and the PDF text
comes out with replacement characters where smart quotes were and with page
numbers on their own lines. This cleans that up and pulls the issue tag
(for example 29/2026) so the chunker and version history have a stable handle.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from pypdf import PdfReader

_ISSUE = re.compile(r"Cytonn\s+Weekly\s*#?\s*(\d{1,2})\s*[/.]\s*(\d{4})", re.I)
_PAGE_NUMBER_LINE = re.compile(r"^\s*\d{1,3}\s*$")
# Smart punctuation folded to ASCII so the text is uniform for scanning and prompts.
_PUNCT = {
    0x2018: "'",
    0x2019: "'",
    0x201C: '"',
    0x201D: '"',
    0x2013: "-",
    0x2014: "-",
    0x2026: "...",
    0xFFFD: "'",  # replacement char, usually a lost apostrophe
}


@dataclass(frozen=True)
class ReportDoc:
    title: str
    issue: str | None  # "29/2026"
    published_on: date | None
    pages: list[str]


def normalize(text: str) -> str:
    """Fold smart punctuation to ASCII and tidy whitespace, keeping line breaks."""
    text = text.replace("\r\n", "\n").replace("\r", "\n").translate(_PUNCT)
    lines = [ln for ln in text.split("\n") if not _PAGE_NUMBER_LINE.match(ln)]
    text = "\n".join(lines)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _clean_title(first_page: str) -> str:
    """The report name from the first line, trimmed before the Cytonn Weekly tag."""
    line = first_page.split("\n", 1)[0]
    line = re.split(r",?\s*&?\s*Cytonn\s+Weekly", line, maxsplit=1, flags=re.I)[0]
    return line.strip(" ,&-")


def extract_issue(text: str) -> str | None:
    m = _ISSUE.search(text)
    return f"{int(m.group(1))}/{m.group(2)}" if m else None


def issue_to_date(issue: str | None) -> date | None:
    """Monday of the ISO week the issue names, a stable published-on stand-in."""
    if not issue:
        return None
    try:
        week, year = issue.split("/")
        return date.fromisocalendar(int(year), int(week), 1)
    except (ValueError, TypeError):
        return None


def load_pdf(path: str) -> ReportDoc:
    """Read a report PDF into normalized pages plus its issue and date."""
    reader = PdfReader(path)
    pages = [normalize(page.extract_text() or "") for page in reader.pages]
    first = pages[0] if pages else ""
    title = _clean_title(first)
    issue = extract_issue("\n".join(pages[:2]))
    return ReportDoc(title=title, issue=issue, published_on=issue_to_date(issue), pages=pages)
