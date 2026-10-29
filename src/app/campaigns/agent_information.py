from __future__ import annotations

import json
import re
from collections.abc import Sequence
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import distinct, func, select
from sqlalchemy.orm import Session

from app.agents.campaign_prompt import CAMPAIGN_REFINE, build_refine_prompt
from app.agents.web_research import WebResult, WebSearchUnavailable, search_verified_web
from app.campaigns.agent_prompts import campaign_prompt_text
from app.config import Settings, get_settings
from app.db.models.rag import DOC_TYPE_REPORT, RagChunk, RagDocument, RagDocumentVersion
from app.privacy.llm_client import (
    CampaignAgentNotConfigured,
    LLMClientError,
    get_campaign_agent_llm_client,
)
from app.privacy.scanners import OutboundLeak, scan_outbound
from app.rag.retrieve import retrieve_product_facts

MAX_WEB_RESULTS = 5
MAX_FINDINGS = 5
MAX_REPORT_PASSAGES = 4
PASSAGE_LIMIT = 700
DOMAIN_PATTERN = re.compile(r"^(?=.{4,253}$)([a-z0-9]([a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}$")
NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")

TOOL_SWITCHES = {
    "get_weekly_report_facts": "campaign_agent_use_weekly_report",
    "search_approved_sites": "campaign_agent_use_web_research",
    "get_campaign_results": "campaign_agent_use_campaign_results",
}


def tool_is_on(tool_name: str, settings: Settings) -> bool:
    switch = TOOL_SWITCHES.get(tool_name)
    return switch is None or bool(getattr(settings, switch))


def allowed_tool_names(names: Sequence[str], settings: Settings) -> tuple[str, ...]:
    return tuple(name for name in names if tool_is_on(name, settings))


def switched_off_tool_names(names: Sequence[str], settings: Settings) -> tuple[str, ...]:
    return tuple(name for name in names if not tool_is_on(name, settings))


def _website_name(raw: str) -> str:
    name = re.sub(r"^[a-z]+://", "", raw.strip().lower()).split("/")[0]
    return name.removeprefix("www.")


def clean_domain_list(value: str) -> str | None:
    domains: list[str] = []
    for raw in value.split(","):
        domain = _website_name(raw)
        if not domain:
            continue
        if not DOMAIN_PATTERN.match(domain):
            raise ValueError(f"'{raw.strip()}' is not a website name like example.com")
        if domain not in domains:
            domains.append(domain)
    return ",".join(domains) or None


def get_weekly_report_facts(session: Session, *, product: str) -> dict[str, Any]:
    name = product.strip().lower() if isinstance(product, str) else ""
    if not name:
        return {"error": "empty_product", "message": "name a product, for example money market"}
    hits = retrieve_product_facts(session, product=name, k=MAX_REPORT_PASSAGES)
    if not hits:
        return {
            "error": "no_weekly_report_facts",
            "message": f"the active weekly report has nothing on '{name}', or none is active",
        }
    title, published_on = session.execute(
        select(RagDocument.title, RagDocumentVersion.published_on)
        .join(RagDocument, RagDocument.doc_id == RagDocumentVersion.doc_id)
        .where(RagDocumentVersion.version_id == hits[0].version_id)
    ).one()
    return {
        "product": name,
        "report": title,
        "published_on": published_on.isoformat() if published_on else None,
        "passages": [
            {"text": hit.text[:PASSAGE_LIMIT], "section": hit.metadata.get("section")}
            for hit in hits
        ],
        "retrieved_at": datetime.now(UTC).isoformat(),
    }


def active_weekly_report(session: Session) -> dict[str, Any] | None:
    row = session.execute(
        select(
            RagDocument.title,
            RagDocumentVersion.version_id,
            RagDocumentVersion.version_no,
            RagDocumentVersion.published_on,
            RagDocumentVersion.ingested_at,
        )
        .join(RagDocument, RagDocument.doc_id == RagDocumentVersion.doc_id)
        .where(RagDocument.doc_type == DOC_TYPE_REPORT, RagDocumentVersion.is_active.is_(True))
        .order_by(RagDocumentVersion.ingested_at.desc())
        .limit(1)
    ).first()
    if row is None:
        return None
    passages = session.scalar(
        select(func.count()).select_from(RagChunk).where(RagChunk.version_id == row.version_id)
    )
    sections = session.scalars(
        select(distinct(RagChunk.chunk_metadata["section"].astext)).where(
            RagChunk.version_id == row.version_id
        )
    ).all()
    return {
        "title": row.title,
        "version_no": row.version_no,
        "published_on": row.published_on,
        "ingested_at": row.ingested_at,
        "passages": passages or 0,
        "sections": sorted(section for section in sections if section),
    }


def _numbers(text: str) -> set[str]:
    return {figure.replace(",", "") for figure in NUMBER.findall(text)}


def _parse_findings(reply: str, results: Sequence[WebResult]) -> list[dict[str, str]]:
    try:
        items = json.loads(reply[reply.index("{") : reply.rindex("}") + 1])["findings"]
    except (ValueError, KeyError, TypeError):
        raise ValueError("the model did not answer in the JSON that was asked for") from None
    findings: list[dict[str, str]] = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        number, text = item.get("source"), item.get("text")
        if not (isinstance(number, int) and 1 <= number <= len(results) and isinstance(text, str)):
            continue
        source = results[number - 1]
        figures = _numbers(text)
        if not figures or not figures <= _numbers(f"{source.title or ''} {source.snippet}"):
            continue
        findings.append({"text": " ".join(text.split()), "url": source.url})
    return findings[:MAX_FINDINGS]


def _refine(
    session: Session, settings: Settings, query: str, results: Sequence[WebResult]
) -> list[dict[str, str]]:
    client = get_campaign_agent_llm_client(settings)
    text = campaign_prompt_text(session, CAMPAIGN_REFINE, date.today())
    system = build_refine_prompt({CAMPAIGN_REFINE: text}, MAX_FINDINGS)
    numbered = "\n".join(
        f"[{number}] {result.title or ''} | {result.snippet}"
        for number, result in enumerate(results, 1)
    )
    request = f"Question: {query}\nResults:\n{numbered}"
    scan_outbound(request)
    reply = client.generate(system=system, user=request)
    scan_outbound(reply)
    return _parse_findings(reply, results)


def campaign_search_web(session: Session, *, query: str, k: int = 5) -> dict[str, Any]:
    if not query or not query.strip():
        return {"error": "empty_query", "message": "a search needs some text to look for"}

    settings = get_settings()
    try:
        results = search_verified_web(
            settings,
            query,
            max(1, min(k, MAX_WEB_RESULTS)),
            domains=settings.campaign_agent_web_domains_list,
        )
    except WebSearchUnavailable as exc:
        return {"error": "web_search_unavailable", "message": str(exc)}

    stamp = datetime.now(UTC).isoformat()
    if not results:
        return {"query": query, "findings": [], "retrieved_at": stamp}
    try:
        findings = _refine(session, settings, query, results)
    except OutboundLeak:
        return {
            "error": "web_results_withheld",
            "message": "the web results held contact details and were not used",
        }
    except (CampaignAgentNotConfigured, LLMClientError, ValueError) as exc:
        return {"error": "refine_failed", "message": str(exc)}
    return {"query": query, "findings": findings, "retrieved_at": stamp}
