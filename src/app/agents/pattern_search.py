from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from itertools import combinations
from typing import Any, NamedTuple

import structlog
from sqlalchemy import Date, cast, exists, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.agents.action_performance import detail_query
from app.agents.action_results import DEPOSIT_TXN_TYPE, SENT_STATUS
from app.agents.agent_loop import NOT_SELECTED_TONIGHT
from app.agents.propose import ACTION_PAUSED, ANGLE_PAUSED
from app.agents.write_tools import NO_ACTION_DECIDED
from app.audit.log import record_audit
from app.config import Settings, get_settings
from app.db.models.action_performance import NONE_LABEL, UNKNOWN_LABEL
from app.db.models.action_result import ActionResult
from app.db.models.active_clients import ActiveClientFund, ActiveTransaction
from app.db.models.agent_proposal import AgentProposal, AgentProposalClient
from app.db.models.campaigns import TouchLog
from app.db.models.observed_pattern import PATTERN_OUTCOMES, ObservedPattern
from app.db.models.outreach import OutreachMessage

logger = structlog.get_logger(__name__)

FEATURE_LABELS = {
    "action_code": "action",
    "angle": "angle",
    "priority_tier": "priority tier",
    "risk_band": "risk band",
    "content_mix": "guide mix",
    "variant": "version",
}
MIN_FEATURES = 2
OTHER_MESSAGES = "other_messages"
NOT_MESSAGED = "not_messaged"
BETTER = "better"
WORSE = "worse"
NOT_MESSAGED_REASONS = (NOT_SELECTED_TONIGHT, NO_ACTION_DECIDED, ANGLE_PAUSED, ACTION_PAUSED)
UNUSABLE_VALUES = (NONE_LABEL, UNKNOWN_LABEL)
REFRESHED_COLUMNS = (
    "description",
    "outcome_percent",
    "comparison_percent",
    "gap_points",
    "sent_count",
    "comparison_count",
    "last_seen_at",
)

Features = tuple[tuple[str, str], ...]


class Observation(NamedTuple):
    features: Features
    group_name: str
    replied: bool
    deposited: bool


class NotMessaged(NamedTuple):
    group_name: str
    deposited: bool


class Counts(NamedTuple):
    size: int
    hits: int


@dataclass(frozen=True)
class PatternRules:
    min_group_size: int
    min_gap_points: float
    min_z: float
    max_features: int
    max_patterns: int

    @classmethod
    def from_settings(cls, settings: Settings) -> PatternRules:
        return cls(
            min_group_size=settings.pattern_min_group_size,
            min_gap_points=settings.pattern_min_gap_points,
            min_z=settings.pattern_min_z,
            max_features=settings.pattern_max_features,
            max_patterns=settings.pattern_max_per_run,
        )


@dataclass(frozen=True)
class FoundPattern:
    features: Features
    outcome: str
    compared_with: str
    direction: str
    outcome_percent: float
    comparison_percent: float
    gap_points: float
    sent_count: int
    comparison_count: int
    z_score: float


@dataclass
class Tally:
    members: list[int] = field(default_factory=list)
    replied: int = 0
    deposited: int = 0
    groups: Counter[str] = field(default_factory=Counter)

    def add(self, index: int, observation: Observation) -> None:
        self.members.append(index)
        self.replied += int(observation.replied)
        self.deposited += int(observation.deposited)
        self.groups[observation.group_name] += 1

    def hits(self, outcome: str) -> int:
        return getattr(self, outcome)


@dataclass(frozen=True)
class PatternSearchResult:
    measured_messages: int
    not_messaged_clients: int
    found: int
    new: int


def describe(features: Features) -> str:
    return ", ".join(
        f"{FEATURE_LABELS[name]} {value.replace('_', ' ')}" for name, value in features
    )


def pattern_key(found: FoundPattern, window_days: int) -> str:
    parts = "&".join(f"{name}={value}" for name, value in found.features)
    return f"{found.direction}|{found.outcome}|{window_days}d|{parts}"


def two_proportion_z(first: Counts, second: Counts) -> float:
    pooled = (first.hits + second.hits) / (first.size + second.size)
    spread = pooled * (1 - pooled) * (1 / first.size + 1 / second.size)
    if spread == 0:
        return 0.0
    return (first.hits / first.size - second.hits / second.size) / math.sqrt(spread)


