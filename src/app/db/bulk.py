"""Bulk load helpers built on Postgres COPY.

COPY parses far faster than multi-row INSERTs, so these pay off when writing
thousands of rows at once. Callers own the transaction: nothing here commits.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any
from uuid import uuid4

from sqlalchemy.orm import Session


def quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _driver_connection(session: Session):
    return session.connection().connection.driver_connection


def _write_rows(copy, columns: list[str], rows: list[dict[str, Any]]) -> None:
    for row in rows:
        copy.write_row([row.get(c) for c in columns])


def copy_insert(session: Session, table: str, rows: list[dict[str, Any]]) -> int:
    """Append rows to a table through COPY. For append-only tables with no conflict."""
    if not rows:
        return 0
    columns = list(rows[0].keys())
    col_sql = ", ".join(quote_ident(c) for c in columns)
    raw = _driver_connection(session)
    with raw.cursor() as cur:
        with cur.copy(f"COPY {quote_ident(table)} ({col_sql}) FROM STDIN") as copy:
            _write_rows(copy, columns, rows)
    return len(rows)


def copy_into_new_temp_table(
    cur, table: str, columns: dict[str, str], rows: Iterable[Sequence[Any]]
) -> None:
    col_defs = ", ".join(f"{quote_ident(name)} {sqltype}" for name, sqltype in columns.items())
    cur.execute(f"CREATE TEMP TABLE {quote_ident(table)} ({col_defs}) ON COMMIT DROP")
    col_sql = ", ".join(quote_ident(name) for name in columns)
    with cur.copy(f"COPY {quote_ident(table)} ({col_sql}) FROM STDIN") as copy:
        for row in rows:
            copy.write_row(list(row))


def copy_upsert(
    session: Session,
    table: str,
    rows: list[dict[str, Any]],
    index_elements: list[str],
    set_clause: str,
) -> int:
    """COPY rows into a temp table, then upsert into the target in one statement.

    `set_clause` is the raw SQL after DO UPDATE SET, referencing EXCLUDED and the
    target table by name. A unique temp name lets several upserts share one
    transaction without clashing.
    """
    if not rows:
        return 0
    columns = list(rows[0].keys())
    col_sql = ", ".join(quote_ident(c) for c in columns)
    conflict_sql = ", ".join(quote_ident(c) for c in index_elements)
    temp = quote_ident(f"_copy_{table}_{uuid4().hex[:8]}")
    raw = _driver_connection(session)
    with raw.cursor() as cur:
        cur.execute(
            f"CREATE TEMP TABLE {temp} (LIKE {quote_ident(table)} INCLUDING DEFAULTS) "
            "ON COMMIT DROP"
        )
        with cur.copy(f"COPY {temp} ({col_sql}) FROM STDIN") as copy:
            _write_rows(copy, columns, rows)
        cur.execute(
            f"INSERT INTO {quote_ident(table)} ({col_sql}) "
            f"SELECT {col_sql} FROM {temp} "
            f"ON CONFLICT ({conflict_sql}) DO UPDATE SET {set_clause}"
        )
    return len(rows)
