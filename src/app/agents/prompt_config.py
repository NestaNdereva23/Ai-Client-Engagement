from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db.models.prompt_config import OutputPolicy, SafetyPolicy, VoiceContract
from app.personalization.eligibility import resolve_fact_eligibility
from app.rules import versioning

_VOICE_SECTIONS = (
    "persona",
    "tone",
    "writing_style",
    "structure",
    "subject_guidance",
    "length_guidance",
    "readability_guidance",
)


def build_voice_block(
    *,
    body_markdown: str | None = None,
    persona: str | None = None,
    tone: str | None = None,
    writing_style: str | None = None,
    structure: str | None = None,
    subject_guidance: str | None = None,
    length_guidance: str | None = None,
    readability_guidance: str | None = None,
) -> str:
    if body_markdown:
        return body_markdown
    values = {
        "persona": persona,
        "tone": tone,
        "writing_style": writing_style,
        "structure": structure,
        "subject_guidance": subject_guidance,
        "length_guidance": length_guidance,
        "readability_guidance": readability_guidance,
    }
    return "\n\n".join(values[name] for name in _VOICE_SECTIONS if values[name])


def render_output_rules(output_row: OutputPolicy) -> str:
    parts: list[str] = []
    if output_row.output_schema_note:
        parts.append(output_row.output_schema_note)

    fields = (output_row.placeholder_rules or {}).get("fields") or []
    if fields:
        tokens = ", ".join(f"{{{{{field}}}}}" for field in fields)
        parts.append(
            "PLACEHOLDERS: You may use {{first_name}} and {{fund_name}}. If the supplied facts "
            f"contain any of these tokens, reproduce them exactly when relevant: {tokens}. "
            "These tokens represent client specific values that will be filled after review. "
            "Do not replace them with numbers, calculate values from them, or omit them when the "
            "selected angle explicitly requires the token. These are the ONLY permitted "
            "placeholders. Never create, modify, or use another placeholder."
        )

    restrictions = output_row.formatting_restrictions or {}
    bounds: list[str] = []
    if "body_min_words" in restrictions and "body_max_words" in restrictions:
        bounds.append(
            "The acceptable body range is "
            f"{restrictions['body_min_words']} to {restrictions['body_max_words']} words."
        )
    if "subject_max_words" in restrictions:
        bounds.append(f"Never exceed {restrictions['subject_max_words']} words in the subject.")
    if restrictions.get("no_em_dashes"):
        bounds.append("Do not use em dashes or hyphens anywhere in the subject or body.")
    if bounds:
        parts.append("FORMATTING: " + " ".join(bounds))

    return " ".join(parts)


@dataclass(frozen=True)
class AgentConfiguration:
    tier_contract_version: int | None
    voice_contract_version: int | None
    safety_policy_version: int | None
    output_policy_version: int | None
    personalization_policy_version: int | None
    voice_text: str | None
    safety_words: tuple[str, ...] | None
    safety_phrases: tuple[str, ...] | None
    campaign_prohibitions: tuple[str, ...] | None
    output_rules: str | None
    default_sign_off: str | None
    allowed_placeholder_fields: tuple[str, ...] | None
    fact_eligibility: Mapping[str, str] | None


HARDCODED_CONFIGURATION = AgentConfiguration(
    tier_contract_version=None,
    voice_contract_version=None,
    safety_policy_version=None,
    output_policy_version=None,
    personalization_policy_version=None,
    voice_text=None,
    safety_words=None,
    safety_phrases=None,
    campaign_prohibitions=None,
    output_rules=None,
    default_sign_off=None,
    allowed_placeholder_fields=None,
    fact_eligibility=None,
)


def _resolve_version(
    session: Session, component_type: str, component_key: str, on: date
) -> int | None:
    spec = versioning.COMPONENTS[component_type]
    column = getattr(spec.model, spec.version_column)
    stmt = select(column).where(
        spec.model.valid_from <= on,
        or_(spec.model.valid_to.is_(None), spec.model.valid_to > on),
    )
    if spec.key_column is not None:
        stmt = stmt.where(getattr(spec.model, spec.key_column) == component_key)
    return session.scalar(stmt.order_by(spec.model.valid_from.desc(), column.desc()).limit(1))