def find_patterns(
    observations: Sequence[Observation],
    not_messaged: Iterable[NotMessaged],
    rules: PatternRules,
) -> list[FoundPattern]:
    tallies = _tally_combinations(observations, rules.max_features)
    everyone = {
        outcome: Counts(
            len(observations), sum(1 for item in observations if getattr(item, outcome))
        )
        for outcome in PATTERN_OUTCOMES
    }
    untouched = _untouched_by_group(not_messaged)

    found: list[FoundPattern] = []
    described: set[tuple[tuple[int, ...], str, str]] = set()
    for features, tally in sorted(tallies.items(), key=lambda item: (len(item[0]), item[0])):
        if len(tally.members) < rules.min_group_size:
            continue
        for pattern in _judge(features, tally, everyone, untouched, rules):
            identity = (tuple(tally.members), pattern.outcome, pattern.direction)
            if identity in described:
                continue
            described.add(identity)
            found.append(pattern)
    found.sort(key=lambda pattern: -abs(pattern.z_score))
    return found[: rules.max_patterns]


def _tally_combinations(
    observations: Sequence[Observation], max_features: int
) -> dict[Features, Tally]:
    tallies: dict[Features, Tally] = defaultdict(Tally)
    for index, observation in enumerate(observations):
        for size in range(MIN_FEATURES, max_features + 1):
            for features in combinations(observation.features, size):
                tallies[features].add(index, observation)
    return tallies


def _untouched_by_group(not_messaged: Iterable[NotMessaged]) -> dict[str, Counts]:
    sizes: Counter[str] = Counter()
    hits: Counter[str] = Counter()
    for client in not_messaged:
        sizes[client.group_name] += 1
        hits[client.group_name] += int(client.deposited)
    return {group: Counts(sizes[group], hits[group]) for group in sizes}


def _judge(
    features: Features,
    tally: Tally,
    everyone: dict[str, Counts],
    untouched: dict[str, Counts],
    rules: PatternRules,
) -> list[FoundPattern]:
    size = len(tally.members)
    verdicts = []
    for outcome in PATTERN_OUTCOMES:
        group = Counts(size, tally.hits(outcome))
        others = Counts(everyone[outcome].size - size, everyone[outcome].hits - group.hits)
        verdicts.append(_compare(features, outcome, OTHER_MESSAGES, BETTER, group, others, rules))
    nothing_sent = _pooled(untouched, tally.groups)
    group = Counts(size, tally.deposited)
    verdicts.append(
        _compare(features, "deposited", NOT_MESSAGED, WORSE, group, nothing_sent, rules)
    )
    return [verdict for verdict in verdicts if verdict is not None]


def _pooled(untouched: dict[str, Counts], groups: Iterable[str]) -> Counts:
    chosen = [untouched[group] for group in groups if group in untouched]
    return Counts(sum(item.size for item in chosen), sum(item.hits for item in chosen))


def _compare(
    features: Features,
    outcome: str,
    compared_with: str,
    direction: str,
    group: Counts,
    reference: Counts,
    rules: PatternRules,
) -> FoundPattern | None:
    if reference.size < rules.min_group_size:
        return None
    sign = 1 if direction == BETTER else -1
    gap_points = (group.hits / group.size - reference.hits / reference.size) * 100
    z_score = two_proportion_z(group, reference)
    if sign * gap_points < rules.min_gap_points or sign * z_score < rules.min_z:
        return None
    return FoundPattern(
        features=features,
        outcome=outcome,
        compared_with=compared_with,
        direction=direction,
        outcome_percent=round(100 * group.hits / group.size, 1),
        comparison_percent=round(100 * reference.hits / reference.size, 1),
        gap_points=round(gap_points, 1),
        sent_count=group.size,
        comparison_count=reference.size,
        z_score=z_score,
    )


def load_observations(
    session: Session, *, window_days: int, since: datetime, until: datetime
) -> list[Observation]:
    rows = session.execute(
        detail_query(window_days, until).where(ActionResult.sent_at >= since)
    ).all()
    return [
        Observation(
            features=tuple(
                (name, getattr(row, name))
                for name in FEATURE_LABELS
                if getattr(row, name) not in UNUSABLE_VALUES
            ),
            group_name=row.group_name,
            replied=row.replied,
            deposited=row.deposited,
        )
        for row in rows
    ]


def latest_data_load(session: Session) -> datetime | None:
    return session.scalar(select(func.max(ActiveClientFund.updated_at)))


