"""Small DB-API compatibility layer for SQLite locally and PostgreSQL when hosted."""

from __future__ import annotations

import logging
import re
import sqlite3
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

from .. import DEFAULT_LEGACY_DB, DEFAULT_PLATFORM_DB


def is_postgres_target(target: Any) -> bool:
    return str(target).startswith(("postgresql://", "postgres://"))


def is_unique_violation(exc: BaseException) -> bool:
    """A duplicate key, as SQLite or PostgreSQL reports it."""
    if isinstance(exc, sqlite3.IntegrityError) and "UNIQUE" in str(exc).upper():
        return True
    if getattr(exc, "sqlstate", None) == "23505" or getattr(exc, "pgcode", None) == "23505":
        return True
    return type(exc).__name__ == "UniqueViolation"


def rollback_quietly(conn: Any, logger: logging.Logger, what: str) -> None:
    """Roll back a transaction left open after a step failed; if even that fails, log it and go on.

    ``what`` completes the warning, "Could not roll back after <what>", and
    ``logger`` is the caller's own, so its warnings stay under its module's name.
    Only a transaction that is open is rolled back, so a connection with none is
    left alone. This never raises, which is the point: it runs in an ``except``
    block, or between steps that must each get their turn.
    """
    try:
        if getattr(conn, "in_transaction", False):
            conn.rollback()
    except Exception:  # noqa: BLE001 - the caller is already handling a failure
        logger.warning("Could not roll back after %s", what, exc_info=True)


# PostgreSQL error classes that mean "try again": a lost connection (08), a serialization failure or
# deadlock (40), and a lock not available or a statement cancelled while waiting (55P03, 57014).
_TRANSIENT_SQLSTATES = ("08", "40", "55P03", "57014")


def is_transient_error(exc: BaseException) -> bool:
    """A database that could not answer just now (locked, busy, a deadlock, a dropped connection), not a bad request.

    On SQLite that is any OperationalError ("database is locked" after the
    busy timeout, among others); on PostgreSQL, psycopg's OperationalError and
    the error classes in _TRANSIENT_SQLSTATES.
    """
    if isinstance(exc, sqlite3.OperationalError):
        return True
    state = str(getattr(exc, "sqlstate", None) or getattr(exc, "pgcode", None) or "")
    if state and state.startswith(_TRANSIENT_SQLSTATES):
        return True
    return type(exc).__name__ in {"OperationalError", "SerializationFailure", "DeadlockDetected", "TransactionRollbackError",
                                  "LockNotAvailable", "QueryCanceled"}


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
            # The connection-level flag makes every later transaction read-only too; a
            # SET TRANSACTION covers only the first, and a commit would end it. It has
            # to be set before any statement opens a transaction.
            self._conn.read_only = True
            self._conn.execute("SET TRANSACTION READ ONLY")

    def execute(self, sql: str, params: Any = ()) -> PostgresCursor:
        return PostgresCursor(self._conn.execute(_postgres_sql(sql), params))

    def executemany(self, sql: str, params: Any) -> PostgresCursor:
        cursor = self._conn.cursor()
        cursor.executemany(_postgres_sql(sql), params)
        return PostgresCursor(cursor)

    def executescript(self, sql: str) -> None:
        self._conn.execute(_postgres_schema(sql), prepare=False)

    @property
    def in_transaction(self) -> bool:
        """Like sqlite3.Connection.in_transaction: a transaction is open and not yet committed.

        psycopg opens one on the first statement, a read included, so this is
        true after any query until commit or rollback.
        """
        from psycopg.pq import TransactionStatus

        return self._conn.info.transaction_status != TransactionStatus.IDLE

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


# Every request opens its own connection, so anything done per connection is
# paid per request. Resolving the path costs two filesystem syscalls (~0.22ms)
# and re-declaring WAL costs ~0.87ms; neither answer changes per connection.
_RESOLVED_PATHS: dict[str, Path] = {}


def _resolved_db_path(path: Path) -> Path:
    """Resolve once per absolute spelling.

    Only absolute paths are cached. A relative path resolves against the working
    directory, so caching one by its spelling would keep answering with the old
    database after a chdir -- a wrong-database bug in exchange for two syscalls.
    """

    if not path.is_absolute():
        return path.expanduser().resolve()
    key = str(path)
    resolved = _RESOLVED_PATHS.get(key)
    if resolved is None:
        resolved = path.expanduser().resolve()
        _RESOLVED_PATHS[key] = resolved
    return resolved


def connect_product(path: Path | str = DEFAULT_PLATFORM_DB, *, read_only: bool = False) -> sqlite3.Connection | PostgresConnection:
    if is_postgres_target(path):
        return PostgresConnection(str(path), read_only=read_only)
    path = _resolved_db_path(path)
    if read_only:
        conn = sqlite3.connect(
            f"file:{path.as_posix()}?mode=ro",
            uri=True,
            check_same_thread=False,
        )
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        # FastAPI may resolve a synchronous dependency in a worker thread and
        # hand it to an async upload endpoint on the event-loop thread. Each
        # request still gets its own connection; disabling only the thread
        # affinity check is therefore safe and avoids cross-request sharing.
        conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if not read_only:
        # WAL is a persistent property of the database file, not of a
        # connection, so declaring it on every writable connection redid work
        # that already survived in the file. Asking what the mode is costs
        # ~0.025ms; switching to it costs ~0.88ms. Reading first is therefore
        # ~35x cheaper on the common path and, unlike remembering which files
        # we have already declared, stays correct when a database is deleted
        # and recreated at the same path.
        if str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower() != "wal":
            conn.execute("PRAGMA journal_mode = WAL")
    return conn


def connect_legacy_read_only(path: Path = DEFAULT_LEGACY_DB) -> sqlite3.Connection:
    path = path.expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(f"Legacy pipeline database not found: {path}")
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    """Backend-agnostic column check. PRAGMA is SQLite-only."""

    if getattr(conn, "backend", "sqlite") == "postgresql":
        row = conn.execute(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_schema=current_schema() AND table_name=? AND column_name=?",
            (table, column),
        ).fetchone()
        return row is not None
    return any(
        str(row["name"]) == column
        for row in conn.execute(f"PRAGMA table_info({table})")
    )
