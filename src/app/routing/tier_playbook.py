from __future__ import annotations

from dataclasses import dataclass

from app.routing.tiers import (
    BELOW_CAPITAL_PRESERVATION_THRESHOLD,
    NO_TRANSACTIONS_IN_REVIEW_PERIOD,
    SEGMENT_ACTIVE_ACTIVE,
    SEGMENT_ACTIVE_DORMANT,
    TIER_FREQUENT_DEPOSITOR,
    TIER_FREQUENT_DEPOSITOR_WITHDRAWER,
    TIER_GRADUAL_DEPOSITOR,
    TIER_GRADUAL_DEPOSITOR_WITHDRAWER,
)


@dataclass(frozen=True)
class SegmentPlaybook:
    label: str
    goal: str
    definition: str


@dataclass(frozen=True)
class TierPlaybook:
    label: str
    goal: str | None
    fa_action: str | None
    success_measure: str | None


SEGMENT_PLAYBOOK = {
    SEGMENT_ACTIVE_ACTIVE: SegmentPlaybook(
        label="Active Active",
        goal="Deepen and diversify",
        definition="Invested within the last 3 months",
    ),
    SEGMENT_ACTIVE_DORMANT: SegmentPlaybook(
        label="Active Dormant",
        goal="Re-engage and raise deposit frequency",
        definition="No investment for over 3 months",
    ),
}

TIER_PLAYBOOK = {
    TIER_GRADUAL_DEPOSITOR: TierPlaybook(
        label="Gradual Depositors",
        goal="Raise deposit frequency",
        fa_action="Call to review goals; set up a regular top-up or standing order",
        success_measure="Moves to Active Active",
    ),
    TIER_GRADUAL_DEPOSITOR_WITHDRAWER: TierPlaybook(
        label="Gradual Depositors + Withdrawers",
        goal="Retain balance",
        fa_action="Review cash needs; split funds between savings and spending",
        success_measure="Withdrawals fall; balance held",
    ),
    TIER_FREQUENT_DEPOSITOR: TierPlaybook(
        label="Frequent Depositors",
        goal="Diversify",
        fa_action="Introduce CMMF USD and CHYF in a portfolio review",
        success_measure="Second product held",
    ),
    TIER_FREQUENT_DEPOSITOR_WITHDRAWER: TierPlaybook(
        label="Frequent Depositors + Withdrawers",
        goal="Turn activity into growth",
        fa_action="Encourage net purchases; show interest earned by staying invested",
        success_measure="Net inflows grow",
    ),
    NO_TRANSACTIONS_IN_REVIEW_PERIOD: TierPlaybook(
        label="Not transacting in the review period",
        goal=None,
        fa_action=None,
        success_measure=None,
    ),
    BELOW_CAPITAL_PRESERVATION_THRESHOLD: TierPlaybook(
        label="Below the capital preservation threshold",
        goal=None,
        fa_action=None,
        success_measure=None,
    ),
}


def tier_tag_fields(tier: str | None, segment: str | None) -> dict[str, str | None]:
    tier_playbook = TIER_PLAYBOOK.get(tier) if tier else None
    segment_playbook = SEGMENT_PLAYBOOK.get(segment) if segment else None
    return {
        "segment": segment,
        "segment_label": segment_playbook.label if segment_playbook else None,
        "tier": tier,
        "tier_label": tier_playbook.label if tier_playbook else None,
        "tier_goal": tier_playbook.goal if tier_playbook else None,
        "tier_action": tier_playbook.fa_action if tier_playbook else None,
    }
