from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

import structlog
from sqlalchemy.orm import Session

from app.agents.events import NO_EVENTS, EventLog, RunEventLog
from app.agents.progress import StepProgress
from app.briefing.email_options import OptionsRejected, check_angle
from app.config import get_settings
from app.db.models.agent_event import ERROR, RUN_COMPLETED, RUN_STARTED, RUN_STATUS
from app.db.models.agent_run import AgentRun
from app.db.models.chat import ChatSession, ChatTurn
from app.db.session import SessionLocal
from app.llmops.operations import trace_operation
from app.llmops.trace_names import TAG_ASSISTANT, assistant_turn_name
from app.llmops.tracing import Tracer
from app.privacy.llm_client import LLMClient, get_briefing_llm_client
from app.schemas.assistant_blocks import (
    DRAFT_EMAIL,
    INFO,
    REVIEW_MESSAGE,
    Block,
    chips_block,
    notice_block,
    options_block,
    parse_action_token,
    result_block,
    steps_block,
    suggestions_block,
    text_block,
)
from app.schemas.assistant_blocks import (
    ERROR as ERROR_TONE,
)
from app.schemas.assistant_blocks import (
    WARNING as WARNING_TONE,
)
from app.services.briefing import (
    BriefingNotFound,
    NarrativeDisabled,
    get_briefing,
    get_narrative_briefing,
)
from app.services.client_assistant import (
    DEEPER_BRIEF,
    EMAIL_OPTIONS,
    FREE,
    QUICK_EMAIL,
    starters_for_follow_up,
)
from app.services.client_email_draft import (
    DailyDraftLimitReached,
    EmailDraftBlocked,
    EmailDraftDisabled,
    EmailDraftFailed,
    draft_email_for_client,
)
from app.services.email_options import (
    EmailOptionsFailed,
    OptionNotFound,
    find_chosen_option,
    suggest_options_for_client,
)

logger = structlog.get_logger(__name__)

BLOCK_READY = "block_ready"

OPTIONS_STEPS_SUMMARY = "Used the verified brief and past interactions"
BRIEF_STEPS_SUMMARY = "Used the verified brief"
DRAFT_STEPS_SUMMARY = "Draft written and saved"
QUICK_STEPS_SUMMARY = "Used the verified brief and past interactions, then wrote the draft"
WRITE_ACTION_LABEL = "Write this email"
OPEN_REVIEW_LABEL = "Open in Review Queue"

MSG_EMAIL_OFF = "AI email drafts are switched off in this environment."
MSG_BLOCKED = "No draft for this client. They have an open complaint or asked not to be contacted."
MSG_LIMIT = "You have used today's email drafts."
MSG_FAILED = "The draft could not be made. Try again."
MSG_OPTIONS_FAILED = "No email options came back. Try again."
MSG_NOT_FOUND = "This client is not in the active book."
MSG_OPTION_GONE = "That option is no longer available. Ask for the options again."
MSG_NARRATION_OFF = "AI narration is switched off in this environment. This is the standard brief."
MSG_NARRATION_FAILED = "No AI brief came back. This is the standard brief."
MSG_ADVISORY = "Advisory only. This is not sent to the client."
MSG_FREE_INTRO = "I can prepare these for this client. Pick one to start."
MSG_BROKEN = "Something went wrong while working on this. Try again."


@dataclass
class TurnContext:
    session: Session
    turn: ChatTurn
    client_id: int
    unit_fund_id: int
    username: str
    on_behalf_of: str | None
    sees_every_book: bool
    events: EventLog = NO_EVENTS
    steps: StepProgress = field(default_factory=StepProgress)
    llm_factory: Callable[[], LLMClient] = lambda: get_briefing_llm_client(get_settings())

    def emit(self, block: Block) -> None:
        index = len(self.turn.blocks)
        self.turn.blocks = [*self.turn.blocks, block]
        self.session.commit()
        self.events.record(RUN_STATUS, stage=BLOCK_READY, index=index, type=block["type"])

    def emit_steps(self, summary: str) -> None:
        if self.steps.labels:
            self.emit(steps_block(self.steps.labels, summary))

    def notice(self, text: str, tone: str = WARNING_TONE) -> None:
        self.session.rollback()
        self.emit(notice_block(text, tone))

    def follow_ups(self, skip_intent: str | None = None) -> None:
        starters = starters_for_follow_up(
            self.session, self.client_id, self.unit_fund_id, skip_intent=skip_intent
        )
        if starters:
            self.emit(suggestions_block(starters))