def load_not_messaged(
    session: Session,
    *,
    window_days: int,
    since: datetime,
    measurable_until: datetime,
    zone_name: str,
) -> list[NotMessaged]:
    window = timedelta(days=window_days)
    left_out_on = cast(func.timezone(zone_name, AgentProposal.created_at), Date)
    deposited = (
        exists()
        .where(
            ActiveTransaction.client_id == AgentProposalClient.client_id,
            ActiveTransaction.unit_fund_id == AgentProposalClient.unit_fund_id,
            ActiveTransaction.txn_type == DEPOSIT_TXN_TYPE,
            ActiveTransaction.txn_date >= left_out_on,
            ActiveTransaction.txn_date < left_out_on + window_days,
        )
        .correlate(AgentProposalClient, AgentProposal)
    )
    messaged_nearby = (
        exists()
        .where(
            OutreachMessage.client_id == AgentProposalClient.client_id,
            TouchLog.message_id == OutreachMessage.message_id,
            TouchLog.delivery_status == SENT_STATUS,
            TouchLog.sent_at >= AgentProposal.created_at - window,
            TouchLog.sent_at < AgentProposal.created_at + window,
        )
        .correlate(AgentProposalClient, AgentProposal)
    )
    rows = session.execute(
        select(AgentProposal.group_name, deposited.label("deposited"))
        .select_from(AgentProposalClient)
        .join(AgentProposal, AgentProposal.proposal_id == AgentProposalClient.proposal_id)
        .where(
            AgentProposalClient.included.is_(False),
            AgentProposalClient.skip_reason.in_(NOT_MESSAGED_REASONS),
            AgentProposal.created_at >= since,
            AgentProposal.created_at <= measurable_until - window,
            ~messaged_nearby,
        )
        .distinct(AgentProposalClient.client_id, AgentProposalClient.unit_fund_id)
        .order_by(
            AgentProposalClient.client_id,
            AgentProposalClient.unit_fund_id,
            AgentProposal.created_at,
        )
    ).all()
    return [NotMessaged(group_name=row.group_name, deposited=row.deposited) for row in rows]


def save_patterns(
    session: Session, found: Sequence[FoundPattern], *, window_days: int, seen_at: datetime
) -> int:
    if not found:
        return 0
    rows = [_row(pattern, window_days, seen_at) for pattern in found]
    keys = [row["pattern_key"] for row in rows]
    known = set(
        session.scalars(
            select(ObservedPattern.pattern_key).where(ObservedPattern.pattern_key.in_(keys))
        )
    )
    stmt = pg_insert(ObservedPattern).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=["pattern_key"],
        set_={column: getattr(stmt.excluded, column) for column in REFRESHED_COLUMNS},
    )
    session.execute(stmt)
    return len(set(keys) - known)


def _row(pattern: FoundPattern, window_days: int, seen_at: datetime) -> dict[str, Any]:
    return {
        "pattern_key": pattern_key(pattern, window_days),
        "description": describe(pattern.features),
        "features": dict(pattern.features),
        "outcome": pattern.outcome,
        "compared_with": pattern.compared_with,
        "direction": pattern.direction,
        "window_days": window_days,
        "outcome_percent": pattern.outcome_percent,
        "comparison_percent": pattern.comparison_percent,
        "gap_points": pattern.gap_points,
        "sent_count": pattern.sent_count,
        "comparison_count": pattern.comparison_count,
        "found_at": seen_at,
        "last_seen_at": seen_at,
    }


def run_pattern_search(
    session: Session, *, now: datetime | None = None, window_days: int | None = None
) -> PatternSearchResult:
    settings = get_settings()
    searched_at = now or datetime.now(UTC)
    window = window_days or settings.action_performance_read_window_days
    since = searched_at - timedelta(hours=settings.action_performance_lookback_hours)

    observations = load_observations(session, window_days=window, since=since, until=searched_at)
    loaded_at = latest_data_load(session)
    not_messaged = (
        []
        if loaded_at is None
        else load_not_messaged(
            session,
            window_days=window,
            since=since,
            measurable_until=min(searched_at, loaded_at),
            zone_name=settings.db_timezone,
        )
    )
    found = find_patterns(observations, not_messaged, PatternRules.from_settings(settings))
    new = save_patterns(session, found, window_days=window, seen_at=searched_at)

    result = PatternSearchResult(
        measured_messages=len(observations),
        not_messaged_clients=len(not_messaged),
        found=len(found),
        new=new,
    )
    record_audit(
        session,
        entity_type="observed_pattern",
        action="search",
        detail={"window_days": window, **asdict(result)},
    )
    session.commit()
    logger.info("pattern_search.finished", window_days=window, **asdict(result))
    return result
