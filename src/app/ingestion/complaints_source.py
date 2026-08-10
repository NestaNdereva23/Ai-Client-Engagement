"""The complaints source: a contract fixed now so a real source can swap in
later with no change to whoever calls it.

Cytonn has no complaints endpoint today, so StubComplaintsSource is the only
implementation: it is honest about being a stub, always answering "nothing
open". Every caller downstream -- the six-signal engine, the router, the
briefing renderer -- depends only on the ComplaintsSource protocol, never on
StubComplaintsSource directly, so the day a real complaints feed or a CRM
export exists, a new class implementing fetch_open_complaints and one line in
app/config.py is the whole change.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel

from app.config import Settings, get_settings


class ComplaintRecord(BaseModel):
    """One open or recently closed complaint against a client.

    No free-text body. The category and channel are enough for routing and
    for a briefing to say "there is an open complaint" without the system
    ever having to read or reason about what it says.
    """

    client_id: int
    opened_at: date
    closed_at: date | None = None
    status: Literal["open", "closed"]
    category: Literal["billing", "service", "product", "other"]
    channel: Literal["call", "email", "branch", "other"]
    source: str  # "stub" today; a real source name once one exists


@runtime_checkable
class ComplaintsSource(Protocol):
    def fetch_open_complaints(self, client_ids: Sequence[int]) -> list[ComplaintRecord]: ...


class StubComplaintsSource:
    """Returns no complaints, which is the honest current state: Cytonn has
    confirmed no complaints data is available.

    An optional local fixture file lets tests exercise the downstream code
    paths that depend on an open complaint before real data exists. The
    fixture is a test seam, not a production behaviour: the app's own
    factory (app.config) never passes fixture_path, so it can only ever be
    set by a test constructing this class directly.
    """

    def __init__(self, fixture_path: str | Path | None = None) -> None:
        self._records: list[ComplaintRecord] = []
        if fixture_path is not None:
            raw = json.loads(Path(fixture_path).read_text(encoding="utf-8"))
            self._records = [ComplaintRecord.model_validate(r) for r in raw]

    def fetch_open_complaints(self, client_ids: Sequence[int]) -> list[ComplaintRecord]:
        wanted = set(client_ids)
        return [r for r in self._records if r.client_id in wanted and r.status == "open"]


def get_complaints_source(settings: Settings | None = None) -> ComplaintsSource:
    """Build the configured ComplaintsSource. The one place an implementation
    is chosen; downstream code only ever depends on the protocol.
    """
    settings = settings or get_settings()
    source = settings.complaints_source
    if source == "stub":
        return StubComplaintsSource()
    raise ValueError(f"unknown complaints source: {source!r}")
