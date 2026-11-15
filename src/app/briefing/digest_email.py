from __future__ import annotations

from dataclasses import dataclass

from app.briefing.book import compact_money, plural
from app.briefing.daily_brief import BriefGroup, DailyBrief

GROUPS_SHOWN = 3
BRIEF_PATH = "/dashboard?view=ai-outreach&section=at-risk"
ORDER_LINE = "They are in order of urgency, with money already leaving first."
AT_RISK_NOTE = (
    "KSh at risk counts each balance in proportion to its risk score. It is not the full balance."
)
CLOSING_LINE = (
    "No client names are in this email. Open the brief to see who, listed in the order above."
)


@dataclass(frozen=True)
class RenderedEmail:
    subject: str
    text_body: str


def _brief_link(ticketing_base_url: str) -> str | None:
    if not ticketing_base_url:
        return None
    return f"{ticketing_base_url.rstrip('/')}{BRIEF_PATH}"


def _headline_lines(brief: DailyBrief) -> list[str]:
    if not brief.headlines:
        return ["Nothing new since yesterday."]
    return [f"- {headline.lead} {headline.text}".rstrip() for headline in brief.headlines]


def _group_size(group: BriefGroup) -> str:
    if group.could_leave_kes <= 0:
        return f"{plural(group.clients)}."
    return f"{plural(group.clients)}, {compact_money(group.could_leave_kes)} at risk."


def _group_lines(groups: tuple[BriefGroup, ...]) -> list[str]:
    lines = ["Where to start", ORDER_LINE, ""]
    for number, group in enumerate(groups, start=1):
        lines.append(f"{number}. {group.title}")
        lines.append(f"   {_group_size(group)}")
        if group.warning_signs:
            lines.append(f"   {group.warning_signs}")
        if group.sharp_fact:
            lines.append(f"   {group.sharp_fact}")
        lines.append(f"   Next step: {group.what_to_do}")
        lines.append("")
    if any(group.could_leave_kes > 0 for group in groups):
        lines.extend([AT_RISK_NOTE, ""])
    return lines


def render_digest_email(*, brief: DailyBrief, ticketing_base_url: str = "") -> RenderedEmail:
    body = [f"Good morning {brief.advisor_name},", ""]
    if brief.worklist_clients == 0:
        body.extend(["Nothing on your worklist this morning.", ""])
    else:
        body.extend(["Here is what needs your attention today.", ""])
        body.extend(["Today at a glance", *_headline_lines(brief), ""])
        if brief.groups:
            body.extend(_group_lines(brief.groups[:GROUPS_SHOWN]))
    if brief.month_end is not None:
        body.extend([f"Month end: {brief.month_end.line}", ""])

    link = _brief_link(ticketing_base_url)
    if brief.worklist_clients and link is not None:
        body.extend([f"Open today's brief: {link}", ""])
    if brief.worklist_clients:
        body.append(CLOSING_LINE)

    return RenderedEmail(subject=brief.subject, text_body="\n".join(body).rstrip() + "\n")
