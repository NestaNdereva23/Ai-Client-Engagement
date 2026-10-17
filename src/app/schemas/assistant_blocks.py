from __future__ import annotations

from typing import Any

from app.briefing.email_options import EmailOption

TEXT = "text"
STEPS = "steps"
OPTIONS = "options"
RESULT = "result"
SUGGESTIONS = "suggestions"
NOTICE = "notice"
CHIPS = "chips"

INFO = "info"
WARNING = "warning"
ERROR = "error"

PRIVATE_PREFIX = "_"
ANGLE_KEY = f"{PRIVATE_PREFIX}angle"

DRAFT_EMAIL = "draft_email"
REVIEW_MESSAGE = "review_message"

Block = dict[str, Any]


def text_block(text: str, *, title: str | None = None, layout: str = "paragraphs") -> Block:
    block: Block = {"type": TEXT, "text": text, "layout": layout}
    if title:
        block["title"] = title
    return block


def steps_block(labels: list[str], summary: str) -> Block:
    return {"type": STEPS, "summary": summary, "items": [{"label": label} for label in labels]}


def notice_block(text: str, tone: str = INFO) -> Block:
    return {"type": NOTICE, "tone": tone, "text": text}


def chips_block(items: list[str]) -> Block:
    return {"type": CHIPS, "items": [{"label": item} for item in items]}


def suggestions_block(starters: list[dict[str, Any]]) -> Block:
    return {
        "type": SUGGESTIONS,
        "items": [
            {"label": starter["label"], "starter_code": starter["code"]} for starter in starters
        ],
    }


def options_block(
    options: list[EmailOption], *, turn_id: int, action_label: str, intro: str | None = None
) -> Block:
    items = [
        {
            "id": option.option_id,
            "title": option.title,
            "summary": option.summary,
            "why": option.why,
            "tags": list(option.tags),
            "recommended": option.recommended,
            "action": {
                "label": action_label,
                "token": action_token(DRAFT_EMAIL, turn_id, option.option_id),
            },
            ANGLE_KEY: option.angle,
        }
        for option in sorted(options, key=lambda option: not option.recommended)
    ]
    block: Block = {"type": OPTIONS, "items": items}
    if intro:
        block["intro"] = intro
    return block


def result_block(
    title: str,
    text: str,
    details: list[tuple[str, str]],
    *,
    next_kind: str,
    next_ref: str,
    next_label: str,
    auto_open: bool = True,
) -> Block:
    return {
        "type": RESULT,
        "title": title,
        "text": text,
        "details": [{"label": label, "value": value} for label, value in details],
        "next": {"kind": next_kind, "ref": next_ref, "label": next_label, "auto_open": auto_open},
    }


def action_token(kind: str, turn_id: int, option_id: str) -> str:
    return f"{kind}:{turn_id}:{option_id}"


def parse_action_token(token: str) -> tuple[str, int, str]:
    parts = token.split(":")
    if len(parts) != 3 or not parts[1].isdigit() or not parts[2]:
        raise ValueError("the action was not recognised")
    return parts[0], int(parts[1]), parts[2]


def public_blocks(blocks: list[Block]) -> list[Block]:
    return [_strip_private(block) for block in blocks]


def _strip_private(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _strip_private(item)
            for key, item in value.items()
            if not key.startswith(PRIVATE_PREFIX)
        }
    if isinstance(value, list):
        return [_strip_private(item) for item in value]
    return value
