from __future__ import annotations

import difflib
import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

MAX_RETRIES = 2
SUBJECT_TOO_CLOSE = 0.8
MAX_RESEARCH_NOTES = 3
RESEARCH_NOTE_LIMIT = 500
MIN_FACT_LENGTH = 15


@dataclass(frozen=True)
class PlanError:
    code: str
    message: str
    slot: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "slot": self.slot, "message": self.message}


@dataclass(frozen=True)
class FactSource:
    text: str
    url: str | None
    retrieved_at: str
    tool: str


@dataclass(frozen=True)
class PlanRules:
    allowed_angles: Collection[str]
    angle_families: Mapping[str, str | None]
    min_gap_days: int
    max_gap_days: int


def clean_text(value: Any) -> str:
    return " ".join(str(value or "").split())


def _squash(text: str) -> str:
    return clean_text(text).casefold()


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _subject_words(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", text.casefold()))


def subjects_too_close(first: str, second: str) -> bool:
    one, two = _subject_words(first), _subject_words(second)
    if not one or not two:
        return False
    return difflib.SequenceMatcher(None, one, two).ratio() >= SUBJECT_TOO_CLOSE


def resolve_cited_facts(
    raw: Any, sources: Sequence[FactSource]
) -> tuple[list[dict[str, Any]], list[str]]:
    if not isinstance(raw, list):
        return [], []
    cited: list[dict[str, Any]] = []
    unsupported: list[str] = []
    for note in raw[:MAX_RESEARCH_NOTES]:
        text = clean_text(note)[:RESEARCH_NOTE_LIMIT]
        if not text:
            continue
        source = _source_for(text, sources)
        if source is None:
            unsupported.append(text)
            continue
        cited.append(
            {
                "text": text,
                "url": source.url,
                "retrieved_at": source.retrieved_at,
                "tool": source.tool,
            }
        )
    return cited, unsupported


def _source_for(text: str, sources: Sequence[FactSource]) -> FactSource | None:
    squashed = _squash(text)
    if len(squashed) < MIN_FACT_LENGTH:
        return None
    return next((source for source in sources if squashed in _squash(source.text)), None)


def _angle_errors(
    number: int, angle: Any, slot: Mapping[str, Any], rules: PlanRules
) -> list[PlanError]:
    if not isinstance(angle, str) or angle not in rules.allowed_angles:
        return [PlanError("unknown_angle", f"{angle!r} is not a live, open angle", number)]
    last = slot.get("prior_angle")
    used = {other for other in (*slot.get("earlier_angles", ()), last) if other}
    if angle == last:
        message = f"slot {number} was already sent {angle!r} last time"
        return [PlanError("repeats_last_angle", message, number)]
    if angle in used:
        message = f"slot {number} was already sent {angle!r} at an earlier touch"
        return [PlanError("repeats_earlier_angle", message, number)]
    family = rules.angle_families.get(angle)
    clash = next(
        (other for other in sorted(used) if family and rules.angle_families.get(other) == family),
        None,
    )
    if clash is not None:
        message = f"slot {number} was already sent {clash!r}, the same family as {angle!r}"
        return [PlanError("repeats_earlier_angle", message, number)]
    return []


def _gap_errors(number: int, gap: Any, rules: PlanRules) -> list[PlanError]:
    if _is_int(gap) and rules.min_gap_days <= gap <= rules.max_gap_days:
        return []
    message = (
        f"slot {number}: gap_days must be between {rules.min_gap_days} and {rules.max_gap_days}"
    )
    return [PlanError("gap_out_of_range", message, number)]


def _wording_errors(number: int, decision: Mapping[str, Any], slot: Mapping[str, Any]):
    errors: list[PlanError] = []
    if not clean_text(decision.get("reason")):
        errors.append(PlanError("missing_reason", f"slot {number} needs a short reason", number))
    if not clean_text(decision.get("next_time")):
        errors.append(
            PlanError("missing_next_time", f"slot {number} needs a next_time note", number)
        )
    subject = clean_text(decision.get("suggested_subject"))
    close = next(
        (
            earlier
            for earlier in slot.get("earlier_subjects", ())
            if subject and subjects_too_close(subject, earlier)
        ),
        None,
    )
    if close is not None:
        message = f"slot {number}: the suggested subject is too close to {close!r}, already sent"
        errors.append(PlanError("subject_too_close", message, number))
    return errors


def check_decisions(
    decisions: Sequence[Mapping[str, Any]],
    slots: Mapping[int, Mapping[str, Any]],
    rules: PlanRules,
    sources: Sequence[FactSource],
) -> tuple[list[PlanError], dict[int, list[dict[str, Any]]]]:
    errors: list[PlanError] = []
    facts: dict[int, list[dict[str, Any]]] = {}
    seen: set[int] = set()
    for decision in decisions:
        number = decision.get("slot")
        slot = slots.get(number) if _is_int(number) else None
        if slot is None:
            message = f"slot {number!r} was not one of this run's due touches"
            errors.append(PlanError("unknown_slot", message))
            continue
        if number in seen:
            message = f"slot {number} was decided more than once in this call"
            errors.append(PlanError("duplicate_slot", message, number))
            continue
        seen.add(number)
        errors.extend(_angle_errors(number, decision.get("angle"), slot, rules))
        errors.extend(_gap_errors(number, decision.get("gap_days"), rules))
        errors.extend(_wording_errors(number, decision, slot))
        cited, unsupported = resolve_cited_facts(decision.get("research_notes"), sources)
        errors.extend(
            PlanError(
                "unsupported_fact",
                f"slot {number}: not in anything search_approved_sites or "
                "get_weekly_report_facts returned "
                f"in this run: {note!r}",
                number,
            )
            for note in unsupported
        )
        facts[number] = cited
    missing = sorted(set(slots) - seen)
    if missing:
        errors.append(PlanError("incomplete", f"slots {missing} still need a decision"))
    return errors, facts
