from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from app.agents.email_agent import build_system_prompt
from app.agents.graph import DraftParser, GuardrailCheck, PromptBuilder
from app.agents.guardrails import DEFAULT_GUARDRAIL_CHECKS, SMS_GUARDRAIL_CHECKS
from app.agents.sms_agent import build_sms_system_prompt
from app.schemas.email_draft import parse_email_draft
from app.schemas.sms_draft import parse_sms_draft

EMAIL_CHANNEL = "email"
SMS_CHANNEL = "sms"


@dataclass(frozen=True)
class ChannelSpec:
    prompt_builder: PromptBuilder
    draft_parser: DraftParser
    guardrail_checks: Sequence[GuardrailCheck]


# Where the batch path and the test draft endpoint pick a channel's own prompt/parser/guardrails.
CHANNEL_SPECS: dict[str, ChannelSpec] = {
    EMAIL_CHANNEL: ChannelSpec(build_system_prompt, parse_email_draft, DEFAULT_GUARDRAIL_CHECKS),
    SMS_CHANNEL: ChannelSpec(build_sms_system_prompt, parse_sms_draft, SMS_GUARDRAIL_CHECKS),
}


class UnknownDraftChannel(KeyError):
    pass


def channel_spec(channel: str) -> ChannelSpec:
    try:
        return CHANNEL_SPECS[channel]
    except KeyError:
        raise UnknownDraftChannel(f"no channel spec registered for {channel!r}") from None
