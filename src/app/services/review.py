"""Server-side re-attachment: turn an accepted generation run into an
outreach_message, injecting the client's real name and fund name into the
draft's placeholders.

This runs entirely after generation and guardrails; nothing here calls the
model, so the resolved values are never seen by it. The only read of
pii_vault happens under the restricted role, scoped to that one lookup, in
its own session so the rest of this module never even holds a connection
with a grant on the vault.
"""

from __future__ import annotations

import uuid

import structlog
from sqlalchemy.orm import Session

from app.audit.log import record_audit
from app.db.models.llmops import GenerationRun
from app.db.models.models import Clients, Funds, PiiVault
from app.db.models.outreach import OutreachMessage
from app.db.session import restricted_session

logger = structlog.get_logger(__name__)

_FALLBACK_FIRST_NAME = "Valued Client"
_FALLBACK_FUND_NAME = "your fund"


def _first_name_from_full_name(full_name: str | None) -> str:
    """The greeting name from a stored full name, or a safe fallback when
    there is nothing on file; a real client should never receive a blank or
    malformed greeting.
    """
    if not full_name or not full_name.strip():
        return _FALLBACK_FIRST_NAME
    return full_name.strip().split()[0]


def _fetch_client_name(client_id: int) -> str | None:
    """The vault's raw client_name for one client, read under the restricted role."""
    with restricted_session() as session:
        vault = session.get(PiiVault, client_id)
        record_audit(
            session,
            entity_type="pii_vault",
            action="read",
            entity_id=str(client_id),
            detail={"purpose": "personalization_inject"},
        )
        session.commit()
        return vault.client_name if vault else None


def resolve_placeholders(text: str, *, first_name: str, fund_name: str) -> str:
    """Substitute the two placeholders EmailAgent is allowed to use."""
    return text.replace("{{first_name}}", first_name).replace("{{fund_name}}", fund_name)


def personalize_content(ai_draft_content: dict, *, first_name: str, fund_name: str) -> dict:
    """The subject/body pair with real values injected in place of placeholders."""
    return {
        "subject": resolve_placeholders(
            ai_draft_content["subject"], first_name=first_name, fund_name=fund_name
        ),
        "body": resolve_placeholders(
            ai_draft_content["body"], first_name=first_name, fund_name=fund_name
        ),
    }


def create_outreach_message(
    session: Session, run: GenerationRun, *, campaign_id: int
) -> OutreachMessage:
    """Re-attach real values to an accepted run and store the result.

    ai_draft_content is copied from the run unchanged; personalized_content
    is computed fresh here. run must be accepted (has ai_draft_content); a
    run with nothing else to review has no message to create.
    """
    client = session.get(Clients, run.client_id)
    fund = session.get(Funds, client.unit_fund_id) if client else None
    fund_name = fund.unit_fund_name if fund else _FALLBACK_FUND_NAME

    full_name = _fetch_client_name(run.client_id)
    first_name = _first_name_from_full_name(full_name)
    if full_name is None:
        logger.warning("personalization.no_client_name", client_id=run.client_id)

    personalized = personalize_content(
        run.ai_draft_content, first_name=first_name, fund_name=fund_name
    )

    message = OutreachMessage(
        message_id=uuid.uuid4().hex,
        campaign_id=campaign_id,
        generation_run_id=run.run_id,
        client_id=run.client_id,
        ai_draft_content=run.ai_draft_content,
        personalized_content=personalized,
    )
    session.add(message)
    record_audit(
        session,
        entity_type="outreach_message",
        action="create",
        entity_id=message.message_id,
        run_id=run.run_id,
        trace_id=run.trace_id,
    )
    session.flush()
    return message
