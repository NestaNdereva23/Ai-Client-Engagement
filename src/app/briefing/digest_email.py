"""Render one account manager's morning email.

Pure over already-fetched inputs, the same discipline as digest/build.py and
risk/scoring.py: nothing here touches the database, the mailer, or a model.
workers/digest_email.py gathers the facts and does the sending.

The email carries counts and money only, aggregated across an advisor's
whole population -- no client name, no fund name, no per-client narrative.
The full call list lives in the console; the email just says how many
people are on it and points there.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field

from app.risk.signals import SIGNAL_LABELS


@dataclass(frozen=True)
class SignalCount:
    """How many of an advisor's clients one signal fired for."""

    tag: str
    clients: int

    @property
    def label(self) -> str:
        return SIGNAL_LABELS.get(f"sig_{self.tag}", self.tag)


@dataclass(frozen=True)
class DigestEmailSummary:
    """The numbers the email is built from.

    Every one of these counts the advisor's whole population for the night,
    not a capped subset, so the email never understates the morning.
    """

    clients_to_call: int
    total_at_risk: float
    new_or_escalated: int
    watchlist_clients: int
    watchlist_at_risk: float
    signals: Sequence[SignalCount] = field(default_factory=tuple)

    @property
    def top_signal(self) -> SignalCount | None:
        return self.signals[0] if self.signals else None

    @property
    def runner_up_signal(self) -> SignalCount | None:
        return self.signals[1] if len(self.signals) > 1 else None


@dataclass(frozen=True)
class RenderedEmail:
    """A subject and a body, ready to hand to a Mailer."""

    subject: str
    text_body: str


def rank_signals(tag_lists: Sequence[Sequence[str]]) -> tuple[SignalCount, ...]:
    """Signal tags counted across a population, most repeated first.

    One client counts once per tag however many times it appears in their
    own list. Ties break on the tag name so two runs over the same night
    always name the same top signal.
    """
    counter: Counter[str] = Counter()
    for tags in tag_lists:
        counter.update(set(tags))
    ordered = sorted(counter.items(), key=lambda item: (-item[1], item[0]))
    return tuple(SignalCount(tag=tag, clients=count) for tag, count in ordered)


def format_money(value: float) -> str:
    """KES with thousands separators and no decimals, for the body."""
    return f"KES {round(value):,}"


def compact_money(value: float) -> str:
    """KES shortened to fit a phone's subject line: KES 4.3M, KES 812K."""
    amount = round(value)
    if amount >= 1_000_000:
        return f"KES {amount / 1_000_000:.1f}M"
    if amount >= 1_000:
        return f"KES {amount / 1_000:.0f}K"
    return f"KES {amount:,}"


def _digest_link(console_base_url: str, fa_id: int) -> str | None:
    if not console_base_url:
        return None
    return f"{console_base_url.rstrip('/')}/digest/fa:{fa_id}"


def render_subject(summary: DigestEmailSummary) -> str:
    """The two numbers that decide whether this gets opened on a phone: how
    many are at risk, and how much money that is.
    """
    if summary.clients_to_call == 0:
        return "Morning Digest: No At-Risk Clients Today"
    plural = "" if summary.clients_to_call == 1 else "s"
    return (
        f"Morning Digest: {summary.clients_to_call} At-Risk Client{plural} Holding "
        f"{compact_money(summary.total_at_risk)}"
    )


def _summary_block(summary: DigestEmailSummary, fa_id: int, console_base_url: str) -> list[str]:
    lines = [
        f"Clients to call today: {summary.clients_to_call}",
        f"Money at risk: {format_money(summary.total_at_risk)}",
    ]
    if summary.clients_to_call:
        average = summary.total_at_risk / summary.clients_to_call
        lines.append(f"Average at risk per client: {format_money(average)}")
    if summary.top_signal is not None:
        top = summary.top_signal
        lines.append(f"Most common reason: {top.label} ({top.clients} clients)")
    if summary.runner_up_signal is not None:
        second = summary.runner_up_signal
        lines.append(f"Next most common: {second.label} ({second.clients} clients)")
    lines.append(f"New or worse since yesterday: {summary.new_or_escalated}")

    link = _digest_link(console_base_url, fa_id)
    watchlist = (
        f"Watchlist: {summary.watchlist_clients} clients, {format_money(summary.watchlist_at_risk)}"
    )
    if link is not None:
        watchlist = f"{watchlist} ({link})"
    lines.append(watchlist)

    if summary.clients_to_call and link is not None:
        lines.append("")
        lines.append(f"See today's full call list: {link}")
    return lines


def render_digest_email(
    *,
    fa_id: int,
    advisor_name: str,
    summary: DigestEmailSummary,
    console_base_url: str = "",
) -> RenderedEmail:
    """One advisor's whole email.

    An advisor with nobody to call still gets a message saying so, so an
    inbox with no email in it always means something actually broke.
    """
    body = [f"Good morning {advisor_name},", ""]
    if summary.clients_to_call == 0:
        body.append("Nothing on your call list this morning.")
        body.append("")
    body.extend(_summary_block(summary, fa_id, console_base_url))

    return RenderedEmail(
        subject=render_subject(summary),
        text_body="\n".join(body).rstrip() + "\n",
    )
