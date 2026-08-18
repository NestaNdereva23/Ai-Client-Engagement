"""Persist a built digest and audit its generation.

Called once at the end of a successful RiskDetectionWorker run, inside that
run's own transaction, so a digest and the risk_run it was built from either
land together or not at all.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.audit.log import record_audit
from app.db.models.digest import DigestLine, DigestRun
from app.digest.build import DigestBuildResult, build_digest
from app.ingestion.fa_assignment_source import FaAssignmentSource


def build_and_persist_digest(
    session: Session,
    risk_run_id: str,
    *,
    fa_assignment_source: FaAssignmentSource,
    cap_per_group: int,
) -> DigestRun:
    """Build the digest for one completed risk run and write it to
    digest_run/digest_line. Returns the new DigestRun row.
    """
    result = build_digest(
        session, risk_run_id, fa_assignment_source=fa_assignment_source, cap_per_group=cap_per_group
    )

    digest_run = DigestRun(risk_run_id=risk_run_id)
    session.add(digest_run)
    session.flush()

    session.add_all(
        DigestLine(
            digest_run_id=digest_run.digest_run_id,
            group_key=line.group_key,
            group_total=result.groups[line.group_key].total_eligible,
            group_fund_value_total=result.groups[line.group_key].total_fund_at_risk,
            rank=line.rank,
            client_id=line.client_id,
            unit_fund_id=line.unit_fund_id,
            risk_score=line.risk_score,
            risk_band=line.risk_band,
            risk_reasons=line.risk_reasons,
            risk_reason_tags=line.risk_reason_tags,
            fund_at_risk=line.fund_at_risk,
            score_delta=line.score_delta,
            route=line.route,
            in_call_queue=line.in_call_queue,
            complaint_caveat=line.complaint_caveat,
        )
        for line in result.lines
    )
    session.flush()

    record_audit(
        session,
        entity_type="digest_run",
        action="generate",
        entity_id=str(digest_run.digest_run_id),
        run_id=risk_run_id,
        detail={
            "groups": len(result.groups),
            "lines": len(result.lines),
        },
    )
    return digest_run


__all__ = ["build_and_persist_digest", "DigestBuildResult"]
