"""Chunk a normalized weekly report into retrieval-sized pieces."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# Recurring section labels; a paragraph that opens with one sets the section.
SECTION_LABELS = (
    "Executive Summary",
    "Fixed Income",
    "Money Market Fund",
    "Money Markets",
    "Equities",
    "Real Estate",
    "Eurobonds",
    "Exchange Rate",
    "Inflation",
    "Weekly Highlights",
    "Investment Updates",
    "Recommendation",
)
_LABEL_RE = re.compile(
    r"^\s*(" + "|".join(re.escape(s) for s in SECTION_LABELS) + r")\b\s*:?", re.I
)
_SENTENCE_SPLIT = re.compile(r"(?<=[.;:])\s+")

TARGET_CHARS = 1100  # roughly a retrieval sized passage
MIN_CHARS = 200


@dataclass(frozen=True)
class ReportChunk:
    ordinal: int
    text: str
    metadata: dict[str, Any] = field(default_factory=dict)


def _section_of(sentence: str) -> str | None:
    m = _LABEL_RE.match(sentence)
    if not m:
        return None
    label = m.group(1).title()
    return "Money Markets" if label == "Money Market Fund" else label


def _sentences(page_text: str) -> list[str]:
    out: list[str] = []
    for para in page_text.split("\n"):
        para = para.strip()
        if para:
            out.extend(s.strip() for s in _SENTENCE_SPLIT.split(para) if s.strip())
    return out


def chunk_report(pages: list[str], *, issue: str | None = None) -> list[ReportChunk]:
    """Pack a report's pages into section-tagged chunks of about TARGET_CHARS.

    Sentences are kept whole; a running section label follows the report's inline
    topic headers. Each chunk records its section, issue, and starting page. The
    document title lives on the document, not on every chunk.
    """
    chunks: list[ReportChunk] = []
    section: str | None = None
    buf: list[str] = []
    buf_page = 1
    ordinal = 0

    def flush() -> None:
        nonlocal ordinal, buf
        text = " ".join(buf).strip()
        if not text:
            return
        meta: dict[str, Any] = {"page": buf_page}
        if section:
            meta["section"] = section
        if issue:
            meta["issue"] = issue
        chunks.append(ReportChunk(ordinal=ordinal, text=text, metadata=meta))
        ordinal += 1
        buf = []

    for page_no, page_text in enumerate(pages, start=1):
        for sentence in _sentences(page_text):
            found = _section_of(sentence)
            # A new section starts a new chunk, so a chunk stays within one section.
            if found and found != section and buf:
                flush()
            if found:
                section = found
            if not buf:
                buf_page = page_no
            buf.append(sentence)
            if sum(len(s) + 1 for s in buf) >= TARGET_CHARS:
                carry = buf[-1] if len(buf[-1]) < MIN_CHARS else ""
                flush()
                if carry:
                    buf = [carry]
                    buf_page = page_no
    flush()
    return chunks
