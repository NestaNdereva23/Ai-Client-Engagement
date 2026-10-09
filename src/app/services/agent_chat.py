from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.chat import ChatSessionNotFound
from app.db.models.agent_run import AgentRun
from app.db.models.chat import ChatSession, ChatTurn
from app.pagination import DEFAULT_LIMIT, clamp_limit, decode_id_cursor, encode_id_cursor
from app.schemas.agent_chat import ChatSessionOut, ChatTurnOut


def get_chat_session(session: Session, session_id: int) -> ChatSessionOut:
    row = session.get(ChatSession, session_id)
    if row is None:
        raise ChatSessionNotFound(session_id)
    return ChatSessionOut(
        session_id=row.session_id,
        started_at=row.started_at,
        title=row.title,
        turns=_turns_for(session, session_id),
    )


def _turns_for(session: Session, session_id: int) -> list[ChatTurnOut]:
    rows = session.execute(
        select(ChatTurn, AgentRun.state)
        .join(AgentRun, AgentRun.run_id == ChatTurn.run_id)
        .where(ChatTurn.session_id == session_id)
        .order_by(ChatTurn.turn_id.asc())
    ).all()
    return [
        ChatTurnOut(
            turn_id=turn.turn_id,
            session_id=turn.session_id,
            run_id=turn.run_id,
            question=turn.question,
            answer=turn.answer,
            created_at=turn.created_at,
            run_state=run_state,
        )
        for turn, run_state in rows
    ]


def list_chat_sessions(
    session: Session, *, cursor: str | None = None, limit: int = DEFAULT_LIMIT
) -> tuple[list[ChatSession], str | None]:
    limit = clamp_limit(limit)
    query = select(ChatSession)
    if cursor is not None:
        query = query.where(ChatSession.session_id < decode_id_cursor(cursor))
    query = query.order_by(ChatSession.session_id.desc()).limit(limit + 1)
    rows = list(session.scalars(query).all())
    if len(rows) > limit:
        rows = rows[:limit]
        return rows, encode_id_cursor(rows[-1].session_id)
    return rows, None
