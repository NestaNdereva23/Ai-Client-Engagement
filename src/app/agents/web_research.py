from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from urllib.parse import urlparse

import structlog
from exa_py import Exa

from app.config import Settings

logger = structlog.get_logger(__name__)

_SNIPPET_LIMIT = 500


class WebSearchUnavailable(Exception):
    pass


@dataclass(frozen=True)
class WebResult:
    title: str | None
    url: str
    snippet: str


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def _domain_allowed(url: str, allowed: list[str]) -> bool:
    host = _host(url)
    return any(host == domain or host.endswith(f".{domain}") for domain in allowed)


def search_verified_web(
    settings: Settings, query: str, k: int, *, domains: Sequence[str] | None = None
) -> list[WebResult]:
    # include_domains narrows what Exa crawls; checked again below in case it misses.
    allowed = list(domains) if domains is not None else settings.web_research_allowed_domains_list
    if not settings.exa_api_key or not allowed:
        raise WebSearchUnavailable("web search has no verified sources configured")

    try:
        response = Exa(api_key=settings.exa_api_key).search(
            query,
            type="auto",
            num_results=k,
            include_domains=allowed,
            contents={"highlights": True},
        )
    except Exception as exc:
        logger.warning("search_verified_web.failed", error=str(exc))
        raise WebSearchUnavailable("the web search could not be completed") from exc

    results = []
    for item in response.results:
        url = item.url or ""
        if not _domain_allowed(url, allowed):
            continue
        highlights = getattr(item, "highlights", None) or []
        snippet = " ".join(highlights)[:_SNIPPET_LIMIT]
        results.append(WebResult(title=item.title, url=url, snippet=snippet))
    return results
