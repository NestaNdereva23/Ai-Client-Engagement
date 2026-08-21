"""Who owns which client, and who covers for whom tonight.

Pure over already-gathered inputs, the same discipline as risk/routing.py:
no database access here. The nightly worker gathers the roster, the current
ownership and this run's clients, calls allocate_advisors once for the whole
book, and writes the answer to fa_assignment itself.

Four rules, in order:

1. A client keeps the advisor they already have, as long as that advisor is
   still on the roster. Ownership is a relationship, so it survives a run.
2. A client with no advisor goes to the least loaded one, measured by the
   money at risk they already carry and then by how many clients they hold.
3. A client whose advisor has left the roster is treated as unassigned and
   gets a new permanent owner by that same rule.
4. When an advisor's call queue for the night runs past their daily
   capacity, the overflow lines are lent to advisors with room. A loan is
   for that night only. It never moves ownership, so tomorrow the client is
   back with the advisor who knows them. A client nobody has room for is
   demoted off the call queue for the night instead of piling onto their
   own advisor's list -- see _lend_overflow.

Load and capacity measure two different things on purpose. Load spreads
ownership of the whole book. Capacity bounds only the call queue, which is
what an advisor can actually get through in one morning.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field

from app.config import FaRecord


@dataclass(frozen=True)
class ClientLoad:
    """One client's weight in the allocation.

    Keyed by client, not by client-fund: an advisor owns a person, so two
    advisors can never end up phoning the same client on the same morning.
    fund_at_risk is that client's total across every fund they hold.
    """

    client_id: int
    fund_at_risk: float
    in_call_queue: bool = False


@dataclass
class AdvisorAllocation:
    """owner is permanent, covering and demoted are for tonight only.

    covering holds an entry only for a lent client, mapping them to the
    advisor making the call tonight. Their owner in `owner` is unchanged.
    demoted holds the client ids whose call was over capacity with nobody
    to lend it to -- their call-queue lines move to the watchlist route for
    the night. Ownership is unchanged for them too.
    """

    owner: dict[int, str] = field(default_factory=dict)
    covering: dict[int, str] = field(default_factory=dict)
    demoted: set[int] = field(default_factory=set)

    def advisor_for(self, client_id: int) -> str | None:
        """Who is calling this client tonight: the stand-in if there is one,
        otherwise the owner.
        """
        stand_in = self.covering.get(client_id)
        return stand_in if stand_in is not None else self.owner.get(client_id)


def allocate_advisors(
    roster: Sequence[FaRecord],
    clients: Sequence[ClientLoad],
    current_owners: dict[int, str],
) -> AdvisorAllocation:
    """Assign every client an owner and lend out tonight's overflow.

    current_owners maps client_id to the advisor already on file for them,
    which for most clients is what they keep. An empty roster returns an
    empty allocation, so an environment with no roster set is untouched.
    """
    rostered = {record.fa_id: record for record in roster}
    if not rostered or not clients:
        return AdvisorAllocation()

    owner: dict[int, str] = {}
    unassigned: list[ClientLoad] = []
    for client in clients:
        existing = current_owners.get(client.client_id)
        if existing is not None and existing in rostered:
            owner[client.client_id] = existing
        else:
            unassigned.append(client)

    money = dict.fromkeys(rostered, 0.0)
    heads = dict.fromkeys(rostered, 0)
    by_id = {client.client_id: client for client in clients}
    for client_id, fa_id in owner.items():
        money[fa_id] += by_id[client_id].fund_at_risk
        heads[fa_id] += 1

    # Biggest first, so the heaviest clients are spread before the light
    # ones fill the gaps. client_id breaks ties so two runs over the same
    # data always land on the same answer.
    for client in sorted(unassigned, key=lambda c: (-c.fund_at_risk, c.client_id)):
        fa_id = min(rostered, key=lambda f: (money[f], heads[f], f))
        owner[client.client_id] = fa_id
        money[fa_id] += client.fund_at_risk
        heads[fa_id] += 1

    covering, demoted = _lend_overflow(rostered, clients, owner)
    return AdvisorAllocation(owner=owner, covering=covering, demoted=demoted)


def _lend_overflow(
    rostered: dict[str, FaRecord],
    clients: Sequence[ClientLoad],
    owner: dict[int, str],
) -> tuple[dict[int, str], set[int]]:
    """Move tonight's call-queue overflow to advisors with room.

    Each over-capacity advisor keeps their biggest clients and lends the
    smallest, since the ones worth most are where knowing the client counts.
    The lent lines are then handed out largest first to whichever advisor
    has the shortest queue. A client nobody has room for is demoted instead
    of staying on their own advisor's already-full list: a call queue past
    what an advisor can actually get through in a morning is not a real
    call list, so the caller moves that client's call-queue lines to the
    watchlist route for the night rather than let the list grow without a
    limit.
    """
    queue_by_fa: dict[str, list[ClientLoad]] = defaultdict(list)
    for client in clients:
        if client.in_call_queue:
            queue_by_fa[owner[client.client_id]].append(client)

    counts = {fa_id: len(queue_by_fa[fa_id]) for fa_id in rostered}
    overflow: list[ClientLoad] = []
    for fa_id, rows in queue_by_fa.items():
        capacity = rostered[fa_id].daily_capacity
        if len(rows) <= capacity:
            continue
        rows.sort(key=lambda c: (c.fund_at_risk, c.client_id))
        overflow.extend(rows[: len(rows) - capacity])

    covering: dict[int, str] = {}
    demoted: set[int] = set()
    for client in sorted(overflow, key=lambda c: (-c.fund_at_risk, c.client_id)):
        home = owner[client.client_id]
        with_room = [
            fa_id
            for fa_id in rostered
            if fa_id != home and counts[fa_id] < rostered[fa_id].daily_capacity
        ]
        if not with_room:
            demoted.add(client.client_id)
            continue
        target = min(with_room, key=lambda f: (counts[f], f))
        covering[client.client_id] = target
        counts[target] += 1
        counts[home] -= 1
    return covering, demoted


__all__ = ["AdvisorAllocation", "ClientLoad", "allocate_advisors"]
