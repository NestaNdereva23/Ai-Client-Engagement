"""Risk endpoints: the latest snapshot and a single queue's contents."""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/risk", tags=["risk"])
