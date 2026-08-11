"""Ingestion worker.

Pulls the endpoint into raw_staging one page at a time, validates each record,
and can resume a run where it stopped. Raw payloads are saved before parsing, so
re-processing never calls the source again. Malformed records go to
ingestion_rejects with a reason instead of stopping the run.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import structlog
from pydantic import BaseModel, ValidationError
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, sessionmaker

from app.db.models.models import IngestionReject, IngestionStatus, RawStaging
from app.db.session import SessionLocal
from app.ingestion.contracts import ClientRecord, FundRecord, RawEnvelope, schema_drift

logger = structlog.get_logger(__name__)

# One page and the cursor to fetch the next one, or None when ther e are no more.
PageFetcher = Callable[[str | None], tuple[str, dict[str, Any]] | None]


class IngestionAborted(RuntimeError):
    """Raised when a run cannot start, for example the endpoint is not live."""


@dataclass
class IngestionResult:
    """What a run did, returned to the caller and logged."""

    run_id: str
    state: str
    pages: int
    records_seen: int
    records_written: int
    records_rejected: int
    shortfall: int


class IngestionWorker:
    """Runs one ingestion pass and can resume it by run_id.

    The client is used to check liveness and fetch pages. Pass a page_fetcher to
    drive paging in tests without real HTTP.
    """

    def __init__(
        self,
        client: Any,
        session_factory: sessionmaker[Session] = SessionLocal,
        *,
        endpoint: str = "inactive-clients",
        fetch_path: str | None = None,
        max_pages: int = 1000,
        page_fetcher: PageFetcher | None = None,
        fund_model: type[BaseModel] = FundRecord,
        client_model: type[BaseModel] = ClientRecord,
        schema_drift_fn: Callable[[dict[str, Any]], set[str]] = schema_drift,
        count_field: str = "inactive_client_count",
    ) -> None:
        self._client = client
        self._session_factory = session_factory
        self._endpoint = endpoint
        self._fetch_path = "" if fetch_path is None else fetch_path
        self._max_pages = max_pages
        self._page_fetcher = page_fetcher
        # Which contract set to validate against, and which field on the fund
        # record carries the source's own headcount for reconciliation. The
        # active-clients feed passes contracts_active's models and
        # "client_count" here; everything else about a run is identical.
        self._fund_model = fund_model
        self._client_model = client_model
        self._schema_drift_fn = schema_drift_fn
        self._count_field = count_field
        # Set once a response's meta says which page is the last one, so a
        # later call can stop without another request.
        self._known_last_page: int | None = None

    def run(self, run_id: str | None = None) -> IngestionResult:
        """Ingest all pages. Resumes if run_id names a run that did not finish."""
        if not self._client.probe(self._fetch_path):
            logger.error("ingestion.aborted", reason="endpoint not live", endpoint=self._endpoint)
            raise IngestionAborted("endpoint is not live")

        current_run_id = run_id
        session = self._session_factory()
        try:
            status = self._open_run(session, run_id)
            current_run_id = status.run_id
            if status.state == "completed":
                logger.info("ingestion.already_complete", run_id=status.run_id)
                return self._summarize(session, status)

            after = status.page_cursor
            pages = 0
            while pages < self._max_pages:
                page = self._fetch_page(after)
                if page is None:
                    break
                page_key, payload = page

                # Save the raw payload first so re-processing never re-fetches.
                self._store_raw(session, status.run_id, page_key, payload)
                session.commit()

                self._process_page(session, status, payload)
                status.page_cursor = page_key
                session.commit()

                after = page_key
                pages += 1

            status.state = "completed"
            status.finished_at = func.now()
            session.commit()
            result = self._summarize(session, status)
            logger.info("ingestion.completed", **result.__dict__)
            return result
        except Exception:
            session.rollback()
            self._mark_failed(current_run_id)
            raise
        finally:
            session.close()

    def _open_run(self, session: Session, run_id: str | None) -> IngestionStatus:
        """Load an existing run to resume, or start a new one."""
        if run_id is not None:
            status = session.get(IngestionStatus, run_id)
            if status is not None:
                if status.state != "completed":
                    status.state = "running"
                    session.commit()
                return status

        status = IngestionStatus(
            run_id=run_id or uuid4().hex,
            endpoint=self._endpoint,
            state="running",
        )
        session.add(status)
        session.commit()
        logger.info("ingestion.started", run_id=status.run_id, endpoint=self._endpoint)
        return status

    def _fetch_page(self, after: str | None) -> tuple[str, dict[str, Any]] | None:
        """Return the page after the given cursor, or None when there are no more.

        The source paginates: every response carries a meta block with
        current_page and last_page. after is the page number already
        checkpointed, so the next page asked for is after + 1, or 1 to start.
        Once a response's own meta says its page is the last one, later calls
        stop before making another request.
        """
        if self._page_fetcher is not None:
            return self._page_fetcher(after)

        next_page = 1 if after is None else int(after) + 1
        if self._known_last_page is not None and next_page > self._known_last_page:
            return None

        payload = self._client.fetch(self._fetch_path, params={"page": next_page})
        meta = payload.get("meta") or {}
        current_page = meta.get("current_page", next_page)
        self._known_last_page = meta.get("last_page", current_page)
        return str(current_page), payload

    def _store_raw(
        self, session: Session, run_id: str, page_key: str, payload: dict[str, Any]
    ) -> None:
        """Upsert the raw payload so a repeated page overwrites instead of duplicating."""
        stmt = pg_insert(RawStaging).values(
            run_id=run_id,
            endpoint=self._endpoint,
            natural_key=page_key,
            payload=payload,
        )
        stmt = stmt.on_conflict_do_update(
            constraint="uq_raw_staging_run_natural",
            set_={"payload": stmt.excluded.payload, "pulled_at": func.now()},
        )
        session.execute(stmt)

    def _process_page(
        self, session: Session, status: IngestionStatus, payload: dict[str, Any]
    ) -> None:
        """Validate one page: count records, save rejects, and note any shortfall."""
        drift = self._schema_drift_fn(payload)
        if drift:
            logger.warning("ingestion.schema_drift", keys=sorted(drift))

        env = RawEnvelope.model_validate(payload)
        for fund_raw in sorted(env.data, key=lambda f: f.get("unit_fund_id") or 0):
            try:
                fund = self._fund_model.model_validate(fund_raw)
            except ValidationError as exc:
                self._reject(session, status, fund_raw, self._reason("fund", exc))
                continue

            returned = len(fund.clients)
            expected = getattr(fund, self._count_field, None)
            if expected is not None:
                gap = expected - returned
                if gap > 0:
                    status.shortfall += gap
                    logger.info(
                        "ingestion.reconciliation",
                        fund_id=fund.unit_fund_id,
                        expected=expected,
                        returned=returned,
                        shortfall=gap,
                    )

            for client_raw in fund.clients:
                status.records_seen += 1
                try:
                    self._client_model.model_validate(client_raw)
                except ValidationError as exc:
                    self._reject(session, status, client_raw, self._reason("client", exc))
                    continue
                status.records_written += 1

            status.fund_cursor = str(fund.unit_fund_id)

    def _reject(
        self, session: Session, status: IngestionStatus, fragment: Any, reason: str
    ) -> None:
        session.add(IngestionReject(run_id=status.run_id, raw_fragment=fragment, reason=reason))
        status.records_rejected += 1

    @staticmethod
    def _reason(kind: str, exc: ValidationError) -> str:
        """Turn a validation error into a short reason string."""
        first = exc.errors()[0]
        loc = ".".join(str(part) for part in first.get("loc", ()))
        return f"{kind}: {loc} {first.get('msg', 'invalid')}".strip()

    def _mark_failed(self, run_id: str | None) -> None:
        if run_id is None:
            return
        with self._session_factory() as session:
            status = session.get(IngestionStatus, run_id)
            if status is not None and status.state != "completed":
                status.state = "failed"
                status.finished_at = func.now()
                session.commit()
                logger.error("ingestion.failed", run_id=run_id)

    def _summarize(self, session: Session, status: IngestionStatus) -> IngestionResult:
        pages = session.scalar(
            select(func.count()).select_from(RawStaging).where(RawStaging.run_id == status.run_id)
        )
        return IngestionResult(
            run_id=status.run_id,
            state=status.state,
            pages=pages or 0,
            records_seen=status.records_seen,
            records_written=status.records_written,
            records_rejected=status.records_rejected,
            shortfall=status.shortfall,
        )
