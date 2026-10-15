from __future__ import annotations

from dataclasses import dataclass

from app.briefing.book import BookFacts, compact_money, format_money, plural


@dataclass(frozen=True)
class RenderedEmail:
    subject: str
    text_body: str


def _digest_link(console_base_url: str, fa_id: str) -> str | None:
    if not console_base_url:
        return None
    return f"{console_base_url.rstrip('/')}/digest/fa:{fa_id}"


def render_subject(facts: BookFacts) -> str:
    if facts.worklist_clients == 0:
        return "Morning Digest: Nothing On Your Worklist Today"
    return (
        f"Morning Digest: {plural(facts.worklist_clients).title()} On Your Worklist, "
        f"{compact_money(facts.worklist_at_risk_kes)} At Risk"
    )


def _summary_block(facts: BookFacts) -> list[str]:
    lines = [
        f"Clients on your worklist: {facts.worklist_clients:,} of {facts.assigned_clients:,}",
        f"Money held: {format_money(facts.worklist_balance_kes)}",
        f"Money at risk: {format_money(facts.worklist_at_risk_kes)}",
    ]
    if facts.worklist_clients:
        lines.append("")
        lines.append("By tier:")
        for tier in facts.tiers:
            if tier.clients:
                lines.append(
                    f"  {tier.label}: {plural(tier.clients)}, "
                    f"{format_money(tier.balance_kes)} held, "
                    f"{format_money(tier.at_risk_kes)} at risk"
                )
    if facts.held_out_clients:
        lines.append("")
        lines.append(f"Not on the worklist: {plural(facts.held_out_clients)}")
    if facts.month_end is not None:
        lines.append("")
        lines.append(f"Month end: {facts.month_end.line}")
    return lines


def render_digest_email(
    *, fa_id: str, facts: BookFacts, console_base_url: str = ""
) -> RenderedEmail:
    body = [f"Good morning {facts.advisor_name},", ""]
    if facts.worklist_clients == 0:
        body.append("Nothing on your worklist this morning.")
        body.append("")
    body.extend(_summary_block(facts))

    link = _digest_link(console_base_url, fa_id)
    if facts.worklist_clients and link is not None:
        body.append("")
        body.append(f"See your clients in order: {link}")

    return RenderedEmail(subject=render_subject(facts), text_body="\n".join(body).rstrip() + "\n")
