"""Read and write one student's `user_settings` rows.

`user_settings(user_id, key, value, updated_at)` holds the automation switches,
their `.on_since` and `.shadow_since` records, and a few small marks that
workers keep (when the daily archive last ran, how far the inbox sweep has
read). Every module that needed one wrote the same two statements, so they
live here, in a leaf that takes the connection and opens no transaction of
its own: the caller owns the transaction (`with conn:`), as it always did.

Only the plain get, put and updated-at read are shared. Statements that differ
stay where they are, because they are not copies:

- `automation.paused` reads with `FOR SHARE` on PostgreSQL.
- `automation._write_pause` moves `updated_at` only when the value flips, and
  `automation.ensure_pause_row` inserts with `DO NOTHING`.
- `auto_triage` and `automation_health.health_summary` read a value and its
  `updated_at` in one statement.

Standard library only; no connection is opened here.
"""

from __future__ import annotations

from typing import Any


def get_setting(conn: Any, user_id: str, key: str) -> str | None:
    """The stored value for ``key``, or None when the student has no such row."""
    row = conn.execute("SELECT value FROM user_settings WHERE user_id=? AND key=?", (user_id, key)).fetchone()
    return None if row is None else str(row[0])


def setting_updated_at(conn: Any, user_id: str, key: str) -> str | None:
    """When ``key`` was last written, as stored, or None when the student has no such row."""
    row = conn.execute("SELECT updated_at FROM user_settings WHERE user_id=? AND key=?", (user_id, key)).fetchone()
    return None if row is None else row[0]


def put_setting(conn: Any, user_id: str, key: str, value: str, stamp: str) -> None:
    """Upsert one setting, stamping ``updated_at``. Opens no transaction: the caller owns it."""
    conn.execute(
        """
        INSERT INTO user_settings(user_id, key, value, updated_at) VALUES(?, ?, ?, ?)
        ON CONFLICT(user_id, key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
        """,
        (user_id, key, value, stamp),
    )
