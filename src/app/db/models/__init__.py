"""SQLAlchemy models, one module per domain area.

Importing this package registers every model on ``Base.metadata`` so migrations
and ``create_all`` see them.
"""

from app.db.models.models import (
    INGESTION_STATES,
    IngestionReject,
    IngestionStatus,
    RawStaging,
)

__all__ = [
    "INGESTION_STATES",
    "IngestionReject",
    "IngestionStatus",
    "RawStaging",
]
