from __future__ import annotations

from dataclasses import dataclass

from sqladmin import BaseView, expose
from sqlalchemy import text
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import Response

from app.db.session import SessionLocal

BYTES_PER_MB = 1024 * 1024

_TABLE_SIZES_SQL = text(
    """
    SELECT
        c.relname AS table_name,
        CASE
            WHEN c.reltuples < 0 AND COALESCE(s.n_live_tup, 0) = 0 THEN NULL
            ELSE GREATEST(c.reltuples, COALESCE(s.n_live_tup, 0))::bigint
        END AS approx_rows,
        pg_table_size(c.oid) AS table_bytes,
        pg_indexes_size(c.oid) AS index_bytes,
        pg_total_relation_size(c.oid) AS total_bytes
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    LEFT JOIN pg_stat_user_tables s ON s.relid = c.oid
    WHERE c.relkind = 'r' AND n.nspname = current_schema()
    ORDER BY total_bytes DESC, table_name
    """
)


@dataclass(frozen=True)
class TableSize:
    name: str
    approx_rows: int | None
    table_mb: float
    index_mb: float
    total_mb: float


def _to_mb(size_bytes: int) -> float:
    return round(size_bytes / BYTES_PER_MB, 2)


def load_table_sizes() -> list[TableSize]:
    with SessionLocal() as session:
        rows = session.execute(_TABLE_SIZES_SQL).all()
    return [
        TableSize(
            name=row.table_name,
            approx_rows=row.approx_rows,
            table_mb=_to_mb(row.table_bytes),
            index_mb=_to_mb(row.index_bytes),
            total_mb=_to_mb(row.total_bytes),
        )
        for row in rows
    ]


class TableSizesView(BaseView):
    name = "Table Sizes"
    icon = "fa-solid fa-hard-drive"
    category = "Database"
    category_icon = "fa-solid fa-server"

    @expose("/table-sizes", methods=["GET"], identity="table-sizes")
    async def table_sizes(self, request: Request) -> Response:
        sizes = await run_in_threadpool(load_table_sizes)
        total_mb = round(sum(size.total_mb for size in sizes), 2)
        return await self.templates.TemplateResponse(
            request,
            "table_sizes.html",
            {
                "title": "Table Sizes",
                "subtitle": "Database",
                "sizes": sizes,
                "total_mb": total_mb,
            },
        )
