"""Keeping the database small and safe: SQLite backups and the purge of expired postings."""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .store import deduplicate


# A posting with an application behind it is history the student still needs,
# so it survives the purge even after it closes. These mirror the statuses the
# product migration turns into `applications` rows.
PURGE_PROTECTED_STATUSES = {"applying", "applied", "interview", "offer", "rejected", "withdrawn"}
PURGE_BACKUPS_KEPT = 14


def backup_sqlite(conn: sqlite3.Connection, label: str, keep: int = PURGE_BACKUPS_KEPT) -> Path | None:
    """Snapshot a file-backed SQLite database before a destructive change.

    Written to a `backups/` directory beside the database with SQLite's online
    backup API, which is consistent even while another process (the dashboard)
    has the database open. Only the newest `keep` snapshots for `label` are
    retained. Returns None for an in-memory database, which has nothing to lose.
    Any failure propagates so the caller never deletes without a backup.
    """
    main = next((row for row in conn.execute("PRAGMA database_list") if row[1] == "main"), None)
    if main is None or not main[2]:
        return None
    backup_dir = Path(main[2]).parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    # Prune only snapshots this function names, never backups made by hand.
    pattern = re.compile(rf"{re.escape(label)}-(\d{{8}}T\d{{12}})Z\.db")
    stamps = sorted(match[1] for path in backup_dir.iterdir() if (match := pattern.fullmatch(path.name)))
    # Name the snapshot after the newest existing one even when the clock
    # disagrees. Two backups in one clock tick would otherwise share a name,
    # the second overwriting the first, and after the clock steps back the new
    # snapshot would sort oldest and be pruned below before the caller used it.
    taken = datetime.now(timezone.utc)
    if stamps:
        newest = datetime.strptime(stamps[-1], "%Y%m%dT%H%M%S%f").replace(tzinfo=timezone.utc)
        taken = max(taken, newest + timedelta(microseconds=1))
    target = backup_dir / f"{label}-{taken.strftime('%Y%m%dT%H%M%S%fZ')}.db"
    destination = sqlite3.connect(target)
    try:
        conn.backup(destination)
    finally:
        destination.close()
    snapshots = sorted(path for path in backup_dir.iterdir() if pattern.fullmatch(path.name))
    for stale in snapshots[:-keep] if keep > 0 else []:
        stale.unlink(missing_ok=True)
    return target


def _deadline_passed(job: sqlite3.Row, today: str) -> bool:
    # Imported lazily: pipeline.py stays runnable without the product package
    # on the path for every other command.
    from opportunity_app.opportunity_metadata import extract_deadline

    deadline = extract_deadline("\n".join((job["title"], job["location"], job["description"])))
    # The deadline day itself is still open, so only strictly earlier dates count.
    return bool(deadline) and deadline[:10] < today


def purge_expired(
    conn: sqlite3.Connection,
    today: str | None = None,
    dry_run: bool = False,
) -> dict[str, int]:
    """Delete retired postings and postings whose stated deadline has passed.

    `active=0` is set by a source batch that no longer lists the posting or by
    a liveness verdict of `expired`. Unlike those, this is a hard delete: if a
    source lists the posting again it comes back as a new row.
    """
    today = today or datetime.now(timezone.utc).date().isoformat()
    rows = conn.execute(
        "SELECT id, company, title, location, description, status, active FROM jobs"
    ).fetchall()
    tally = {"retired": 0, "past_deadline": 0, "kept": 0, "deleted": 0}
    doomed: list[str] = []
    for row in rows:
        if not row["active"]:
            reason = "retired"
        elif _deadline_passed(row, today):
            reason = "past_deadline"
        else:
            continue
        tally[reason] += 1
        if row["status"] in PURGE_PROTECTED_STATUSES:
            tally["kept"] += 1
            continue
        doomed.append(row["id"])

    if dry_run:
        print(
            f"Would delete {len(doomed)} posting(s): {tally['retired']} retired, "
            f"{tally['past_deadline']} past deadline, {tally['kept']} kept for applications"
        )
        return tally

    if doomed:
        # Deletion is permanent and a retirement can come from one bad fetch or
        # liveness verdict, so snapshot first. A failed backup raises and
        # nothing is deleted.
        conn.commit()
        backup = backup_sqlite(conn, "pipeline")
        if backup:
            print(f"Backed up to {backup}")
    for start in range(0, len(doomed), 500):
        chunk = doomed[start : start + 500]
        placeholders = ",".join("?" for _ in chunk)
        conn.execute(f"DELETE FROM jobs WHERE id IN ({placeholders})", chunk)
    tally["deleted"] = len(doomed)
    if doomed:
        # A deleted row may have been a canonical other rows pointed at.
        deduplicate(conn)
    conn.commit()
    print(
        f"Deleted {tally['deleted']} posting(s): {tally['retired']} retired, "
        f"{tally['past_deadline']} past deadline, {tally['kept']} kept for applications"
    )
    return tally
