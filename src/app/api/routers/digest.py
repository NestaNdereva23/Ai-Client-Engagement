"""Digest endpoints: today's digest for an account manager or a fund."""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/digest", tags=["digest"])
