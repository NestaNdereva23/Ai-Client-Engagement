"""What a message written off a finding must never say.

A finding can be worded for the people who read it on a screen: a group can
be called quiet, shrinking, or at risk of leaving. None of that may reach a
client. The agent writes that line itself when it writes the finding, and
this is the one place it is turned into a rule the drafting prompt carries,
so an internal label cannot travel from the finding to an inbox.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.db.models.agent_insight import AgentInsight
from app.db.models.agent_proposal import AgentProposal

GROUP_NAME_RULE = (
    "Never use the wording this group is described by internally, and never "
    "tell the client they were put in a group at all."
)


def insight_prohibitions(insight: AgentInsight) -> tuple[str, ...]:
    """The lines one finding adds to what a message may never say."""
    lines = [GROUP_NAME_RULE]
    avoid = (insight.avoid_saying or "").strip()
    if avoid:
        lines.append(f"Never claim or imply the following: {avoid}")
    return tuple(lines)


def proposal_prohibitions(session: Session, proposal: AgentProposal) -> tuple[str, ...]:
    """The same lines for a proposal, read through the finding it came from.

    A proposal with no finding behind it adds nothing: the campaign and
    angle level rules already cover it.
    """
    if proposal.insight_id is None:
        return ()
    insight = session.get(AgentInsight, proposal.insight_id)
    if insight is None:
        return ()
    return insight_prohibitions(insight)
