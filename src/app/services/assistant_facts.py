from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.briefing.assistant_bands import (
    contact_age_band,
    contact_count_band,
    deposit_rhythm_band,
    withdrawal_recency_band,
    withdrawal_size_band,
)
from app.briefing.render import BriefingFacts
from app.config import get_settings
from app.db.models.active_clients import (
    CHANNEL_BY_INTERACTION_TYPE,
    CONTACT_INTERACTION_TYPES,
    ActiveClientInteraction,
)
from app.privacy.fact_block import FUND_DISPLAY_NAMES, AssistantFactBlock
from app.risk.signals import NEAR_PRESERVATION_LINE
from app.services.briefing import deposit_trend_band_of
from app.services.client_next_actions import next_action_for_client
from app.transform.features import fund_type_from_name

RECENT_CONTACT_DAYS = 90
RESCHEDULE_MEETING = "reschedule_meeting"


@dataclass(frozen=True)
class ContactSummary:
    channel: str
    outcome: str
    age_band: str
    count_band: str


def _contact_summary(
    session: Session, client_id: int, unit_fund_id: int, today: date
) -> ContactSummary:
    zone = ZoneInfo(get_settings().db_timezone)
    rows = session.execute(
        select(
            ActiveClientInteraction.type,
            ActiveClientInteraction.outcome,
            ActiveClientInteraction.created_at,
        )
        .where(
            ActiveClientInteraction.client_id == client_id,
            ActiveClientInteraction.unit_fund_id == unit_fund_id,
            ActiveClientInteraction.type.in_(CONTACT_INTERACTION_TYPES),
        )
        .order_by(ActiveClientInteraction.id.desc())
    ).all()
    if not rows:
        return ContactSummary("none", "none", contact_age_band(None), contact_count_band(0))
    latest = rows[0]
    latest_day = latest.created_at.astimezone(zone).date()
    cutoff = today - timedelta(days=RECENT_CONTACT_DAYS)
    recent = sum(1 for row in rows if row.created_at.astimezone(zone).date() >= cutoff)
    return ContactSummary(
        CHANNEL_BY_INTERACTION_TYPE[latest.type],
        latest.outcome or "none",
        contact_age_band(max(0, (today - latest_day).days)),
        contact_count_band(recent),
    )


def _missed_meeting(session: Session, client_id: int, unit_fund_id: int) -> bool:
    action = next_action_for_client(session, client_id, unit_fund_id).action
    return action is not None and action.email_purpose == RESCHEDULE_MEETING


def build_assistant_facts(
    session: Session, facts: BriefingFacts, client_id: int, unit_fund_id: int, today: date
) -> AssistantFactBlock:
    contact = _contact_summary(session, client_id, unit_fund_id, today)
    signals = facts.signals
    return AssistantFactBlock(
        risk_band=facts.risk_band,
        balance_tier=facts.balance_tier if facts.balance_tier != "Unknown" else None,
        value_tier=facts.value_tier,
        recency_band=facts.recency_band,
        deposit_trend_band=deposit_trend_band_of(facts.deposit_trend),
        deposit_rhythm_band=deposit_rhythm_band(facts.typical_gap_days),
        largest_withdrawal_size_band=withdrawal_size_band(facts.withdrawal_pct),
        last_withdrawal_recency_band=withdrawal_recency_band(facts.days_since_withdrawal),
        fund_name=FUND_DISPLAY_NAMES.get(fund_type_from_name(facts.fund_name)),
        sig_heavy_withdrawal=signals.get("sig_heavy_withdrawal"),
        sig_dormant=signals.get("sig_dormant"),
        sig_broken_pattern=signals.get("sig_broken_pattern"),
        sig_shrinking=signals.get("sig_shrinking"),
        near_preservation_line=signals.get(NEAR_PRESERVATION_LINE),
        sig_never_repeated=signals.get("sig_never_repeated"),
        holds_both_funds=facts.holds_both_funds,
        last_contact_channel=contact.channel,
        last_contact_outcome=contact.outcome,
        last_contact_age_band=contact.age_band,
        contacts_last_three_months_band=contact.count_band,
        missed_meeting=_missed_meeting(session, client_id, unit_fund_id) or None,
    )
