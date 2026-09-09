"""What one agent run's model calls cost, at the rate in force that day."""

from __future__ import annotations

from datetime import date

from sqlalchemy.orm import Session

from app.campaigns.generation_cost import (
    GenerationCostConfigMissing,
    UnknownGenerationModel,
    active_generation_cost_config,
)


def run_cost_kes(session: Session, model: str, as_of: date, call_count: int) -> float | None:
    """The cost of a run's model calls, or None when the model has no priced rate."""
    if call_count <= 0:
        return None
    try:
        config = active_generation_cost_config(session, model, as_of)
    except (UnknownGenerationModel, GenerationCostConfigMissing):
        return None
    return call_count * config.cost_per_generation_kes
