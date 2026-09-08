"""Read only tools the agent can call: one test per tool, plus an empty
result and a refusal case for each, wherever the tool has one.

The watch list itself already has its own tests, so most tools here get
their groups from a monkeypatched build_watchlist rather than a seeded
active book. describe_group and get_client_facts read the risk tables
directly, so those two seed real rows.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy import delete

from app.agents import tools as tools_module
from app.agents.tools import (
    check_allowance,
    describe_group,
    estimate_cost,
    get_client_facts,
    get_contact_history,
    list_angles,
    list_groups,
    search_knowledge,
)
from app.agents.watchlist import (
    FEES_WILL_EMPTY,
    GroupMember,
    WatchGroup,
    WatchlistConfigMissing,
    WatchlistThresholds,
)
from app.campaigns.generation_cost import DEFAULT_MODEL, active_generation_cost_config
from app.db.models.active_clients import ActiveClientFund
from app.db.models.agent_proposal import AgentProposal, AgentProposalClient
from app.db.models.models import Funds
from app.db.models.risk import ClientRiskFeatures
from app.db.session import SessionLocal
from app.rag.retrieve import Retrieved

FUND_ID = 9615
SECOND_FUND_ID = 9616
CLIENT_A = 961501
CLIENT_B = 961502
CLIENT_C = 961503

AS_OF = date(2026, 9, 7)

THRESHOLDS = WatchlistThresholds(
    new_client_days=30, months_until_empty=6.0, small_balance=100.0, awaiting_call_days=2
)


def _member(client_id: int, unit_fund_id: int = FUND_ID, balance: float = 100_000.0) -> GroupMember:
    return GroupMember(client_id=client_id, unit_fund_id=unit_fund_id, balance=balance)


def _stub_watchlist(monkeypatch, members: tuple[GroupMember, ...]) -> None:
    group = WatchGroup(name=FEES_WILL_EMPTY, definition={}, members=members)
    monkeypatch.setattr(tools_module, "load_thresholds", lambda session, as_of: THRESHOLDS)
    monkeypatch.setattr(
        tools_module, "build_watchlist", lambda session, as_of, thresholds: (group,)
    )


# --- list_groups ---


def test_list_groups_reports_counts_and_money(db: None, monkeypatch) -> None:
    _stub_watchlist(
        monkeypatch, (_member(CLIENT_A, balance=300_000.0), _member(CLIENT_B, balance=200_000.0))
    )
    with SessionLocal() as session:
        result = list_groups(session, as_of=AS_OF.isoformat())
    assert result["as_of"] == AS_OF.isoformat()
    [group] = result["groups"]
    assert group["group_name"] == FEES_WILL_EMPTY
    assert group["client_count"] == 2
    assert group["fund_count"] == 2
    assert group["money_total_kes"] == 500_000.0


def test_list_groups_still_returns_a_group_with_nobody_in_it(db: None, monkeypatch) -> None:
    _stub_watchlist(monkeypatch, ())
    with SessionLocal() as session:
        result = list_groups(session, as_of=AS_OF.isoformat())
    [group] = result["groups"]
    assert group["client_count"] == 0
    assert group["money_total_kes"] == 0


def test_list_groups_refuses_without_a_risk_config(db: None, monkeypatch) -> None:
    def _raise(session, as_of):
        raise WatchlistConfigMissing("no config")

    monkeypatch.setattr(tools_module, "load_thresholds", _raise)
    with SessionLocal() as session:
        result = list_groups(session, as_of=AS_OF.isoformat())
    assert result["error"] == "no_risk_config"


# --- describe_group ---


@pytest.fixture
def risk_book(db: None):
    with SessionLocal() as session:
        session.execute(
            delete(ClientRiskFeatures).where(
                ClientRiskFeatures.client_id.in_((CLIENT_A, CLIENT_B, CLIENT_C))
            )
        )
        session.execute(delete(Funds).where(Funds.unit_fund_id.in_((FUND_ID, SECOND_FUND_ID))))
        session.commit()

        session.add_all(
            [
                Funds(unit_fund_id=FUND_ID, unit_fund_name="Cytonn Money Market Fund"),
                Funds(unit_fund_id=SECOND_FUND_ID, unit_fund_name="Cytonn High Yield Fund"),
            ]
        )
        session.add_all(
            [
                ClientRiskFeatures(
                    client_id=CLIENT_A,
                    unit_fund_id=FUND_ID,
                    sig_heavy_withdrawal=False,
                    sig_dormant=False,
                    sig_broken_pattern=False,
                    sig_shrinking=False,
                    sig_going_dormant=False,
                    sig_never_repeated=False,
                    risk_score=20,
                    risk_band="Watch",
                    risk_reasons="no signal",
                    fund_at_risk=0.0,
                    config_version=1,
                    value_tier="Medium",
                ),
                ClientRiskFeatures(
                    client_id=CLIENT_B,
                    unit_fund_id=SECOND_FUND_ID,
                    sig_heavy_withdrawal=False,
                    sig_dormant=False,
                    sig_broken_pattern=False,
                    sig_shrinking=False,
                    sig_going_dormant=False,
                    sig_never_repeated=False,
                    risk_score=70,
                    risk_band="High",
                    risk_reasons="no signal",
                    fund_at_risk=0.0,
                    config_version=1,
                    value_tier="Top",
                ),
            ]
        )
        session.commit()

    yield

    with SessionLocal() as session:
        session.execute(
            delete(ClientRiskFeatures).where(
                ClientRiskFeatures.client_id.in_((CLIENT_A, CLIENT_B, CLIENT_C))
            )
        )
        session.execute(delete(Funds).where(Funds.unit_fund_id.in_((FUND_ID, SECOND_FUND_ID))))
        session.commit()


def test_describe_group_reports_bands_and_fund_spread(risk_book: None, monkeypatch) -> None:
    _stub_watchlist(
        monkeypatch,
        (
            _member(CLIENT_A, FUND_ID),
            _member(CLIENT_B, SECOND_FUND_ID),
            _member(CLIENT_C, FUND_ID + 100),
        ),
    )
    with SessionLocal() as session:
        result = describe_group(session, group_name=FEES_WILL_EMPTY, as_of=AS_OF.isoformat())

    assert result["client_count"] == 3
    assert result["risk_bands"] == {"Watch": 1, "High": 1}
    assert result["value_tiers"] == {"Medium": 1, "Top": 1}
    assert result["fund_spread"] == {"money_market": 1, "high_yield": 1, "other": 1}
    assert result["no_risk_data"] == 1


def test_describe_group_is_empty_when_nobody_is_in_it(db: None, monkeypatch) -> None:
    _stub_watchlist(monkeypatch, ())
    with SessionLocal() as session:
        result = describe_group(session, group_name=FEES_WILL_EMPTY, as_of=AS_OF.isoformat())
    assert result["client_count"] == 0
    assert result["risk_bands"] == {}
    assert result["fund_spread"] == {}


def test_describe_group_refuses_an_unknown_group(db: None) -> None:
    with SessionLocal() as session:
        result = describe_group(session, group_name="not_a_real_group", as_of=AS_OF.isoformat())
    assert result["error"] == "unknown_group"


# --- get_client_facts ---


@pytest.fixture
def client_book(db: None):
    with SessionLocal() as session:
        session.execute(
            delete(ActiveClientFund).where(ActiveClientFund.client_id.in_((CLIENT_A, CLIENT_B)))
        )
        session.execute(
            delete(ClientRiskFeatures).where(ClientRiskFeatures.client_id.in_((CLIENT_A, CLIENT_B)))
        )
        session.commit()

        session.add(
            ClientRiskFeatures(
                client_id=CLIENT_A,
                unit_fund_id=FUND_ID,
                sig_heavy_withdrawal=False,
                sig_dormant=True,
                sig_broken_pattern=False,
                sig_shrinking=False,
                sig_going_dormant=False,
                sig_never_repeated=False,
                risk_score=60,
                risk_band="High",
                risk_reasons="no deposit in a year",
                fund_at_risk=500_000.0,
                config_version=1,
                balance_tier="Institutional",
                value_tier="Top",
                recency_band="1-2y",
            )
        )
        session.add(
            ActiveClientFund(
                client_id=CLIENT_A,
                unit_fund_id=FUND_ID,
                balance=1_000_000.0,
                n_deposits=3,
                n_withdrawals=0,
                last_deposit_date=AS_OF - timedelta(days=400),
                deposit_count_capped=False,
                withdrawal_history_hidden=False,
            )
        )
        session.add(
            ClientRiskFeatures(
                client_id=CLIENT_B,
                unit_fund_id=FUND_ID,
                sig_heavy_withdrawal=False,
                sig_dormant=False,
                sig_broken_pattern=False,
                sig_shrinking=False,
                sig_going_dormant=False,
                sig_never_repeated=False,
                risk_score=5,
                risk_band="None",
                risk_reasons="no signal",
                fund_at_risk=0.0,
                config_version=1,
            )
        )
        session.add(
            ActiveClientFund(
                client_id=CLIENT_B,
                unit_fund_id=FUND_ID,
                balance=10_000.0,
                n_deposits=1,
                n_withdrawals=0,
                deposit_count_capped=False,
                withdrawal_history_hidden=False,
            )
        )
        session.commit()

    yield

    with SessionLocal() as session:
        session.execute(
            delete(ActiveClientFund).where(ActiveClientFund.client_id.in_((CLIENT_A, CLIENT_B)))
        )
        session.execute(
            delete(ClientRiskFeatures).where(ClientRiskFeatures.client_id.in_((CLIENT_A, CLIENT_B)))
        )
        session.commit()


def test_get_client_facts_returns_the_allowed_bands_and_signals(client_book: None) -> None:
    with SessionLocal() as session:
        result = get_client_facts(
            session, client_id=CLIENT_A, unit_fund_id=FUND_ID, as_of=AS_OF.isoformat()
        )
    facts = result["facts"]
    assert facts["risk_band"] == "High"
    assert facts["balance_tier"] == "Institutional"
    assert facts["value_tier"] == "Top"
    assert facts["sig_dormant"] is True
    assert "client_id" not in facts
    assert "sig_heavy_withdrawal" not in facts


def test_get_client_facts_is_sparse_when_nothing_fired(client_book: None) -> None:
    with SessionLocal() as session:
        result = get_client_facts(
            session, client_id=CLIENT_B, unit_fund_id=FUND_ID, as_of=AS_OF.isoformat()
        )
    facts = result["facts"]
    assert facts["risk_band"] == "None"
    assert not any(key.startswith("sig_") for key in facts)


def test_get_client_facts_refuses_a_client_with_no_risk_data(db: None) -> None:
    with SessionLocal() as session:
        result = get_client_facts(
            session, client_id=999999999, unit_fund_id=999999999, as_of=AS_OF.isoformat()
        )
    assert result["error"] == "not_found"


# --- get_contact_history ---


@pytest.fixture
def contact_history_proposal(db: None):
    with SessionLocal() as session:
        proposal = AgentProposal(
            action_code="fee_warning",
            catalog_version=1,
            group_name=FEES_WILL_EMPTY,
            client_count=1,
            money_total_kes=250_000.0,
            evidence="one client fits the fee warning group",
            reason="tell them the fee will empty the account",
            permission_applied="suggest_only",
            status="proposed",
        )
        session.add(proposal)
        session.flush()
        session.add(
            AgentProposalClient(
                proposal_id=proposal.proposal_id,
                client_id=CLIENT_A,
                unit_fund_id=FUND_ID,
                included=True,
            )
        )
        session.commit()
        proposal_id = proposal.proposal_id

    yield proposal_id

    with SessionLocal() as session:
        session.execute(
            delete(AgentProposalClient).where(AgentProposalClient.proposal_id == proposal_id)
        )
        session.execute(delete(AgentProposal).where(AgentProposal.proposal_id == proposal_id))
        session.commit()


def test_get_contact_history_reports_the_most_recent_proposal(
    contact_history_proposal: int,
) -> None:
    with SessionLocal() as session:
        result = get_contact_history(session, group_name=FEES_WILL_EMPTY)
    assert result["ever_proposed"] is True
    assert result["action_code"] == "fee_warning"
    assert result["status"] == "proposed"
    assert result["included_count"] == 1


def test_get_contact_history_is_empty_when_the_group_was_never_proposed(db: None) -> None:
    with SessionLocal() as session:
        result = get_contact_history(session, group_name="waiting_on_a_call")
    assert result == {"group_name": "waiting_on_a_call", "ever_proposed": False}


def test_get_contact_history_refuses_an_unknown_group(db: None) -> None:
    with SessionLocal() as session:
        result = get_contact_history(session, group_name="not_a_real_group")
    assert result["error"] == "unknown_group"


# --- search_knowledge ---


def test_search_knowledge_returns_the_ranked_passages(db: None, monkeypatch) -> None:
    hits = [
        Retrieved(
            chunk_id=1,
            text="Money market funds hold short term instruments.",
            metadata={"section": "Money Markets"},
            score=0.912345,
            version_id=1,
        ),
        Retrieved(
            chunk_id=2,
            text="Yields moved slightly this month.",
            metadata={"section": "Investment Updates"},
            score=0.5,
            version_id=1,
        ),
    ]
    monkeypatch.setattr(tools_module, "search_rag_corpus", lambda session, **kwargs: hits)

    with SessionLocal() as session:
        result = search_knowledge(session, query="money market fund")

    assert result["query"] == "money market fund"
    assert result["results"][0]["text"] == hits[0].text
    assert result["results"][0]["section"] == "Money Markets"
    assert result["results"][0]["score"] == 0.912


def test_search_knowledge_is_empty_when_nothing_matches(db: None, monkeypatch) -> None:
    monkeypatch.setattr(tools_module, "search_rag_corpus", lambda session, **kwargs: [])
    with SessionLocal() as session:
        result = search_knowledge(session, query="a phrase nothing matches")
    assert result["results"] == []


def test_search_knowledge_refuses_an_empty_query(db: None) -> None:
    with SessionLocal() as session:
        result = search_knowledge(session, query="   ")
    assert result["error"] == "empty_query"


# --- list_angles ---


def test_list_angles_lists_what_is_live_today(db: None) -> None:
    with SessionLocal() as session:
        result = list_angles(session, as_of=AS_OF.isoformat())
    by_angle = {row["angle"]: row for row in result["angles"]}
    assert "not_a_goodbye" in by_angle
    entry = by_angle["not_a_goodbye"]
    assert entry["headline"]
    assert entry["claim"]
    assert entry["never"]
    assert isinstance(entry["held"], bool)


def test_list_angles_is_empty_before_any_catalogue_existed(db: None) -> None:
    with SessionLocal() as session:
        result = list_angles(session, as_of="2000-01-01")
    assert result["angles"] == []


def test_list_angles_refuses_a_malformed_date(db: None) -> None:
    with SessionLocal() as session:
        with pytest.raises(ValueError):
            list_angles(session, as_of="not-a-date")


# --- estimate_cost ---


def test_estimate_cost_prices_the_whole_group(db: None, monkeypatch) -> None:
    _stub_watchlist(monkeypatch, (_member(CLIENT_A), _member(CLIENT_B), _member(CLIENT_C)))
    with SessionLocal() as session:
        config = active_generation_cost_config(session, DEFAULT_MODEL, AS_OF)
        result = estimate_cost(session, group_name=FEES_WILL_EMPTY, as_of=AS_OF.isoformat())

    assert result["client_count"] == 3
    assert result["model"] == DEFAULT_MODEL
    assert result["total_cost_kes"] == pytest.approx(3 * config.cost_per_generation_kes)


def test_estimate_cost_is_zero_for_an_empty_group(db: None, monkeypatch) -> None:
    _stub_watchlist(monkeypatch, ())
    with SessionLocal() as session:
        result = estimate_cost(session, group_name=FEES_WILL_EMPTY, as_of=AS_OF.isoformat())
    assert result["client_count"] == 0
    assert result["total_cost_kes"] == 0


def test_estimate_cost_refuses_an_unknown_group(db: None) -> None:
    with SessionLocal() as session:
        result = estimate_cost(session, group_name="not_a_real_group", as_of=AS_OF.isoformat())
    assert result["error"] == "unknown_group"


def test_estimate_cost_refuses_an_unpriced_model(db: None, monkeypatch) -> None:
    _stub_watchlist(monkeypatch, (_member(CLIENT_A),))
    with SessionLocal() as session:
        result = estimate_cost(
            session, group_name=FEES_WILL_EMPTY, model="claude-mythos-5", as_of=AS_OF.isoformat()
        )
    assert result["error"] == "unknown_model"


# --- check_allowance ---


class _FakePermission:
    def __init__(self, max_clients_per_day, max_money_kes):
        self.max_clients_per_day = max_clients_per_day
        self.max_money_kes = max_money_kes


def test_check_allowance_reports_what_is_left_under_a_cap(db: None, monkeypatch) -> None:
    monkeypatch.setattr(tools_module, "load_action", lambda session, code, at: object())
    monkeypatch.setattr(
        tools_module, "resolve_permission", lambda session, code: _FakePermission(5, 100_000.0)
    )
    monkeypatch.setattr(tools_module, "effective_permission", lambda session, code: "approve_each")

    from app.services.agent_proposals import DailyUsage

    monkeypatch.setattr(
        tools_module,
        "daily_usage",
        lambda session, *, action_code, as_of: DailyUsage(used_clients=2, used_money_kes=40_000.0),
    )

    with SessionLocal() as session:
        result = check_allowance(session, action_code="fee_warning", as_of=AS_OF.isoformat())

    assert result["permission"] == "approve_each"
    assert result["used_clients"] == 2
    assert result["remaining_clients"] == 3
    assert result["remaining_money_kes"] == 60_000.0


def test_check_allowance_treats_no_cap_as_unlimited(db: None, monkeypatch) -> None:
    monkeypatch.setattr(tools_module, "load_action", lambda session, code, at: object())
    monkeypatch.setattr(tools_module, "resolve_permission", lambda session, code: None)
    monkeypatch.setattr(tools_module, "effective_permission", lambda session, code: "suggest_only")

    from app.services.agent_proposals import DailyUsage

    monkeypatch.setattr(
        tools_module,
        "daily_usage",
        lambda session, *, action_code, as_of: DailyUsage(used_clients=0, used_money_kes=0.0),
    )

    with SessionLocal() as session:
        result = check_allowance(session, action_code="fee_warning", as_of=AS_OF.isoformat())

    assert result["remaining_clients"] is None
    assert result["remaining_money_kes"] is None


def test_check_allowance_refuses_an_unknown_action(db: None, monkeypatch) -> None:
    monkeypatch.setattr(tools_module, "load_action", lambda session, code, at: None)
    with SessionLocal() as session:
        result = check_allowance(session, action_code="not_a_real_action", as_of=AS_OF.isoformat())
    assert result["error"] == "unknown_action"


def test_check_allowance_without_an_action_sums_every_action(db: None, monkeypatch) -> None:
    monkeypatch.setattr(
        tools_module,
        "load_active_actions",
        lambda session, at: {"fee_warning": object(), "start_win_back": object()},
    )
    monkeypatch.setattr(
        tools_module,
        "_action_allowance",
        lambda session, code, day: {
            "action_code": code,
            "permission": "suggest_only",
            "cap_clients_per_day": None,
            "used_clients": 4,
            "remaining_clients": None,
            "cap_money_kes": None,
            "used_money_kes": 1_000.0,
            "remaining_money_kes": None,
        },
    )

    with SessionLocal() as session:
        result = check_allowance(session, as_of=AS_OF.isoformat())

    assert result["overall"]["used_clients"] == 8
    assert result["overall"]["used_money_kes"] == 2_000.0
    assert result["overall"]["remaining_clients"] is None
    assert set(result["by_action"]) == {"fee_warning", "start_win_back"}
