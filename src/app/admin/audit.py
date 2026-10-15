"""Record every change made in the admin screen in the audit log.

The admin screen has one shared login, so the actor is that account. Only the
access table can be changed there, which makes this the trail of who was given
or lost access, and of who an admin was set to view as.
"""

from __future__ import annotations

import json
from typing import Any

from sqladmin.audit import AuditEntry, DBAuditBackend
from starlette.requests import Request

from app.config import get_settings
from app.db.models.audit import AuditLog

ENTITY_TYPE = "admin_screen"


class AdminScreenAudit(DBAuditBackend):
    async def get_actor(self, request: Request) -> str:
        return get_settings().admin_username

    def build_row(self, entry: AuditEntry, actor: Any, request: Request) -> AuditLog:
        changes = json.loads(json.dumps(entry.changes, default=str)) if entry.changes else None
        return AuditLog(
            entity_type=ENTITY_TYPE,
            entity_id=f"{entry.identity}:{entry.pk}",
            action=entry.action,
            actor_id=actor,
            detail={"changes": changes} if changes else None,
        )
