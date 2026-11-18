from __future__ import annotations

from collections.abc import Sequence

from fpdf import FPDF
from fpdf.fonts import FontFace

from app.briefing.book import compact_money, plural
from app.services.weekly_report import AdvisorReport, MoneyLine, day_label

APPBAR = ((18, 179, 168), (17, 121, 166), (11, 90, 138))
TEXT_1 = (20, 25, 34)
TEXT_2 = (92, 101, 114)
LINE = (225, 229, 234)
WHITE = (255, 255, 255)
MARGIN = 14
BAND_HEIGHT = 30
ROWS_SHOWN = 25
FOLLOW_UPS_SHOWN = 8
COUNTED_NOTE = (
    'How this is counted: a client is "flagged" when the nightly risk run scores them at or above '
    'the at-risk line and their balance is not in small balance review. "Reached" means a logged '
    "call, email, WhatsApp or meeting that was not a no answer. Snoozing or dismissing does not "
    "count as a reach-out. Money in and out leaves out fees and other system deductions. KSh at "
    "risk counts each balance in proportion to its risk score."
)
_PLAIN = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-", "−": "-"})


def _text(value: object) -> str:
    cleaned = str(value).translate(_PLAIN).replace("**", "").replace("__", "").replace("--", "-")
    return cleaned.replace("~~", "").encode("latin-1", "replace").decode("latin-1")


def _week(start, end) -> str:
    return f"{day_label(start)} to {day_label(end)} {end.year}"


def _net(value: float) -> str:
    return f"{'-' if value < 0 else '+'}{compact_money(abs(value))}"


def _pct(part: int, whole: int) -> str:
    return f"{round(100 * part / whole)}%" if whole else "-"


def _days(value: float | None) -> str:
    return "-" if value is None else f"{value:.1f}"


def _two_lines(top: object, sub: object) -> str:
    return f"**{_text(top)}**\n{_text(sub)}" if sub else f"**{_text(top)}**"


def _money_cell(amount: float, count: int, word: str) -> str:
    return f"{compact_money(amount)}\n{plural(count, word)}"


class _Pdf(FPDF):
    def footer(self) -> None:
        self.set_y(-10)
        self.set_font("Helvetica", size=7)
        self.set_text_color(*TEXT_2)
        self.cell(0, 4, f"Page {self.page_no()}", align="R")


def _new_pdf() -> _Pdf:
    pdf = _Pdf(format="A4")
    pdf.set_margins(MARGIN, MARGIN, MARGIN)
    pdf.set_auto_page_break(True, margin=14)
    return pdf


def _band(pdf: FPDF, eyebrow: str, title: str, meta: Sequence[str]) -> None:
    pdf.add_page()
    steps = 80
    width = pdf.w / steps
    for step in range(steps):
        share = step / (steps - 1)
        left, right = (APPBAR[0], APPBAR[1]) if share < 0.55 else (APPBAR[1], APPBAR[2])
        local = share / 0.55 if share < 0.55 else (share - 0.55) / 0.45
        pdf.set_fill_color(*(round(a + (b - a) * local) for a, b in zip(left, right, strict=True)))
        pdf.rect(step * width, 0, width + 0.3, BAND_HEIGHT, style="F")
    pdf.set_text_color(*WHITE)
    pdf.set_xy(MARGIN, 8)
    pdf.set_font("Helvetica", "B", 7)
    pdf.cell(0, 4, _text(eyebrow.upper()))
    pdf.set_xy(MARGIN, 14)
    pdf.set_font("Helvetica", "B", 17)
    pdf.cell(0, 8, _text(title))
    pdf.set_font("Helvetica", size=8)
    for index, line in enumerate(meta):
        pdf.set_xy(MARGIN, 8 + index * 4.5)
        pdf.cell(0, 4, _text(line), align="R")
    pdf.set_fill_color(*WHITE)
    pdf.set_y(BAND_HEIGHT + 7)


def _paragraph(pdf: FPDF, text: str, *, size: float = 10, muted: bool = False) -> None:
    pdf.set_font("Helvetica", size=size)
    pdf.set_text_color(*(TEXT_2 if muted else TEXT_1))
    pdf.multi_cell(0, size * 0.5, _text(text), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)


