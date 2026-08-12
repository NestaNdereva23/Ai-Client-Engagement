"""The nightly risk detection worker.

Structured like workers/ingestion.py: opens a run, works through its stages
in order, and can resume a run that stopped partway. Marks itself failed on
an unhandled exception rather than leaving a run stuck at "running".

Stages, in order:
    1. Ingest the active-clients feed into raw_staging (reuses IngestionWorker,
       keyed by this run's own run_id).
    2. Transform into active_client_fund and derive the behavioural measures.
    3. Fetch open complaints and FA assignments for this run's population.
    4. Evaluate the six signals and compose a score for every client-fund.
    5. Route.
    6. Write one risk_snapshot row per client-fund and upsert
       client_risk_features to the latest values.

A completed run is never redone: calling run() again with the same run_id
just returns its summary. A run that failed partway resumes: ingestion picks
up from its own checkpoint, and the scoring/routing/snapshot stages recompute
deterministically and skip any snapshot row already written for this run_id,
so nothing is duplicated.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date
from typing import Any
from uuid import uuid4

import structlog
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.audit.log import record_audit
from app.config import get_settings
from app.db.models.digest import DigestRun
from app.db.models.risk import ClientRiskFeatures, RiskConfigVersion, RiskRun, RiskSnapshot
from app.db.models.suppression import Suppression
from app.db.session import SessionLocal
from app.ingestion.complaints_source import ComplaintsSource, get_complaints_source
from app.ingestion.endpoints import resolve_endpoint
from app.ingestion.fa_assignment_source import FaAssignmentSource, get_fa_assignment_source
from app.risk.history import write_snapshot
from app.risk.routing import RoutableRow, route_population
from app.risk.scoring import ScoreResult, compose_score
from app.risk.signals import SIGNAL_ORDER
from app.risk.store import active_config_version
from app.transform.active_features import ActiveFeatureMeasures, derive_active_measures
from app.transform.active_flatten import flatten_active_run
from app.transform.active_load import persist_active_result
from app.transform.load import upsert
from app.workers.digest import build_and_persist_digest
from app.workers.ingestion import IngestionWorker

logger = structlog.get_logger(__name__)

ENDPOINT = "active-clients"

_FEATURE_UPDATE_COLUMNS = [
    "recency_band",
    "balance_tier",
    "value_tier",
    "credible_rhythm",
    "lapse_ratio",
    "sig_drawdown",
    "sig_dormant",
    "sig_cadence_break",
    "sig_shrinking",
    "sig_fee_erosion",
    "sig_never_repeated",
    "risk_score",
    "risk_band",
    "risk_reasons",
    "aum_at_risk",
    "config_version",
    "route",
    "queue_rank",
]


class RiskRunAborted(RuntimeError):
    """Raised when a run cannot start at all, for example no risk config is active."""


@dataclass
class RiskRunResult:
    """What a run did, returned to the caller and logged."""

    run_id: str
    state: str
    clients_seen: int
    signals_fired: dict[str, int]
    route_distribution: dict[str, int]
    routes_changed: int | None
    digest_run_id: int | None = None


def _lapse_ratio(m: ActiveFeatureMeasures) -> float | None:
    """How many multiples of their own rhythm a client is overdue by.

    None whenever there is no credible rhythm or no purchase to measure
    against -- the same evidence gate sig_cadence_break uses.
    """
    if m.rhythm_days is None or m.rhythm_days == 0 or m.days_since_purchase is None:
        return None
    return m.days_since_purchase / m.rhythm_days


def _feature_row(
    m: ActiveFeatureMeasures,
    score: ScoreResult,
    route_str: str,
    queue_rank: int | None,
    config_version: int,
) -> dict[str, Any]:
    return {
        "client_id": m.client_id,
        "unit_fund_id": m.unit_fund_id,
        "recency_band": score.recency_band,
        "balance_tier": score.balance_tier,
        "value_tier": score.value_tier,
        "credible_rhythm": m.rhythm_days is not None,
        "lapse_ratio": _lapse_ratio(m),
        **score.signals,
        "risk_score": score.risk_score,
        "risk_band": score.risk_band,
        "risk_reasons": score.risk_reasons,
        "aum_at_risk": score.aum_at_risk,
        "config_version": config_version,
        "route": route_str,
        "queue_rank": queue_rank,
    }


def _signal_counts(scores: dict[tuple[int, int], ScoreResult]) -> dict[str, int]:
    counts = dict.fromkeys(SIGNAL_ORDER, 0)
    for score in scores.values():
        for name in SIGNAL_ORDER:
            if score.signals[name]:
                counts[name] += 1
    return counts


class RiskDetectionWorker:
    """Runs one nightly risk detection pass and can resume it by run_id.

    client is handed straight to IngestionWorker for the ingest stage; pass
    page_fetcher to drive paging in tests without real HTTP, same as
    IngestionWorker itself.
    """

    def __init__(
        self,
        client: Any,
        session_factory: sessionmaker[Session] = SessionLocal,
        *,
        complaints_source: ComplaintsSource | None = None,
        fa_assignment_source: FaAssignmentSource | None = None,
        max_pages: int = 1000,
        page_fetcher: Any = None,
    ) -> None:
        self._client = client
        self._session_factory = session_factory
        self._complaints_source = complaints_source
        self._fa_assignment_source = fa_assignment_source
        self._max_pages = max_pages
        self._page_fetcher = page_fetcher

    def run(self, run_id: str | None = None) -> RiskRunResult:
        current_run_id = run_id
        session = self._session_factory()
        try:
            run = self._open_run(session, run_id)
            current_run_id = run.run_id
            if run.state == "completed":
                logger.info("risk_detection.already_complete", run_id=run.run_id)
                return self._summarize(session, run)

            config_row = session.scalar(
                select(RiskConfigVersion).where(RiskConfigVersion.version == run.config_version)
            )
            if config_row is None:
                raise RiskRunAborted(f"config version {run.config_version} no longer exists")

            self._ingest(run)

            flat = flatten_active_run(session, run.run_id, reference_date=run.reference_ts)
            persist_active_result(session, flat, source=ENDPOINT)
            measures = derive_active_measures(
                flat,
                reference_date=run.reference_ts,
                system_sale_max=config_row.thresholds["SYSTEM_SALE_MAX"],
                fee_per_month=config_row.thresholds["FEE_PER_MONTH"],
            )
            logger.info("risk_detection.transformed", run_id=run.run_id, client_funds=len(measures))

            client_ids = sorted({m.client_id for m in measures.values()})
            complaints_source = self._complaints_source or get_complaints_source()
            complaints = complaints_source.fetch_open_complaints(client_ids)
            complaint_ids = {c.client_id for c in complaints}
            fa_source = self._fa_assignment_source or get_fa_assignment_source(session=session)
            fa_assignments = fa_source.fetch_assignments(client_ids)
            suppressed_ids = set(
                session.scalars(
                    select(Suppression.client_id).where(Suppression.client_id.in_(client_ids))
                )
            )
            logger.info(
                "risk_detection.context_fetched",
                run_id=run.run_id,
                open_complaints=len(complaint_ids),
                fa_assignments=len(fa_assignments),
                suppressed=len(suppressed_ids),
            )

            scores: dict[tuple[int, int], ScoreResult] = {}
            routable: list[RoutableRow] = []
            for key, m in measures.items():
                score = compose_score(m, config_row)
                scores[key] = score
                routable.append(
                    RoutableRow(
                        key=key,
                        balance=m.balance,
                        risk_score=score.risk_score,
                        sig_dormant=score.signals["sig_dormant"],
                        aum_at_risk=score.aum_at_risk,
                        has_open_complaint=m.client_id in complaint_ids,
                        suppressed=m.client_id in suppressed_ids,
                    )
                )
            routes = route_population(routable, config_row)
            route_distribution = Counter(r.route for r in routes.values())
            signal_counts = _signal_counts(scores)
            logger.info(
                "risk_detection.scored",
                run_id=run.run_id,
                signals_fired=signal_counts,
                route_distribution=dict(route_distribution),
            )

            prior_routes = {
                (r.client_id, r.unit_fund_id): r.route
                for r in session.scalars(
                    select(ClientRiskFeatures).where(ClientRiskFeatures.client_id.in_(client_ids))
                )
            }
            existing_snapshot_keys = set(
                session.execute(
                    select(RiskSnapshot.client_id, RiskSnapshot.unit_fund_id).where(
                        RiskSnapshot.run_id == run.run_id
                    )
                ).all()
            )

            changes: list[dict[str, Any]] = []
            feature_rows: list[dict[str, Any]] = []
            for key, m in measures.items():
                score = scores[key]
                route = routes[key]
                if key not in existing_snapshot_keys:
                    write_snapshot(
                        session,
                        run.run_id,
                        m.client_id,
                        m.unit_fund_id,
                        score,
                        route,
                        config_row.version,
                        credible_rhythm=m.rhythm_days is not None,
                        lapse_ratio=_lapse_ratio(m),
                    )
                if prior_routes.get(key) != route.route:
                    changes.append(
                        {
                            "client_id": m.client_id,
                            "unit_fund_id": m.unit_fund_id,
                            "route": route.route,
                            "risk_band": score.risk_band,
                            "reasons": score.risk_reasons,
                        }
                    )
                feature_rows.append(
                    _feature_row(m, score, route.route, route.queue_rank, config_row.version)
                )

            upsert(
                session,
                ClientRiskFeatures,
                feature_rows,
                ("client_id", "unit_fund_id"),
                _FEATURE_UPDATE_COLUMNS,
                extra_set={"updated_at": func.now()},
            )

            if changes:
                record_audit(
                    session,
                    entity_type="risk_snapshot",
                    action="route",
                    entity_id=run.run_id,
                    run_id=run.run_id,
                    detail={"changed": changes, "changed_count": len(changes)},
                )

            digest_run = build_and_persist_digest(
                session,
                run.run_id,
                fa_assignment_source=fa_source,
                cap_per_group=config_row.digest_cap_per_group,
            )
            logger.info(
                "risk_detection.digest_built",
                run_id=run.run_id,
                digest_run_id=digest_run.digest_run_id,
            )

            run.state = "completed"
            run.finished_at = func.now()
            record_audit(
                session,
                entity_type="risk_run",
                action="complete",
                entity_id=run.run_id,
                run_id=run.run_id,
                detail={
                    "clients_seen": len(measures),
                    "route_distribution": dict(route_distribution),
                    "routes_changed": len(changes),
                },
            )
            session.commit()

            result = RiskRunResult(
                run_id=run.run_id,
                state=run.state,
                clients_seen=len(measures),
                signals_fired=signal_counts,
                route_distribution=dict(route_distribution),
                routes_changed=len(changes),
                digest_run_id=digest_run.digest_run_id,
            )
            logger.info("risk_detection.completed", **result.__dict__)
            return result
        except Exception:
            session.rollback()
            self._mark_failed(current_run_id)
            raise
        finally:
            session.close()

    def _ingest(self, run: RiskRun) -> None:
        """Pull the active-clients feed into raw_staging, keyed by this run's own id."""
        config = resolve_endpoint(ENDPOINT, get_settings())
        worker = IngestionWorker(
            self._client,
            session_factory=self._session_factory,
            endpoint=ENDPOINT,
            fetch_path=config.fetch_path,
            max_pages=self._max_pages,
            page_fetcher=self._page_fetcher,
            fund_model=config.fund_model,
            client_model=config.client_model,
            schema_drift_fn=config.schema_drift_fn,
            count_field=config.count_field,
        )
        ingestion_result = worker.run(run_id=run.run_id)
        logger.info(
            "risk_detection.ingested",
            run_id=run.run_id,
            pages=ingestion_result.pages,
            records_seen=ingestion_result.records_seen,
            records_written=ingestion_result.records_written,
            records_rejected=ingestion_result.records_rejected,
            shortfall=ingestion_result.shortfall,
        )

    def _open_run(self, session: Session, run_id: str | None) -> RiskRun:
        """Load an existing run to resume, or start a new one."""
        if run_id is not None:
            run = session.get(RiskRun, run_id)
            if run is not None:
                if run.state != "completed":
                    run.state = "running"
                    session.commit()
                return run

        version = active_config_version(session, date.today())
        if version is None:
            raise RiskRunAborted("no active risk_config_version for today")

        run = RiskRun(run_id=run_id or uuid4().hex, config_version=version)
        session.add(run)
        session.flush()
        record_audit(
            session,
            entity_type="risk_run",
            action="start",
            entity_id=run.run_id,
            run_id=run.run_id,
            detail={"config_version": version},
        )
        session.commit()
        logger.info("risk_detection.started", run_id=run.run_id, config_version=version)
        return run

    def _mark_failed(self, run_id: str | None) -> None:
        if run_id is None:
            return
        with self._session_factory() as session:
            run = session.get(RiskRun, run_id)
            if run is not None and run.state != "completed":
                run.state = "failed"
                run.finished_at = func.now()
                record_audit(
                    session, entity_type="risk_run", action="fail", entity_id=run_id, run_id=run_id
                )
                session.commit()
                logger.error("risk_detection.failed", run_id=run_id)

    def _summarize(self, session: Session, run: RiskRun) -> RiskRunResult:
        rows = session.execute(
            select(RiskSnapshot.route, func.count())
            .where(RiskSnapshot.run_id == run.run_id)
            .group_by(RiskSnapshot.route)
        ).all()
        route_distribution = {route: count for route, count in rows}
        digest_run_id = session.scalar(
            select(DigestRun.digest_run_id)
            .where(DigestRun.risk_run_id == run.run_id)
            .order_by(DigestRun.digest_run_id.desc())
            .limit(1)
        )
        return RiskRunResult(
            run_id=run.run_id,
            state=run.state,
            clients_seen=sum(route_distribution.values()),
            signals_fired={},
            route_distribution=route_distribution,
            routes_changed=None,
            digest_run_id=digest_run_id,
        )