def _configuration_from_rows(
    *,
    tier_contract_version: int | None,
    voice_contract_version: int | None,
    safety_policy_version: int | None,
    output_policy_version: int | None,
    personalization_policy_version: int | None,
    voice_row: VoiceContract | None,
    safety_row: SafetyPolicy | None,
    output_row: OutputPolicy | None,
    fact_eligibility: Mapping[str, str] | None = None,
) -> AgentConfiguration:
    output_rules = None
    if output_row is not None and voice_row is not None and voice_row.body_markdown is None:
        output_rules = render_output_rules(output_row)

    allowed_placeholder_fields = None
    if output_row is not None and output_row.placeholder_rules:
        fields = output_row.placeholder_rules.get("fields")
        if fields:
            allowed_placeholder_fields = tuple(fields)

    return AgentConfiguration(
        tier_contract_version=tier_contract_version,
        voice_contract_version=voice_contract_version,
        safety_policy_version=safety_policy_version,
        output_policy_version=output_policy_version,
        personalization_policy_version=personalization_policy_version,
        voice_text=voice_row.rendered_text if voice_row is not None else None,
        safety_words=(
            tuple(safety_row.banned_words) if safety_row and safety_row.banned_words else None
        ),
        safety_phrases=(
            tuple(safety_row.banned_phrases) if safety_row and safety_row.banned_phrases else None
        ),
        campaign_prohibitions=(
            tuple(safety_row.campaign_prohibitions)
            if safety_row and safety_row.campaign_prohibitions
            else None
        ),
        output_rules=output_rules,
        default_sign_off=voice_row.default_sign_off if voice_row is not None else None,
        allowed_placeholder_fields=allowed_placeholder_fields,
        fact_eligibility=fact_eligibility,
    )


def resolve_active_configuration(
    session: Session,
    *,
    angle: str | None,
    tier: str | None,
    at: date | None = None,
    settings: Settings | None = None,
) -> AgentConfiguration:
    settings = settings or get_settings()
    if settings.prompt_config_source == "hardcoded":
        return HARDCODED_CONFIGURATION

    on = at or date.today()
    default_key = versioning.DEFAULT_COMPONENT_KEY

    tier_contract_version = (
        _resolve_version(session, "tier_contract", tier, on) if tier is not None else None
    )
    voice_contract_version = _resolve_version(session, "voice_contract", default_key, on)
    safety_policy_version = _resolve_version(session, "safety_policy", default_key, on)
    output_policy_version = _resolve_version(session, "output_policy", default_key, on)
    personalization_policy_version = _resolve_version(
        session, "personalization_policy", default_key, on
    )

    voice_row = (
        session.scalar(select(VoiceContract).where(VoiceContract.version == voice_contract_version))
        if voice_contract_version is not None
        else None
    )
    safety_row = (
        session.scalar(select(SafetyPolicy).where(SafetyPolicy.version == safety_policy_version))
        if safety_policy_version is not None
        else None
    )
    output_row = (
        session.scalar(select(OutputPolicy).where(OutputPolicy.version == output_policy_version))
        if output_policy_version is not None
        else None
    )
    fact_eligibility = (
        resolve_fact_eligibility(session, angle, personalization_policy_version)
        if personalization_policy_version is not None
        else None
    )

    return _configuration_from_rows(
        tier_contract_version=tier_contract_version,
        voice_contract_version=voice_contract_version,
        safety_policy_version=safety_policy_version,
        output_policy_version=output_policy_version,
        personalization_policy_version=personalization_policy_version,
        voice_row=voice_row,
        safety_row=safety_row,
        output_row=output_row,
        fact_eligibility=fact_eligibility,
    )


def resolve_pinned_configuration(
    session: Session,
    *,
    voice_version: int | None,
    safety_version: int | None,
    output_version: int | None,
    personalization_version: int | None,
    tier_contract_version: int | None = None,
    angle: str | None = None,
) -> AgentConfiguration:
    voice_row = (
        session.scalar(select(VoiceContract).where(VoiceContract.version == voice_version))
        if voice_version is not None
        else None
    )
    safety_row = (
        session.scalar(select(SafetyPolicy).where(SafetyPolicy.version == safety_version))
        if safety_version is not None
        else None
    )
    output_row = (
        session.scalar(select(OutputPolicy).where(OutputPolicy.version == output_version))
        if output_version is not None
        else None
    )
    fact_eligibility = (
        resolve_fact_eligibility(session, angle, personalization_version)
        if personalization_version is not None
        else None
    )

    return _configuration_from_rows(
        tier_contract_version=tier_contract_version,
        voice_contract_version=voice_row.version if voice_row is not None else None,
        safety_policy_version=safety_row.version if safety_row is not None else None,
        output_policy_version=output_row.version if output_row is not None else None,
        personalization_policy_version=personalization_version,
        voice_row=voice_row,
        safety_row=safety_row,
        output_row=output_row,
        fact_eligibility=fact_eligibility,
    )