EMAIL_FAILURES = (
    (EmailDraftDisabled, MSG_EMAIL_OFF, INFO),
    (EmailDraftBlocked, MSG_BLOCKED, WARNING_TONE),
    (BriefingNotFound, MSG_NOT_FOUND, WARNING_TONE),
    (DailyDraftLimitReached, MSG_LIMIT, WARNING_TONE),
    (EmailDraftFailed, MSG_FAILED, ERROR_TONE),
    (EmailOptionsFailed, MSG_OPTIONS_FAILED, ERROR_TONE),
    (OptionsRejected, MSG_OPTIONS_FAILED, ERROR_TONE),
)
EMAIL_FAILURE_TYPES = tuple(failure[0] for failure in EMAIL_FAILURES)


def _report_email_failure(ctx: TurnContext, exc: Exception) -> None:
    for kind, text, tone in EMAIL_FAILURES:
        if isinstance(exc, kind):
            ctx.notice(text, tone)
            return
    raise exc


def _suggest(ctx: TurnContext):
    return suggest_options_for_client(
        ctx.session,
        ctx.client_id,
        ctx.unit_fund_id,
        username=ctx.username,
        llm_client=ctx.llm_factory(),
        steps=ctx.steps,
    )


def _run_email_options(ctx: TurnContext) -> None:
    try:
        options = _suggest(ctx)
    except EMAIL_FAILURE_TYPES as exc:
        return _report_email_failure(ctx, exc)
    top = next(option for option in options if option.recommended)
    ctx.emit_steps(OPTIONS_STEPS_SUMMARY)
    ctx.emit(
        text_block(
            f"{len(options)} kinds of email fit this client. The top pick is {top.title}. {top.why}"
        )
    )
    ctx.emit(options_block(options, turn_id=ctx.turn.turn_id, action_label=WRITE_ACTION_LABEL))


def _draft(ctx: TurnContext, angle: str):
    return draft_email_for_client(
        ctx.session,
        ctx.client_id,
        ctx.unit_fund_id,
        username=ctx.username,
        on_behalf_of=ctx.on_behalf_of,
        llm_client=ctx.llm_factory(),
        angle=angle,
        steps=ctx.steps,
    )


def _emit_draft_result(ctx: TurnContext, title: str, draft) -> None:
    ctx.emit(
        result_block(
            "Draft ready",
            "Waiting for your review. Nothing has been sent.",
            [
                ("Kind of email", title),
                ("Subject", draft.subject),
                ("Drafts left today", str(draft.drafts_left_today)),
            ],
            next_kind=REVIEW_MESSAGE,
            next_ref=draft.message_id,
            next_label=OPEN_REVIEW_LABEL,
        )
    )


def _run_quick_email(ctx: TurnContext) -> None:
    try:
        options = _suggest(ctx)
        top = next(option for option in options if option.recommended)
        draft = _draft(ctx, check_angle(top.angle))
    except EMAIL_FAILURE_TYPES as exc:
        return _report_email_failure(ctx, exc)
    ctx.emit_steps(QUICK_STEPS_SUMMARY)
    ctx.emit(text_block(f"I went with {top.title}. {top.why}"))
    _emit_draft_result(ctx, top.title, draft)


def _narrative_view(ctx: TurnContext):
    if not get_settings().ai_briefing_enabled:
        return None, MSG_NARRATION_OFF
    try:
        with ctx.steps.step("Writing the summary"):
            view = get_narrative_briefing(
                ctx.session,
                ctx.client_id,
                ctx.unit_fund_id,
                viewer_id=ctx.username,
                on_behalf_of=ctx.on_behalf_of,
                llm_client=ctx.llm_factory(),
            )
    except NarrativeDisabled:
        return None, MSG_NARRATION_OFF
    return view, None if view.mode == "narrative" else MSG_NARRATION_FAILED


