from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from app.agents.events import NO_EVENTS, EventLog
from app.db.models.agent_event import STEP_COMPLETED, STEP_STARTED


class StepProgress:
    def __init__(self, events: EventLog = NO_EVENTS) -> None:
        self.events = events
        self.labels: list[str] = []

    @contextmanager
    def step(self, label: str) -> Iterator[None]:
        index = len(self.labels)
        self.labels.append(label)
        self.events.record(STEP_STARTED, label=label, index=index)
        yield
        self.events.record(STEP_COMPLETED, label=label, index=index)
