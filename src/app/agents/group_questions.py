"""The open question the agent is asked of each group, all in one place.

A group is still a plain database filter. What changes here is that the
agent is not handed a menu of actions to pick from: it is handed a question
and the tools to answer it with, and it may answer that a group holds
nothing worth raising.

The last entry has no filter behind it at all. It is where the agent may
look for something none of the named groups would catch, which is the only
place in the system it is allowed to find something nobody asked about.
"""

from __future__ import annotations

from app.agents.watchlist import (
    FEES_WILL_EMPTY,
    GETTING_SMALLER,
    HEALTHY_ONE_FUND,
    MORE_URGENT_BUT_NOT_CALLED,
    SIGNED_UP_RECENTLY,
    VERY_SMALL_AND_QUIET,
    WAITING_ON_A_CALL,
)

EVERYTHING_ELSE = "everything_else"

GROUP_QUESTIONS: dict[str, str] = {
    SIGNED_UP_RECENTLY: (
        "Who has not built a habit of paying in, and who is doing fine on their own?"
    ),
    FEES_WILL_EMPTY: "Which of these can still be saved, and which are already gone?",
    VERY_SMALL_AND_QUIET: (
        "Which of these are worth winning back, and which are too small to chase?"
    ),
    GETTING_SMALLER: "Is this the time of year, or is something actually wrong?",
    HEALTHY_ONE_FUND: (
        "Which of these look like our clients with several funds did before they took a second one?"
    ),
    WAITING_ON_A_CALL: "Which of these cannot wait for the team to catch up?",
    MORE_URGENT_BUT_NOT_CALLED: (
        "Whose situation got worse overnight in a way nobody will hear about otherwise?"
    ),
    EVERYTHING_ELSE: "Is anything happening here that none of the other groups would catch?",
}

INVESTIGATION_GROUP_NAMES: tuple[str, ...] = tuple(GROUP_QUESTIONS)


def question_for(group_name: str) -> str:
    """The open question for one group, or a plain error when it has none."""
    try:
        return GROUP_QUESTIONS[group_name]
    except KeyError:
        raise KeyError(f"'{group_name}' has no question written for it") from None
