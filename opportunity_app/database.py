"""Small DB-API compatibility layer for SQLite locally and PostgreSQL when hosted."""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from typing import Any


def is_postgres_target(target: Any) -> bool:
    return str(target).startswith(("postgresql://", "postgres://"))


class HybridRow(dict):
    """Mapping row with sqlite.Row-compatible numeric lookup."""

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, int):
            return list(self.values())[key]
        return super().__getitem__(key)


def _postgres_sql(sql: str) -> str:
    translated = sql.replace(" COLLATE NOCASE", "")
    was_ignore = "INSERT OR IGNORE INTO" in translated.upper()
    translated = translated.replace("INSERT OR IGNORE INTO", "INSERT INTO")
    if was_ignore and "ON CONFLICT" not in translated.upper():
        translated = translated.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"
    return translated.replace("?", "%s")


class PostgresCursor:
    def __init__(self, cursor: Any):
        self._cursor = cursor

    @property
    def rowcount(self) -> int:
        return self._cursor.rowcount

    def fetchone(self) -> HybridRow | None:
        row = self._cursor.fetchone()
        return None if row is None else HybridRow(row)

    def fetchall(self) -> list[HybridRow]:
        return [HybridRow(row) for row in self._cursor.fetchall()]

    def __iter__(self) -> Iterator[HybridRow]:
        return (HybridRow(row) for row in self._cursor)


def _postgres_schema(sql: str) -> str:
    schema = re.sub(r"^PRAGMA[^;]+;\s*", "", sql, flags=re.MULTILINE)
    schema = schema.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "BIGSERIAL PRIMARY KEY")
    schema = schema.replace(
        """SELECT source.*
    FROM opportunity_sources source
    WHERE source.rowid = (
        SELECT MIN(candidate.rowid)
        FROM opportunity_sources candidate
        WHERE candidate.opportunity_id = source.opportunity_id
    )""",
        """SELECT DISTINCT ON (source.opportunity_id) source.*
    FROM opportunity_sources source
    ORDER BY source.opportunity_id, source.source_key, source.external_id""",
    )
    return schema


class PostgresConnection:
    """Expose the subset of sqlite's connection API used by product modules."""

    backend = "postgresql"

    def __init__(self, url: str, *, read_only: bool = False):
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:  # pragma: no cover - deployment dependency
            raise RuntimeError("PostgreSQL requires `pip install psycopg[binary]`") from exc
        self._conn = psycopg.connect(url, row_factory=dict_row)
        if read_only:
            self._conn.execute("SET TRANSACTION READ ONLY")

    def execute(self, sql: str, params: Any = ()) -> PostgresCursor:
        return PostgresCursor(self._conn.execute(_postgres_sql(sql), params))

    def executemany(self, sql: str, params: Any) -> PostgresCursor:
        cursor = self._conn.cursor()
        cursor.executemany(_postgres_sql(sql), params)
        return PostgresCursor(cursor)

    def executescript(self, sql: str) -> None:
        self._conn.execute(_postgres_schema(sql), prepare=False)

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "PostgresConnection":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if exc_type is None:
            self.commit()
        else:
            self.rollback()
