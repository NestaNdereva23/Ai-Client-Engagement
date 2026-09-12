from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.email_agent import build_system_prompt
from app.agents.graph import (
    ClientContext,
    GenerationState,
    build_generation_graph,
    load_client_facts,
    new_generation_state,
)
from app.agents.guardrails import DEFAULT_GUARDRAIL_CHECKS
from app.agents.prompt_config import resolve_pinned_configuration
from app.config import Settings, get_settings
from app.db.models.llmops import GenerationRun
from app.db.models.rules import MessageAngleCatalog, TierContract
from app.db.models.views import llm_client_context
from app.llmops.versions import persist_generation_run
from app.privacy.fact_block import ModelFactBlock
from app.privacy.llm_client import LLMClient, get_llm_client
from app.rag.retrieve import retrieve_product_facts

RUN_KIND_TEST = "test"


class PromptTestingError(ValueError):
    pass


def _angle_row(
    session: Session, angle: str | None, angle_version: int | None
) -> MessageAngleCatalog | None:
    if angle is None or angle_version is None:
        return None
    return session.scalar(
        select(MessageAngleCatalog).where(
            MessageAngleCatalog.angle == angle, MessageAngleCatalog.version == angle_version
        )
    )


def _tier_row(session: Session, tier: str | None, tier_version: int | None) -> TierContract | None:
    if tier is None or tier_version is None:
        return None
    return session.scalar(
        select(TierContract).where(TierContract.tier == tier, TierContract.version == tier_version)
    )


def _facts_from_client(session: Session, client_id: int) -> dict[str, Any] | None:
    row = (
        session.execute(
            select(llm_client_context).where(llm_client_context.c.client_id == client_id)
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise PromptTestingError(f"no llm_client_context row for client {client_id!r}")
    return load_client_facts(session, client_id, dict(row))


def _facts_from_profile(fact_profile: dict[str, Any]) -> dict[str, Any]:
    return ModelFactBlock(**fact_profile).to_dict()


@dataclass(frozen=True)
class _TestBrief:
    headline: str
    who: str
    claim: str
    ask: str
    never: str
    use: str | None
    cta: str | None


def _brief_with_cta_override(
    brief: MessageAngleCatalog | None, cta_override: str | None
) -> MessageAngleCatalog | _TestBrief | None:
    if brief is None or cta_override is None:
        return brief
    return _TestBrief(
        headline=brief.headline,
        who=brief.who,
        claim=brief.claim,
        ask=brief.ask,
        never=brief.never,
        use=brief.use,
        cta=cta_override,
    )


def generate_test_draft(
    session: Session,
    *,
    angle: str | None,
    tier: str | None,
    angle_version: int | None,
    tier_version: int | None,
    voice_version: int | None,
    safety_version: int | None,
    output_version: int | None,
    personalization_version: int | None,
    client_id: int | None = None,
    fact_profile: dict[str, Any] | None = None,
    product: str = "money market",
    n: int = 1,
    settings: Settings | None = None,
    llm_client: LLMClient | None = None,
    use_rag: bool = True,
    cta_override: str | None = None,
) -> list[GenerationRun]:
    if (client_id is None) == (fact_profile is None):
        raise PromptTestingError("give exactly one of client_id or fact_profile")

    settings = settings or get_settings()
    facts = (
        _facts_from_client(session, client_id)
        if client_id is not None
        else _facts_from_profile(fact_profile)
    )
    brief = _brief_with_cta_override(_angle_row(session, angle, angle_version), cta_override)
    contract = _tier_row(session, tier, tier_version)
    chunks = (
        retrieve_product_facts(session, product=product, angle=angle) if angle and use_rag else ()
    )

    context = ClientContext(
        raw_context={},
        angle=angle or "",
        prompt_variant=angle or "",
        chunks=chunks,
        brief=brief,
        contract=contract,
        facts=facts,
        priority_tier=tier,
        rule_version=None,
        angle_catalog_version=angle_version,
        tier_contract_version=tier_version,
    )

    config = resolve_pinned_configuration(
        session,
        voice_version=voice_version,
        safety_version=safety_version,
        output_version=output_version,
        personalization_version=personalization_version,
        tier_contract_version=tier_version,
        angle=angle,
    )

    graph = build_generation_graph(
        context_loader=lambda _client_id, _product: context,
        llm_client=llm_client or get_llm_client(settings),
        guardrail_checks=DEFAULT_GUARDRAIL_CHECKS,
        prompt_builder=build_system_prompt,
        config_resolver=lambda **_kwargs: config,
    )

    runs: list[GenerationRun] = []
    for _ in range(n):
        state: GenerationState = new_generation_state(client_id=client_id, product=product)
        final = graph.invoke(state)
        run = persist_generation_run(session, final, settings, run_kind=RUN_KIND_TEST)
        session.flush()
        runs.append(run)
    return runs
