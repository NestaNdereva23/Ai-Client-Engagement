"""The open question the agent is asked of each group, all in one place.
The last entry has no filter behind it: it is where the agent may look for something new."""

from __future__ import annotations

from app.agents.situations import (
    APPROACHING_PRESERVATION_LINE,
    DEPOSIT_SIZE_FALLING,
    FA_CALL_OVERDUE,
    FIRST_DEPOSIT_NO_REPEAT,
    FREQUENT_DEPOSITOR_ONE_FUND,
    FREQUENT_DEPOSITOR_WITHDRAWING,
    GRADUAL_DEPOSITOR_DUE,
    GRADUAL_DEPOSITOR_WITHDRAWING,
    RISK_ROSE_NO_CALL,
)

EVERYTHING_ELSE = "everything_else"

GROUP_QUESTIONS: dict[str, str] = {
    GRADUAL_DEPOSITOR_DUE: (
        "Whose pause is longer than usual for them, and who simply pays in at a slow pace?"
    ),
    GRADUAL_DEPOSITOR_WITHDRAWING: (
        "Who is taking money out because they need it, and who might be leaving?"
    ),
    FREQUENT_DEPOSITOR_ONE_FUND: (
        "Which of these look like our clients with several funds did before they took a second one?"
    ),
    FREQUENT_DEPOSITOR_WITHDRAWING: (
        "Whose withdrawals are cancelling out their deposits, and who is just moving cash around?"
    ),
    DEPOSIT_SIZE_FALLING: "Is this the time of year, or is something actually wrong?",
    FIRST_DEPOSIT_NO_REPEAT: (
        "Who has not built a habit of paying in, and who is doing fine on their own?"
    ),
    FA_CALL_OVERDUE: "Which of these calls cannot wait any longer?",
    RISK_ROSE_NO_CALL: (
        "Whose situation got worse overnight in a way nobody will hear about otherwise?"
    ),
    APPROACHING_PRESERVATION_LINE: (
        "Who is likely to fall below the line soon, and who is only having a quiet spell?"
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
