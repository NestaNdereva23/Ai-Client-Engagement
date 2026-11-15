from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from app.agents.card_copy import copy_for_history

STEP_LABELS = {
    "gather": "Read what is already on file",
    "plan": "Wrote a short plan",
    "choose": "Chose what to do for each group",
    "check": "Ran the safety checks",
    "propose": "Wrote up what it suggests",
    "report": "Wrote the summary",
    "investigate": "Looked into each group",
    "record": "Saved what it found",
    "summarise": "Wrote the summary",
}

TOOL_WARN_LABELS = {
    "insight_not_accepted": "That needs a person to accept the finding first",
    "proposal_not_approved": "That needs a person to approve it first",
    "output_blocked": "Could not read the data it asked for",
}

GROUP_OUTCOMES = {
    "found_nothing": "found nothing worth raising",
    "said_nothing": "came back with nothing",
    "ran_out_of_turns": "ran out of time before it finished",
}

TONES = {
    "warning": "warning",
    "error": "critical",
    "insight_created": "good",
    "approval_needed": "attention",
    "run_completed": "good",
}


def group_label(name: str | None) -> str:
    return copy_for_history(name).screen_label if name else "a group"


def _plural(count: int, one: str, many: str) -> str:
    return f"{count} {one if count == 1 else many}"


def _run_completed(d: Mapping[str, Any]) -> str:
    if d.get("state") == "failed":
        return "Run stopped early"
    if d.get("state") == "cancelled":
        return "Run cancelled"
    found = d.get("insight_count")
    if isinstance(found, int):
        if found == 0:
            return "Run finished. Nothing worth raising tonight"
        return f"Run finished with {_plural(found, 'thing', 'things')} worth a look"
    return "Run finished"


def _status(d: Mapping[str, Any]) -> str | None:
    group = group_label(d.get("group_name"))
    stage = d.get("stage")
    groups = _plural(d.get("group_count") or 0, "group", "groups")
    if stage == "gathered":
        return f"Read the file on {groups}"
    if stage == "investigating":
        return f"Looking into {groups}, {d.get('at_once') or 1} at a time"
    if stage == "group_started":
        return f"Started on {group}"
    if stage == "group_done":
        wrote = d.get("insight_count") or 0
        if wrote:
            return f"Finished {group}, wrote {_plural(wrote, 'finding', 'findings')}"
        return f"Finished {group}, {GROUP_OUTCOMES.get(d.get('outcome'), 'nothing to raise')}"
    if stage == "chosen":
        return f"Chose an action for {_plural(d.get('chosen_count') or 0, 'group', 'groups')}"
    if stage == "checked":
        already = d.get("already_running_count") or 0
        if already:
            covered = _plural(already, "client", "clients")
            return f"Ran the safety checks, left out {covered} already covered by a run in progress"
        return f"Ran the safety checks on {groups}"
    if stage == "answer_ready":
        return "Answer ready"
    # Waiting between groups is a timer, not something a person needs to read.
    return None


def _warning(d: Mapping[str, Any]) -> str:
    if d.get("about") == "tool_call":
        return TOOL_WARN_LABELS.get(d.get("error"), "Could not do that")
    if d.get("about") == "group":
        return f"Ran out of time on {group_label(d.get('group_name'))}"
    if d.get("about") == "choose":
        return "Could not settle on an action, used the safe default"
    return "Something did not go to plan"


def _error(d: Mapping[str, Any]) -> str:
    if d.get("about") == "group":
        return f"Could not finish {group_label(d.get('group_name'))}"
    if d.get("about") == "step":
        return f"Could not finish the {d.get('step') or ''} step".strip()
    return "The run hit a problem and stopped"


def _run_started(d: Mapping[str, Any]) -> str:
    return (
        "Run started after the nightly risk job" if d.get("trigger") == "nightly" else "Run started"
    )


def _insight_created(d: Mapping[str, Any]) -> str:
    clients = _plural(d.get("client_count") or 0, "client", "clients")
    return f"Found something in {group_label(d.get('group_name'))}, covering {clients}"


def _insight_updated(d: Mapping[str, Any]) -> str | None:
    return "Attached the number behind that finding" if d.get("change") == "fact_added" else None


LINES: dict[str, Callable[[Mapping[str, Any]], str | None]] = {
    "run_started": _run_started,
    "run_completed": _run_completed,
    "run_status": _status,
    "step_started": lambda d: STEP_LABELS.get(d.get("step")),
    "insight_created": _insight_created,
    "insight_updated": _insight_updated,
    "insight_dismissed": lambda d: "A person set that finding aside",
    "proposal_created": lambda d: f"Wrote up a suggestion for {group_label(d.get('group_name'))}",
    "approval_needed": lambda d: (
        f"Waiting on you to decide about {group_label(d.get('group_name'))}"
    ),
    "approval_given": lambda d: "A person approved that suggestion",
    "action_started": lambda d: "Started the approved work",
    "action_completed": lambda d: "Finished the approved work",
    "warning": _warning,
    "error": _error,
    "paused": lambda d: "Paused",
    "resumed": lambda d: "Carried on",
}


def event_line(kind: str, detail: Mapping[str, Any] | None) -> tuple[str | None, str]:
    """The sentence a person reads for one event, and its tone. No sentence means hide it."""
    build = LINES.get(kind)
    detail = detail or {}
    text = build(detail) if build else None
    state = detail.get("state") if kind == "run_completed" else None
    if state == "failed":
        return text, "critical"
    if state == "cancelled":
        return text, "warning"
    return text, TONES.get(kind, "plain")
