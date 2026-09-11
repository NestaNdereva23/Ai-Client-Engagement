"""Read only tools the agent can call.

Each tool is a thin wrapper over a service that already exists elsewhere in
this codebase: the watch list, the proposal store, the risk facts a briefing
already reads, the message angle catalogue, the RAG search, and the
generation cost estimator. No tool here reaches the database on a path of
its own, and no tool ever returns a client's name, an exact figure tied to
one client, or an exact date. When a tool cannot answer, for example an
unknown group name or a client with no risk facts on file, it returns a
plain dict with an "error" key instead of raising, so a run can read the
refusal and carry on rather than crash.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Sequence
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.action_catalog import load_action, load_active_actions
from app.agents.permissions import effective_permission, resolve_permission
from app.agents.watchlist import (
    GROUP_NAMES,
    WatchGroup,
    WatchlistConfigMissing,
    build_watchlist,
    load_thresholds,
)
from app.briefing.narrative_prompt import narrative_facts
from app.campaigns.generation_cost import (
    DEFAULT_MODEL,
    MODEL_LABELS,
    GenerationCostConfigMissing,
    UnknownGenerationModel,
    active_generation_cost_config,
)
from app.db.models.models import Funds
from app.db.models.risk import ClientRiskFeatures
from app.privacy.llm_client import ToolSpec
from app.rules.catalog import load_active_angles
from app.services.agent_proposals import daily_usage, list_proposals
from app.services.briefing import gather_briefing_facts, to_risk_fact_block
from app.services.rag import search as search_rag_corpus
from app.transform.features import fund_type_from_name as classify_fund_type

_MAX_SEARCH_RESULTS = 10
_SEARCH_SCORE_DECIMALS = 3


def _parse_as_of(raw: str | None) -> date:
    """An optional ISO date string, or today when none is given."""
    return date.today() if raw is None else date.fromisoformat(raw)


def _unknown_group(group_name: str) -> dict[str, Any]:
    return {
        "error": "unknown_group",
        "message": f"'{group_name}' is not one of tonight's watch list groups",
    }


def _groups_for(session: Session, day: date) -> tuple[Sequence[WatchGroup], dict[str, Any] | None]:
    """Every watch list group for the day, or a refusal when no risk config
    is in force yet to cut the groups against.
    """
    try:
        thresholds = load_thresholds(session, day)
    except WatchlistConfigMissing:
        return (), {
            "error": "no_risk_config",
            "message": f"no risk config version is in force on {day.isoformat()}",
        }
    return build_watchlist(session, day, thresholds), None


def _find_group(groups: Sequence[WatchGroup], group_name: str) -> WatchGroup | None:
    return next((group for group in groups if group.name == group_name), None)


def list_groups(session: Session, *, as_of: str | None = None) -> dict[str, Any]:
    """Tonight's watch list groups, each with a client count, a fund count,
    and the money held across it. Never a client id, never a name.
    """
    day = _parse_as_of(as_of)
    groups, refusal = _groups_for(session, day)
    if refusal is not None:
        return refusal
    return {
        "as_of": day.isoformat(),
        "groups": [
            {
                "group_name": group.name,
                "client_count": group.client_count,
                "fund_count": group.fund_count,
                "money_total_kes": group.money_total,
            }
            for group in groups
        ],
    }


def describe_group(
    session: Session, *, group_name: str, as_of: str | None = None
) -> dict[str, Any]:
    """One group's composition: how many clients sit in each risk band and
    deposit size band, and how the group's funds split by type. Every number
    here is a count across the group, never one client's own figure.
    """
    day = _parse_as_of(as_of)
    if group_name not in GROUP_NAMES:
        return _unknown_group(group_name)

    groups, refusal = _groups_for(session, day)
    if refusal is not None:
        return refusal
    group = _find_group(groups, group_name)

    empty = {
        "group_name": group_name,
        "as_of": day.isoformat(),
        "client_count": 0,
        "risk_bands": {},
        "value_tiers": {},
        "no_risk_data": 0,
        "fund_spread": {},
    }
    if group is None or not group.members:
        return empty

    # A group can hold tens of thousands of members, so the risk rows are
    # fetched by the small set of funds a group actually spans, then matched
    # back to this group's exact members in Python. Filtering by a single
    # composite key for every member in one query builds a WHERE clause big
    # enough to overflow Postgres's own query parser on a large group.
    keys = {(member.client_id, member.unit_fund_id) for member in group.members}
    fund_ids = {member.unit_fund_id for member in group.members}
    risk_rows = [
        row
        for row in session.execute(
            select(
                ClientRiskFeatures.client_id,
                ClientRiskFeatures.unit_fund_id,
                ClientRiskFeatures.risk_band,
                ClientRiskFeatures.value_tier,
            ).where(ClientRiskFeatures.unit_fund_id.in_(fund_ids))
        ).all()
        if (row.client_id, row.unit_fund_id) in keys
    ]
    matched_keys = {(row.client_id, row.unit_fund_id) for row in risk_rows}

    fund_names = dict(
        session.execute(
            select(Funds.unit_fund_id, Funds.unit_fund_name).where(Funds.unit_fund_id.in_(fund_ids))
        ).all()
    )

    return {
        "group_name": group_name,
        "as_of": day.isoformat(),
        "client_count": group.client_count,
        "risk_bands": dict(Counter(row.risk_band for row in risk_rows)),
        "value_tiers": dict(Counter(row.value_tier or "Unknown" for row in risk_rows)),
        "no_risk_data": len(keys) - len(matched_keys),
        "fund_spread": dict(
            Counter(
                classify_fund_type(fund_names.get(member.unit_fund_id)) for member in group.members
            )
        ),
    }


def get_client_facts(
    session: Session, *, client_id: int, unit_fund_id: int, as_of: str | None = None
) -> dict[str, Any]:
    """One client fund's allowed facts: the same bands, tiers and warning
    signs a risk briefing is already allowed to hand to a model. No name, no
    client id, no exact balance or exact date.
    """
    day = _parse_as_of(as_of)
    facts = gather_briefing_facts(session, client_id, unit_fund_id, day)
    if facts is None:
        return {
            "error": "not_found",
            "message": "no risk facts are on file for this client fund",
        }
    return {"facts": narrative_facts(to_risk_fact_block(facts))}


def get_contact_history(session: Session, *, group_name: str) -> dict[str, Any]:
    """The most recent proposal made for this group, and what happened to
    it: which action, how many clients qualified, and its current status.
    """
    if group_name not in GROUP_NAMES:
        return _unknown_group(group_name)

    rows, _ = list_proposals(session, group_name=group_name, limit=1)
    if not rows:
        return {"group_name": group_name, "ever_proposed": False}

    proposal, included_count = rows[0]
    return {
        "group_name": group_name,
        "ever_proposed": True,
        "action_code": proposal.action_code,
        "status": proposal.status,
        "client_count": proposal.client_count,
        "included_count": included_count or 0,
        "proposed_at": proposal.created_at.isoformat(),
        "decided_at": proposal.decided_at.isoformat() if proposal.decided_at else None,
    }


def search_knowledge(
    session: Session,
    *,
    query: str,
    product: str | None = None,
    k: int = 5,
) -> dict[str, Any]:
    """Search the knowledge store for product facts, market facts and
    teaching notes. Returns the matched passages as they are indexed; the
    corpus itself carries no client data.
    """
    if not query or not query.strip():
        return {"error": "empty_query", "message": "a search needs some text to look for"}

    capped_k = max(1, min(k, _MAX_SEARCH_RESULTS))
    hits = search_rag_corpus(session, product=product, q=query, k=capped_k)
    return {
        "query": query,
        "results": [
            {
                "text": hit.text,
                "section": hit.metadata.get("section"),
                "score": round(hit.score, _SEARCH_SCORE_DECIMALS),
            }
            for hit in hits
        ],
    }


def list_angles(session: Session, *, as_of: str | None = None) -> dict[str, Any]:
    """The message angles live today, and what each one may and may not claim."""
    day = _parse_as_of(as_of)
    angles = load_active_angles(session, day)
    return {
        "as_of": day.isoformat(),
        "angles": [
            {
                "angle": row.angle,
                "headline": row.headline,
                "who": row.who,
                "claim": row.claim,
                "ask": row.ask,
                "never": row.never,
                "use": row.use,
                "held": row.held,
            }
            for row in sorted(angles.values(), key=lambda row: row.angle)
        ],
    }


def estimate_cost(
    session: Session,
    *,
    group_name: str,
    model: str = DEFAULT_MODEL,
    as_of: str | None = None,
) -> dict[str, Any]:
    """What drafting one message per client in this group would cost, at the
    given model's current rate. Prices the whole group, the same
    upper-bound, planning-only assumption the campaign cost estimator makes.
    """
    day = _parse_as_of(as_of)
    if group_name not in GROUP_NAMES:
        return _unknown_group(group_name)

    groups, refusal = _groups_for(session, day)
    if refusal is not None:
        return refusal
    group = _find_group(groups, group_name)
    client_count = 0 if group is None else group.client_count

    try:
        config = active_generation_cost_config(session, model, day)
    except UnknownGenerationModel:
        choices = sorted(MODEL_LABELS)
        return {
            "error": "unknown_model",
            "message": f"'{model}' is not a model this can price. Choose from {choices}",
        }
    except GenerationCostConfigMissing:
        return {
            "error": "no_cost_config",
            "message": f"'{model}' has no drafting rate in force on {day.isoformat()}",
        }

    return {
        "group_name": group_name,
        "as_of": day.isoformat(),
        "model": config.model,
        "client_count": client_count,
        "cost_per_client_kes": config.cost_per_generation_kes,
        "cost_per_client_usd": config.cost_per_generation_usd,
        "total_cost_kes": client_count * config.cost_per_generation_kes,
        "total_cost_usd": client_count * config.cost_per_generation_usd,
    }


def _action_allowance(session: Session, action_code: str, day: date) -> dict[str, Any]:
    permission_row = resolve_permission(session, action_code)
    usage = daily_usage(session, action_code=action_code, as_of=day)
    cap_clients = permission_row.max_clients_per_day if permission_row else None
    cap_money = permission_row.max_money_kes if permission_row else None
    return {
        "action_code": action_code,
        "permission": effective_permission(session, action_code),
        "cap_clients_per_day": cap_clients,
        "used_clients": usage.used_clients,
        "remaining_clients": None
        if cap_clients is None
        else max(cap_clients - usage.used_clients, 0),
        "cap_money_kes": cap_money,
        "used_money_kes": usage.used_money_kes,
        "remaining_money_kes": (
            None if cap_money is None else max(cap_money - usage.used_money_kes, 0.0)
        ),
    }


def check_allowance(
    session: Session, *, action_code: str | None = None, as_of: str | None = None
) -> dict[str, Any]:
    """How many clients and how much money are left in today's allowance,
    for one action, or for every action at once when none is named.

    There is no overall daily cap configured yet, only a per action one, so
    the overall total is reported with its remaining figures left as None
    rather than guessing at a ceiling that does not exist.
    """
    day = _parse_as_of(as_of)

    if action_code is not None:
        if load_action(session, action_code, day) is None:
            return {
                "error": "unknown_action",
                "message": (
                    f"'{action_code}' is not in the action catalogue in force on {day.isoformat()}"
                ),
            }
        return {"as_of": day.isoformat(), **_action_allowance(session, action_code, day)}

    actions = load_active_actions(session, day)
    by_action = {code: _action_allowance(session, code, day) for code in actions}
    return {
        "as_of": day.isoformat(),
        "overall": {
            "used_clients": sum(entry["used_clients"] for entry in by_action.values()),
            "used_money_kes": sum(entry["used_money_kes"] for entry in by_action.values()),
            "remaining_clients": None,
            "remaining_money_kes": None,
            "note": "no overall daily cap is set yet, only the per action caps above",
        },
        "by_action": by_action,
    }


TOOL_FUNCTIONS: dict[str, Callable[..., dict[str, Any]]] = {
    "list_groups": list_groups,
    "describe_group": describe_group,
    "get_client_facts": get_client_facts,
    "get_contact_history": get_contact_history,
    "search_knowledge": search_knowledge,
    "list_angles": list_angles,
    "estimate_cost": estimate_cost,
    "check_allowance": check_allowance,
}

_AS_OF_PROPERTY = {
    "type": "string",
    "description": "The day to evaluate, as YYYY-MM-DD. Defaults to today.",
}

TOOL_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="list_groups",
        description=(
            "List tonight's watch list groups, each with how many clients and how "
            "much money it holds."
        ),
        input_schema={"type": "object", "properties": {"as_of": _AS_OF_PROPERTY}},
    ),
    ToolSpec(
        name="describe_group",
        description=(
            "Describe one watch list group's risk bands, deposit size bands and "
            "fund spread, all as counts across the group, never one client's own figures."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "group_name": {
                    "type": "string",
                    "description": "One of the watch list's group names, from list_groups.",
                },
                "as_of": _AS_OF_PROPERTY,
            },
            "required": ["group_name"],
        },
    ),
    ToolSpec(
        name="get_client_facts",
        description=(
            "The allowed facts for one client fund: bands and warning signs only, "
            "never a name, an id, or an exact figure."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "client_id": {"type": "integer", "description": "The client's internal id."},
                "unit_fund_id": {"type": "integer", "description": "The fund's internal id."},
                "as_of": _AS_OF_PROPERTY,
            },
            "required": ["client_id", "unit_fund_id"],
        },
    ),
    ToolSpec(
        name="get_contact_history",
        description=(
            "When a watch list group was last proposed for contact, and what happened to it."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "group_name": {
                    "type": "string",
                    "description": "One of the watch list's group names, from list_groups.",
                }
            },
            "required": ["group_name"],
        },
    ),
    ToolSpec(
        name="search_knowledge",
        description=(
            "Search the knowledge store for product facts, market facts and teaching notes."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What to search for."},
                "product": {
                    "type": "string",
                    "description": "Narrow the search to one product, for example 'money market'.",
                },
                "k": {
                    "type": "integer",
                    "description": f"How many results to return, at most {_MAX_SEARCH_RESULTS}.",
                },
            },
            "required": ["query"],
        },
    ),
    ToolSpec(
        name="list_angles",
        description=(
            "List the message angles available today and what each one may and may not claim."
        ),
        input_schema={"type": "object", "properties": {"as_of": _AS_OF_PROPERTY}},
    ),
    ToolSpec(
        name="estimate_cost",
        description=(
            "Estimate what drafting a message for every client in one watch list group would cost."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "group_name": {
                    "type": "string",
                    "description": "One of the watch list's group names, from list_groups.",
                },
                "model": {
                    "type": "string",
                    "description": (
                        "Which drafting model to price. Defaults to the standard drafting model."
                    ),
                },
                "as_of": _AS_OF_PROPERTY,
            },
            "required": ["group_name"],
        },
    ),
    ToolSpec(
        name="check_allowance",
        description=(
            "How many clients and how much money are left in today's allowance, "
            "for one action or for every action at once."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "action_code": {
                    "type": "string",
                    "description": (
                        "One action from the action catalogue. Leave out for every action."
                    ),
                },
                "as_of": _AS_OF_PROPERTY,
            },
        },
    ),
)