def _heading(pdf: FPDF, title: str, count: str = "") -> None:
    if pdf.will_page_break(28):
        pdf.add_page()
    pdf.ln(3)
    pdf.set_font("Helvetica", "B", 10.5)
    pdf.set_text_color(*TEXT_1)
    pdf.cell(pdf.get_string_width(_text(title)) + 3, 6, _text(title))
    pdf.set_font("Helvetica", size=8)
    pdf.set_text_color(*TEXT_2)
    pdf.cell(0, 6, _text(count), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(1)


def _stats(pdf: FPDF, stats: Sequence[tuple[str, str, str]]) -> None:
    width = (pdf.w - 2 * MARGIN) / len(stats)
    top = pdf.get_y()
    pdf.set_draw_color(*LINE)
    pdf.line(MARGIN, top, pdf.w - MARGIN, top)
    for index, (number, label, sub) in enumerate(stats):
        left = MARGIN + index * width
        pdf.set_xy(left, top + 2.5)
        pdf.set_font("Helvetica", "B", 15)
        pdf.set_text_color(*TEXT_1)
        pdf.cell(width, 7, _text(number))
        pdf.set_xy(left, top + 10)
        pdf.set_font("Helvetica", size=7.5)
        pdf.set_text_color(*TEXT_2)
        pdf.cell(width, 4, _text(label))
        pdf.set_xy(left, top + 14)
        pdf.set_text_color(*TEXT_1)
        pdf.cell(width, 4, _text(sub))
    pdf.line(MARGIN, top + 20.5, pdf.w - MARGIN, top + 20.5)
    pdf.set_xy(MARGIN, top + 23)


def _table(
    pdf: FPDF,
    headings: Sequence[str],
    rows: Sequence[Sequence[object]],
    widths: Sequence[float],
    right_from: int,
    *,
    total_last: bool = False,
) -> None:
    pdf.set_font("Helvetica", size=8)
    pdf.set_text_color(*TEXT_1)
    pdf.set_draw_color(*LINE)
    aligns = ["LEFT" if index < right_from else "RIGHT" for index in range(len(headings))]
    with pdf.table(
        col_widths=tuple(widths),
        text_align=tuple(aligns),
        borders_layout="HORIZONTAL_LINES",
        headings_style=FontFace(emphasis="BOLD", color=TEXT_2, size_pt=6.5),
        line_height=4,
        padding=(1.4, 1, 1.4, 0.5),
        markdown=True,
    ) as table:
        table.row([_text(heading.upper()) for heading in headings])
        for number, cells in enumerate(rows, start=1):
            bold = total_last and number == len(rows)
            row = table.row()
            for cell in cells:
                value = cell if isinstance(cell, str) and "**" in cell else _text(cell)
                row.cell(f"**{value}**" if bold and value and "**" not in value else value)
    pdf.ln(2)


def _more(pdf: FPDF, hidden: int) -> None:
    if hidden > 0:
        _paragraph(pdf, f"and {hidden} more.", size=8, muted=True)


def _money_row(label: str, sub: str, line: MoneyLine) -> list[str]:
    return [
        _two_lines(label, sub),
        _money_cell(line.money_in, line.deposits, "deposit"),
        _money_cell(line.money_out, line.withdrawals, "withdrawal"),
        _net(line.net),
    ]


def _advisor_lede(report: AdvisorReport) -> str:
    if not report.flagged:
        return "Nothing flagged this week."
    missed = report.not_reached
    withdrew = sum(1 for client in missed if client.withdrawn_kes > 0)
    text = (
        f"You had {plural(len(report.flagged), 'flagged client')} this week and reached "
        f"{len(report.reached)} of them."
    )
    if missed:
        text += f" {plural(len(missed))} had no reach-out at all"
        text += f", and {withdrew} of those made a withdrawal." if withdrew else "."
    return text


def _advisor_stats(report: AdvisorReport) -> list[tuple[str, str, str]]:
    flagged = len(report.flagged)
    before = report.previous
    last_pct = f" - last week {before.reached_pct}%" if before and before.flagged else ""
    last_days = f" - last week {_days(before.median_days)}" if before else ""
    at_risk = sum(client.at_risk_kes for client in report.not_reached)
    return [
        (
            str(flagged),
            "Flagged clients",
            f"{report.new_flags} new - {flagged - report.new_flags} carried over",
        ),
        (
            f"{len(report.reached)} of {flagged}",
            "Reached",
            f"{_pct(len(report.reached), flagged)}{last_pct}",
        ),
        (
            str(len(report.tried)),
            "Tried, no answer",
            plural(sum(client.attempts for client in report.tried), "attempt"),
        ),
        (str(len(report.not_reached)), "No reach-out", f"{compact_money(at_risk)} at risk"),
        (_days(report.median_days), "Days, flag to first contact", f"median{last_days}"),
    ]


def _flag_columns(client) -> list[str]:
    return [
        _two_lines(client.name, client.detail),
        client.reason,
        f"{client.risk_score} - {client.risk_band}",
        compact_money(client.at_risk_kes),
    ]


def _advisor_sheet(pdf: FPDF, report: AdvisorReport) -> None:
    _band(
        pdf,
        "Weekly follow-up report - Active clients",
        report.advisor.name,
        (
            f"Week: {_week(report.week_start, report.week_end)}",
            f"Active book: {plural(report.book_clients)}",
        ),
    )
    _paragraph(pdf, _advisor_lede(report))
    if report.flagged:
        _stats(pdf, _advisor_stats(report))

    _heading(pdf, "Money in and out", "your whole book, this week")
    money_rows = [
        _money_row("Flagged, reached", plural(report.money_reached.clients), report.money_reached),
        _money_row(
            "Flagged, not reached",
            plural(report.money_not_reached.clients),
            report.money_not_reached,
        ),
        _money_row("Rest of your book", plural(report.money_rest.clients), report.money_rest),
        _money_row("Whole book", "", report.money_total),
    ]
    if report.previous is not None:
        money_rows.append(_money_row("Last week", "", report.previous.money))
    _table(pdf, ("Clients", "Money in", "Money out", "Net"), money_rows, (70, 40, 40, 32), 1)

    missed = report.not_reached
    if missed:
        _heading(pdf, "No reach-out yet", plural(len(missed)))
        _table(
            pdf,
            ("Client", "Why flagged", "Risk score", "KSh at risk", "Days open", "On record"),
            [
                [*_flag_columns(client), client.days_open, client.on_record]
                for client in missed[:ROWS_SHOWN]
            ],
            (44, 46, 24, 22, 14, 32),
            3,
        )
        _more(pdf, len(missed) - ROWS_SHOWN)

    if report.tried:
        _heading(pdf, "Tried, not reached", plural(len(report.tried)))
        _table(
            pdf,
            ("Client", "Why flagged", "Risk score", "KSh at risk", "Attempts and last note"),
            [[*_flag_columns(client), client.on_record] for client in report.tried[:ROWS_SHOWN]],
            (44, 46, 24, 22, 46),
            3,
        )
        _more(pdf, len(report.tried) - ROWS_SHOWN)

    if report.logged:
        _heading(pdf, "What you logged", plural(report.contacts, "contact"))
        _table(
            pdf,
            ("Type", "Total", "Connected", "Follow-up", "No answer"),
            [
                [
                    line.channel.capitalize(),
                    line.total,
                    line.connected,
                    line.follow_up,
                    line.no_answer,
                ]
                for line in report.logged
            ],
            (62, 30, 30, 30, 30),
            1,
        )
    if report.contacts or report.snoozed or report.dismissed or report.passed_on:
        _paragraph(
            pdf,
            f"Also logged: {report.snoozed} snoozed, {report.dismissed} dismissed, "
            f"{report.passed_on} passed to the account manager. {report.contacts_with_note} of "
            f"{plural(report.contacts, 'contact')} had a note.",
            size=8,
            muted=True,
        )

    if report.follow_ups:
        _heading(pdf, "Follow-ups you set", f"{len(report.follow_ups)} open")
        _table(
            pdf,
            ("Client", "Planned", "Due"),
            [
                [
                    f"**{_text(item.name)}**",
                    item.title,
                    f"Overdue {plural(item.overdue_days, 'day')}"
                    if item.overdue_days
                    else day_label(item.due_date),
                ]
                for item in report.follow_ups[:FOLLOW_UPS_SHOWN]
            ],
            (60, 82, 40),
            3,
        )
        _more(pdf, len(report.follow_ups) - FOLLOW_UPS_SHOWN)

    after = report.after_contact
    if after.clients:
        _heading(pdf, "After your contact", f"{plural(after.clients)} reached in the past 4 weeks")
        _stats(
            pdf,
            [
                (str(after.deposited), "Deposited again", compact_money(after.deposited_kes)),
                (str(after.no_change), "No change", "no deposit or withdrawal"),
                (str(after.withdrew), "Withdrew further", compact_money(after.withdrew_kes)),
            ],
        )
    _paragraph(pdf, COUNTED_NOTE, size=7.5, muted=True)


def _sum_money(lines: Sequence[MoneyLine]) -> MoneyLine:
    total = MoneyLine()
    for line in lines:
        total.add(line)
    return total


def _team_sheet(pdf: FPDF, reports: Sequence[AdvisorReport]) -> None:
    first = reports[0]
    flagged = sum(len(r.flagged) for r in reports)
    reached = sum(len(r.reached) for r in reports)
    missed = [(client, r.advisor.name) for r in reports for client in r.not_reached]
    withdrew = [(client, name) for client, name in missed if client.withdrawn_kes > 0]
    before_flagged = sum(r.previous.flagged for r in reports if r.previous)
    before_reached = sum(r.previous.reached for r in reports if r.previous)
    _band(
        pdf,
        "Weekly follow-up report - Active clients - Team",
        "Advisor team summary",
        (
            f"Week: {_week(first.week_start, first.week_end)}",
            f"Team: {plural(len(reports), 'advisor')} - "
            f"{plural(sum(r.book_clients for r in reports))}",
        ),
    )
    if flagged:
        _paragraph(
            pdf,
            f"The team had {plural(flagged, 'flagged client')} this week and reached {reached} "
            f"of them. {len(missed)} had no reach-out at all, and {len(withdrew)} of those made "
            "a withdrawal.",
        )
    else:
        _paragraph(pdf, "Nothing flagged this week.")
    _stats(
        pdf,
        [
            (str(flagged), "Flagged clients", f"{sum(r.new_flags for r in reports)} new"),
            (
                _pct(reached, flagged),
                "Reached",
                f"{reached} of {flagged} - last week {_pct(before_reached, before_flagged)}",
            ),
            (
                str(len(missed)),
                "No reach-out",
                f"{compact_money(sum(c.at_risk_kes for c, _ in missed))} at risk",
            ),
            (
                str(len(withdrew)),
                "Withdrew, no reach-out",
                f"{compact_money(sum(c.withdrawn_kes for c, _ in withdrew))} withdrawn",
            ),
        ],
    )

    ordered = sorted(reports, key=lambda r: (-len(r.not_reached), r.advisor.name))
    _heading(pdf, "Advisor by advisor")
    rows = [
        [
            f"**{_text(r.advisor.name)}**",
            len(r.flagged),
            f"{len(r.reached)} - {_pct(len(r.reached), len(r.flagged))}" if r.flagged else "-",
            len(r.tried),
            len(r.not_reached),
            sum(1 for client in r.not_reached if client.withdrawn_kes > 0),
            _days(r.median_days),
            _pct(r.contacts_with_note, r.contacts),
            r.overdue_follow_ups,
        ]
        for r in ordered
    ]
    rows.append(
        [
            "Team",
            flagged,
            f"{reached} - {_pct(reached, flagged)}",
            sum(len(r.tried) for r in reports),
            len(missed),
            len(withdrew),
            "",
            _pct(sum(r.contacts_with_note for r in reports), sum(r.contacts for r in reports)),
            sum(r.overdue_follow_ups for r in reports),
        ]
    )
    _table(
        pdf,
        (
            "Advisor",
            "Flagged",
            "Reached",
            "Tried, no answer",
            "No reach-out",
            "of which withdrew",
            "Days to first contact",
            "Contacts with a note",
            "Overdue follow-ups",
        ),
        rows,
        (42, 15, 21, 17, 17, 17, 19, 17, 17),
        1,
        total_last=True,
    )

    _heading(pdf, "Money in and out, by advisor", "whole book, this week")
    money_rows = [
        [
            f"**{_text(r.advisor.name)}**",
            _money_cell(r.money_total.money_in, r.money_total.deposits, "deposit"),
            _money_cell(r.money_total.money_out, r.money_total.withdrawals, "withdrawal"),
            _net(r.money_total.net),
            _net(r.previous.money.net) if r.previous else "-",
            _money_cell(
                r.money_not_reached.money_out, r.money_not_reached.withdrawals, "withdrawal"
            ),
        ]
        for r in ordered
    ]
    team_money = _sum_money([r.money_total for r in reports])
    team_missed = _sum_money([r.money_not_reached for r in reports])
    money_rows.append(
        [
            "Team",
            _money_cell(team_money.money_in, team_money.deposits, "deposit"),
            _money_cell(team_money.money_out, team_money.withdrawals, "withdrawal"),
            _net(team_money.net),
            _net(_sum_money([r.previous.money for r in reports if r.previous]).net),
            _money_cell(team_missed.money_out, team_missed.withdrawals, "withdrawal"),
        ]
    )
    _table(
        pdf,
        ("Advisor", "Money in", "Money out", "Net", "Net last week", "Out, flagged not reached"),
        money_rows,
        (42, 30, 30, 26, 26, 28),
        1,
        total_last=True,
    )

    if withdrew:
        withdrew.sort(key=lambda pair: -pair[0].withdrawn_kes)
        _heading(pdf, "Withdrew, and nobody reached out", plural(len(withdrew)))
        _table(
            pdf,
            (
                "Client",
                "Advisor",
                "Withdrawal",
                "Risk score",
                "KSh at risk",
                "Days open",
                "On record",
            ),
            [
                [
                    _two_lines(client.name, client.detail),
                    name,
                    client.reason,
                    f"{client.risk_score} - {client.risk_band}",
                    compact_money(client.at_risk_kes),
                    client.days_open,
                    client.on_record,
                ]
                for client, name in withdrew[:ROWS_SHOWN]
            ],
            (38, 28, 36, 22, 20, 12, 26),
            4,
        )
        _more(pdf, len(withdrew) - ROWS_SHOWN)

    stale = [
        (r.advisor.name, [c for c in r.not_reached if c.flagged_on < r.week_start]) for r in ordered
    ]
    stale = [(name, clients) for name, clients in stale if clients]
    if stale:
        _heading(pdf, "Still open from earlier weeks", "flagged before this week, no reach-out")
        _table(
            pdf,
            ("Advisor", "Clients", "Longest wait"),
            [
                [name, len(clients), plural(max(c.days_open for c in clients), "day")]
                for name, clients in stale
            ],
            (90, 46, 46),
            1,
        )

    _heading(pdf, "How well things were logged")
    contacts = sum(r.contacts for r in reports)
    _table(
        pdf,
        ("Check", "This week"),
        [
            ["Contacts logged", contacts],
            ["Contacts with no note", contacts - sum(r.contacts_with_note for r in reports)],
            ["Snoozed", sum(r.snoozed for r in reports)],
            ["Dismissed", sum(r.dismissed for r in reports)],
            ["Passed to the account manager", sum(r.passed_on for r in reports)],
        ],
        (136, 46),
        1,
    )
    _paragraph(pdf, COUNTED_NOTE, size=7.5, muted=True)


def render_advisor_report(report: AdvisorReport) -> bytes:
    pdf = _new_pdf()
    _advisor_sheet(pdf, report)
    return bytes(pdf.output())


def render_lead_report(reports: Sequence[AdvisorReport]) -> bytes:
    pdf = _new_pdf()
    _team_sheet(pdf, reports)
    for report in sorted(reports, key=lambda r: r.advisor.name):
        _advisor_sheet(pdf, report)
    return bytes(pdf.output())
