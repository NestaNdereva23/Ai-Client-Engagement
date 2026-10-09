from __future__ import annotations

from sqlalchemy.orm import Session

from app.agents.run_report import build_run_report
from app.schemas.agent_reports import RunReportOut
from app.services.agent_runs import get_agent_run


def get_run_report(session: Session, run_id: int) -> RunReportOut:
    return build_run_report(session, get_agent_run(session, run_id))
