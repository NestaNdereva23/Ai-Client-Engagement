"""Briefing endpoints: one client's deterministic risk briefing."""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/briefing", tags=["briefing"])
