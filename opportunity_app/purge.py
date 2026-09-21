"""Delete expired opportunities from the product database.

Expired means retired (``active=0``) or past a deadline stored from the source
posting. Opportunities a student has an application on are kept: the
``applications`` foreign key has no cascade on purpose, and that history
outlives the posting. Everything else hanging off an opportunity (sources,
attributes, fit scores, saves, captures) cascades or is unlinked by the schema.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pipeline import backup_sqlite

from . import DEFAULT_PLATFORM_DB
from .database import is_postgres_target
from .schema import connect_product

_CHUNK = 500


def purge_expired_opportunities(
    conn, *, today: str | None = None, dry_run: bool = False, backup: bool = True
) -> dict[str, Any]:
    today = today or datetime.now(timezone.utc).date().isoformat()
    rows = conn.execute(
        """
        SELECT o.id, o.active, EXISTS(SELECT 1 FROM applications a WHERE a.opportunity_id = o.id)
        FROM opportunities o
        WHERE o.active = 0 OR (o.deadline_at IS NOT NULL AND SUBSTR(o.deadline_at, 1, 10) < ?)
        """,
        (today,),
    ).fetchall()
    doomed = [str(row[0]) for row in rows if not row[2]]
    result = {
        "retired": sum(1 for row in rows if not row[1]),
        "past_deadline": sum(1 for row in rows if row[1]),
        "kept_for_applications": len(rows) - len(doomed),
        "deleted": 0,
        "dry_run": dry_run,
        "backup": None,
    }
    if dry_run or not doomed:
        return result
    if backup:
        # Deletion is permanent, so snapshot first; a failed backup raises and
        # nothing is deleted. PostgreSQL has no file to copy here, so it must
        # be backed up with the operations tooling and purged with backup=False.
        if not isinstance(conn, sqlite3.Connection):
            raise RuntimeError(
                "Automatic purge backups need SQLite. Back up PostgreSQL first, then pass --no-backup."
            )
        snapshot = backup_sqlite(conn, "platform")
        result["backup"] = str(snapshot) if snapshot else None
    with conn:
        for start in range(0, len(doomed), _CHUNK):
            chunk = doomed[start : start + _CHUNK]
            placeholders = ",".join("?" for _ in chunk)
            # duplicate_of references opportunities without a cascade, so a
            # surviving duplicate must be detached before its canonical goes.
            conn.execute(
                f"UPDATE opportunities SET duplicate_of = NULL WHERE duplicate_of IN ({placeholders})",
                chunk,
            )
            conn.execute(f"DELETE FROM opportunities WHERE id IN ({placeholders})", chunk)
    result["deleted"] = len(doomed)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DEFAULT_PLATFORM_DB), help="SQLite path or PostgreSQL URL")
    parser.add_argument("--dry-run", action="store_true", help="Report what would be deleted without deleting")
    parser.add_argument(
        "--no-backup",
        action="store_true",
        help="Skip the pre-purge snapshot (required for PostgreSQL, which must be backed up separately)",
    )
    args = parser.parse_args()
    target = args.db if is_postgres_target(args.db) else Path(args.db)
    with closing(connect_product(target)) as conn:
        result = purge_expired_opportunities(conn, dry_run=args.dry_run, backup=not args.no_backup)
        print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