def _run_deeper_brief(ctx: TurnContext) -> None:
    try:
        view, notice = _narrative_view(ctx)
        if view is None:
            with ctx.steps.step("Reading the verified brief"):
                view = get_briefing(
                    ctx.session,
                    ctx.client_id,
                    ctx.unit_fund_id,
                    viewer_id=ctx.username,
                    on_behalf_of=ctx.on_behalf_of,
                )
    except BriefingNotFound:
        return ctx.notice(MSG_NOT_FOUND)
    ctx.emit_steps(BRIEF_STEPS_SUMMARY)
    layout = "paragraphs" if view.mode == "narrative" else "preformatted"
    ctx.emit(text_block(view.text, title="Deeper brief", layout=layout))
    if view.basis:
        ctx.emit(chips_block(view.basis))
    ctx.emit(notice_block(notice or MSG_ADVISORY, WARNING_TONE if notice else INFO))
    ctx.follow_ups(skip_intent=DEEPER_BRIEF)


def _run_draft_email(ctx: TurnContext) -> None:
    try:
        _kind, source_turn_id, option_id = parse_action_token(ctx.turn.action_ref or "")
        chosen = find_chosen_option(
            ctx.session,
            client_id=ctx.client_id,
            unit_fund_id=ctx.unit_fund_id,
            turn_id=source_turn_id,
            option_id=option_id,
            username=ctx.username,
            sees_every_book=ctx.sees_every_book,
        )
    except (ValueError, OptionNotFound):
        return ctx.notice(MSG_OPTION_GONE)
    try:
        draft = _draft(ctx, chosen.angle)
    except EMAIL_FAILURE_TYPES as exc:
        return _report_email_failure(ctx, exc)
    ctx.emit_steps(DRAFT_STEPS_SUMMARY)
    _emit_draft_result(ctx, chosen.title, draft)


def _run_free(ctx: TurnContext) -> None:
    ctx.emit(text_block(MSG_FREE_INTRO))
    ctx.follow_ups()


INTENT_HANDLERS: dict[str, Callable[[TurnContext], None]] = {
    QUICK_EMAIL: _run_quick_email,
    EMAIL_OPTIONS: _run_email_options,
    DEEPER_BRIEF: _run_deeper_brief,
    DRAFT_EMAIL: _run_draft_email,
    FREE: _run_free,
}


def _finish_run(session: Session, run_id: int, state: str, reason: str | None = None) -> None:
    session.rollback()
    run = session.get(AgentRun, run_id)
    run.state = state
    run.failure_reason = reason
    run.finished_at = datetime.now(UTC)
    session.commit()


def run_assistant_turn(
    turn_id: int,
    *,
    username: str,
    on_behalf_of: str | None,
    sees_every_book: bool,
    llm_factory: Callable[[], LLMClient] | None = None,
    tracer: Tracer | None = None,
) -> None:
    with SessionLocal() as session:
        turn = session.get(ChatTurn, turn_id)
        if turn is None:
            return
        chat_session = session.get(ChatSession, turn.session_id)
        run_id = turn.run_id
        events = RunEventLog(run_id)
        ctx = TurnContext(
            session=session,
            turn=turn,
            client_id=chat_session.client_id,
            unit_fund_id=chat_session.unit_fund_id,
            username=username,
            on_behalf_of=on_behalf_of,
            sees_every_book=sees_every_book,
            events=events,
            steps=StepProgress(events),
        )
        if llm_factory is not None:
            ctx.llm_factory = llm_factory
        events.record(RUN_STARTED, agent="assistant", trigger="chat", intent=turn.intent)
        state, reason = "completed", None
        try:
            with trace_operation(
                assistant_turn_name(turn.intent),
                tracer=tracer,
                as_type="agent",
                input={"intent": turn.intent or FREE},
                metadata={"turn_id": turn_id, "run_id": run_id},
                tags=[TAG_ASSISTANT],
                user_id=username,
                session_id=f"assistant-chat-{turn.session_id}",
            ):
                INTENT_HANDLERS[turn.intent or FREE](ctx)
        except Exception as exc:
            logger.exception("client_assistant.turn_failed", turn_id=turn_id, run_id=run_id)
            state, reason = "failed", type(exc).__name__
            events.record(ERROR, about="run", reason=reason)
            try:
                ctx.notice(MSG_BROKEN, ERROR_TONE)
            except Exception:
                logger.exception("client_assistant.notice_failed", turn_id=turn_id)
        try:
            _finish_run(session, run_id, state, reason)
        finally:
            events.record(RUN_COMPLETED, state=state)
            events.close()
