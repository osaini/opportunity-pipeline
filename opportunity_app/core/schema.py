"""SQLite product schema: the migration runner and its Python steps.

ensure_product_schema applies migrations/*.sql in order. The steps that SQL alone cannot
express (guarded column adds, backfills) are Python functions keyed by migration name. The
connection factory lives in core/database.py and the legacy-data sync in opportunities/legacy_sync.py.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Callable

from pipeline_core.identity import sort_key

from .. import ROOT
from .company_tags import ensure_company_tags_current
from .database import has_column
from .timestamps import canonical_utc, utc_now


MIGRATIONS_DIR = ROOT / "migrations"
LOCAL_USER_ID = "local-user"
# The updated_at of an 'automation_paused' row that was made 'off' and never
# flipped. That timestamp means "when the pause last started or ended", so a
# row that merely came into being must not look like a resume that happened
# just now (outreach_schedule would then say a late send was held by a pause).
PAUSE_NEVER_CHANGED = "1970-01-01T00:00:00+00:00"


def backfill_posted_at_utc(conn: sqlite3.Connection) -> int:
    """Derive posted_at_utc from the raw posted_at for every row.

    Deterministic and therefore safe to repeat: it recomputes from the
    untouched source string rather than advancing any state, so a crash
    part-way through costs nothing but the work. That is what makes the
    migration restart-safe without a separate completion marker -- a NULL
    result is indistinguishable from "not yet done", so "done" is not
    something this can record.
    """

    rows = conn.execute("SELECT id, posted_at FROM opportunities").fetchall()
    updates = [
        (canonical_utc(row["posted_at"]), str(row["id"]))
        for row in rows
    ]
    if updates:
        conn.executemany(
            "UPDATE opportunities SET posted_at_utc=? WHERE id=?", updates
        )
    return sum(1 for value, _ in updates if value is not None)


def _repair_observation_timestamps(conn: sqlite3.Connection) -> int:
    """Prove -- not assume -- that the other ranking timestamps sort correctly.

    `first_seen_at` (the `newest` and `discovered` sorts) and `last_seen_at`
    (the `score` tie-break) are written by this project, and both writers emit
    `+00:00`: pipeline.now_iso() at second precision and utc_now() at
    microsecond precision. Mixing those two spellings happens to sort correctly
    as text, because a missing fraction sorts before any fraction and means
    `.000000`. A `Z` suffix would not, so any row that is not already canonical
    or safely `+00:00` is rewritten rather than reported.
    """

    repaired = 0
    for column in ("first_seen_at", "last_seen_at"):
        rows = conn.execute(
            f"SELECT id, {column} AS value FROM opportunities WHERE {column} IS NOT NULL"
        ).fetchall()
        fixes = [
            (canonical_utc(row["value"]), str(row["id"]))
            for row in rows
            if not str(row["value"]).endswith("+00:00")
        ]
        fixes = [(value, row_id) for value, row_id in fixes if value is not None]
        if fixes:
            conn.executemany(
                f"UPDATE opportunities SET {column}=? WHERE id=?", fixes
            )
            repaired += len(fixes)
    return repaired


def _apply_posted_at_utc(conn: sqlite3.Connection, sql: str) -> None:
    # ALTER TABLE ADD COLUMN is not idempotent and DDL commits on its own in
    # SQLite, so a crash between the column and the migration marker would make
    # the next start fail on a duplicate column. Guarding the add, recreating
    # the view with IF EXISTS, and recomputing the backfill are each repeatable,
    # which makes the whole step repeatable.
    if not has_column(conn, "opportunities", "posted_at_utc"):
        conn.execute("ALTER TABLE opportunities ADD COLUMN posted_at_utc TEXT")
    conn.executescript(sql)
    backfill_posted_at_utc(conn)
    _repair_observation_timestamps(conn)


def backfill_sort_keys(conn: sqlite3.Connection) -> int:
    """Derive company_sort_key/title_sort_key. Idempotent by recomputation."""

    rows = conn.execute("SELECT id, company, title FROM opportunities").fetchall()
    updates = [
        (sort_key(row["company"]), sort_key(row["title"]), str(row["id"]))
        for row in rows
    ]
    if updates:
        conn.executemany(
            "UPDATE opportunities SET company_sort_key=?, title_sort_key=? WHERE id=?",
            updates,
        )
    return len(updates)


def _apply_company_sort_keys(conn: sqlite3.Connection, sql: str) -> None:
    for column in ("company_sort_key", "title_sort_key"):
        if not has_column(conn, "opportunities", column):
            conn.execute(f"ALTER TABLE opportunities ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")
    conn.executescript(sql)
    backfill_sort_keys(conn)


def _add_columns(conn: sqlite3.Connection, columns: tuple[tuple[str, str, str], ...]) -> None:
    """ALTER TABLE ... ADD COLUMN for each (table, column, definition) the table does not have yet.

    ADD COLUMN is not idempotent and DDL commits on its own in SQLite, so a crash between a column and
    the migration marker would make the next start fail on a duplicate column. Guarding each add makes
    the step repeatable: running it again after a crash repairs it.
    """
    for table, column, definition in columns:
        if not has_column(conn, table, column):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _columns_step(columns: tuple[tuple[str, str, str], ...]) -> Callable[[Any, str], None]:
    """A migration step that adds the guarded columns, then runs the migration's SQL (every statement IF NOT EXISTS)."""

    def step(conn: sqlite3.Connection, sql: str) -> None:
        _add_columns(conn, columns)
        conn.executescript(sql)

    return step


# Columns the automation ledger needs on existing tables: who made an
# interaction or a task, and how the Gmail connection is doing.
_AUTOMATION_COLUMNS = (
    ("opportunity_interactions", "source", "TEXT NOT NULL DEFAULT 'user'"),
    ("application_tasks", "origin", "TEXT NOT NULL DEFAULT 'user'"),
    ("application_tasks", "origin_ref", "TEXT NOT NULL DEFAULT ''"),
    ("connector_accounts", "last_ok_at", "TEXT"),
    ("connector_accounts", "last_error", "TEXT NOT NULL DEFAULT ''"),
    ("connector_accounts", "token_granted_at", "TEXT"),
    ("connector_accounts", "backoff_until", "TEXT"),
)


def _apply_automation(conn: sqlite3.Connection, sql: str) -> None:
    # The same reasoning as _apply_posted_at_utc: every column is guarded, the
    # tables are IF NOT EXISTS, and the seed skips rows that exist, so a crash
    # anywhere before the migration marker is repaired by running it again.
    _add_columns(conn, _AUTOMATION_COLUMNS)
    conn.executescript(sql)
    # Every student starts unpaused, with the row in place: pausing is then an
    # UPDATE that takes the row's lock, which the hand-over to Gmail waits on.
    # Nobody paused, so the row carries no pause time (PAUSE_NEVER_CHANGED).
    users = [(str(row[0]), PAUSE_NEVER_CHANGED) for row in conn.execute("SELECT id FROM users").fetchall()]
    if users:
        conn.executemany(
            "INSERT INTO user_settings(user_id, key, value, updated_at) VALUES(?, 'automation_paused', 'off', ?) "
            "ON CONFLICT(user_id, key) DO NOTHING",
            users,
        )


# Columns application mail needs on existing tables: the assessment or
# scheduling link a task opens (shown in the app only), and who decided a
# monitored email ('system' when automation acted on it).
_APPLICATION_MAIL_COLUMNS = (
    ("application_tasks", "link", "TEXT NOT NULL DEFAULT ''"),
    ("monitored_events", "decided_by", "TEXT NOT NULL DEFAULT ''"),
)

_apply_application_mail = _columns_step(_APPLICATION_MAIL_COLUMNS)


# The student's own name for a résumé kept for one kind of role (student/resume_variants.py).
_INTERNAL_AUTOMATION_COLUMNS = (
    ("resume_files", "variant_label", "TEXT NOT NULL DEFAULT ''"),
)

_apply_internal_automation = _columns_step(_INTERNAL_AUTOMATION_COLUMNS)


# What an outreach event records beside its text (outreach.log_event's ``data``): a
# reply read from Gmail keeps its ids, its sender, and both readings of it.
_DECLINE_THANK_YOU_COLUMNS = (
    ("outreach_events", "detail_json", "TEXT NOT NULL DEFAULT '{}'"),
)

_apply_decline_thank_you = _columns_step(_DECLINE_THANK_YOU_COLUMNS)


# What a message outreach read keeps beyond its kind: how it was matched to a
# company (via) and under which version of the rules (rules), why it was set
# aside or is only a possible reply, the other companies that could have sent
# it, where it sits in Gmail, and, for a reply or a possible reply, its subject,
# its Message-ID and sender's name (to answer it in its thread), and while a
# possible reply waits, its words (outreach/inbox.py).
_OUTREACH_REPLY_RULES_COLUMNS = (
    ("outreach_inbox_messages", "via", "TEXT NOT NULL DEFAULT ''"),
    ("outreach_inbox_messages", "rules", "INTEGER NOT NULL DEFAULT 0"),
    ("outreach_inbox_messages", "candidates_json", "TEXT NOT NULL DEFAULT '[]'"),
    ("outreach_inbox_messages", "thread_id", "TEXT NOT NULL DEFAULT ''"),
    ("outreach_inbox_messages", "message_id", "TEXT NOT NULL DEFAULT ''"),
    ("outreach_inbox_messages", "from_name", "TEXT NOT NULL DEFAULT ''"),
    ("outreach_inbox_messages", "subject", "TEXT NOT NULL DEFAULT ''"),
    ("outreach_inbox_messages", "text", "TEXT NOT NULL DEFAULT ''"),
    ("outreach_inbox_messages", "reason", "TEXT NOT NULL DEFAULT ''"),
    ("outreach_inbox_messages", "in_spam", "INTEGER NOT NULL DEFAULT 0"),
    ("outreach_inbox_messages", "decided_at", "TEXT"),
    ("outreach_inbox_messages", "meta_json", "TEXT NOT NULL DEFAULT '{}'"),
)

_apply_outreach_reply_rules = _columns_step(_OUTREACH_REPLY_RULES_COLUMNS)


# Research for call prep: the company from the web (outreach/research.py), and the interviewer.
_TECH_BRIEF_COLUMNS = (
    ("outreach_targets", "tech_brief_json", "TEXT NOT NULL DEFAULT '{}'"),
    ("outreach_targets", "tech_brief_at", "TEXT"),
    ("outreach_targets", "tech_brief_by", "TEXT NOT NULL DEFAULT ''"),
    ("outreach_targets", "tech_brief_error", "TEXT NOT NULL DEFAULT ''"),
    ("outreach_targets", "tech_brief_tried_at", "TEXT"),
    ("outreach_targets", "tech_brief_job_id", "TEXT"),
    # Who the call is with and notes from their LinkedIn (outreach/interviewer.py);
    # the student's own entry for who it is, and their profile link.
    ("outreach_targets", "interviewer_json", "TEXT NOT NULL DEFAULT '{}'"),
    ("outreach_targets", "interviewer_at", "TEXT"),
    ("outreach_targets", "interviewer_error", "TEXT NOT NULL DEFAULT ''"),
    ("outreach_targets", "interviewer_tried_at", "TEXT"),
    ("outreach_targets", "interviewer_name", "TEXT NOT NULL DEFAULT ''"),
    ("outreach_targets", "interviewer_linkedin", "TEXT NOT NULL DEFAULT ''"),
)

_apply_tech_brief = _columns_step(_TECH_BRIEF_COLUMNS)


# The Gmail label a reply carries (outreach/labels.py), and which account the
# Gmail connection signed into.
_GMAIL_REPLY_LABELS_COLUMNS = (
    ("outreach_inbox_messages", "label_name", "TEXT NOT NULL DEFAULT ''"),
    ("outreach_inbox_messages", "labeled_at", "TEXT"),
    ("outreach_inbox_messages", "label_note", "TEXT NOT NULL DEFAULT ''"),
    ("connector_accounts", "account_email", "TEXT NOT NULL DEFAULT ''"),
)

_apply_gmail_reply_labels = _columns_step(_GMAIL_REPLY_LABELS_COLUMNS)


# Apply for me (apply/runs.py): whether a job email's sender was vouched for, and
# the hash of the approved text a generated PDF was rendered from. The Gmail
# address the app reads is connector_accounts.account_email, added by 0043.
_APPLY_AGENT_COLUMNS = (
    ("application_mail_messages", "sender_verified", "INTEGER NOT NULL DEFAULT 0"),
    ("generated_document_artifacts", "content_sha256", "TEXT NOT NULL DEFAULT ''"),
)

_apply_apply_agent = _columns_step(_APPLY_AGENT_COLUMNS)


# The company name as the student typed it, beside the matching key of a stored sensitive answer.
_APPLY_SENSITIVE_COMPANY_NAME_COLUMNS = (
    ("apply_sensitive_answers", "company_name", "TEXT NOT NULL DEFAULT ''"),
)

_apply_apply_sensitive_company_name = _columns_step(_APPLY_SENSITIVE_COMPANY_NAME_COLUMNS)


# When the student marked an outreach company not interested; NULL while it is in play.
_OUTREACH_NOT_INTERESTED_COLUMNS = (
    ("outreach_targets", "not_interested_at", "TEXT"),
)

_apply_outreach_not_interested = _columns_step(_OUTREACH_NOT_INTERESTED_COLUMNS)


# Why an outreach company is set aside: '' not interested, 'applied_directly' the student applied on its own site.
_OUTREACH_APPLIED_DIRECTLY_COLUMNS = (
    ("outreach_targets", "set_aside_reason", "TEXT NOT NULL DEFAULT ''"),
)

_apply_outreach_applied_directly = _columns_step(_OUTREACH_APPLIED_DIRECTLY_COLUMNS)


# Which agent ran a deep search when it was not the one the student chose ('' when it ran as chosen).
_OUTREACH_DISCOVERY_AGENT_NOTE_COLUMNS = (
    ("outreach_discovery_runs", "agent_note", "TEXT NOT NULL DEFAULT ''"),
)

_apply_outreach_discovery_agent_note = _columns_step(_OUTREACH_DISCOVERY_AGENT_NOTE_COLUMNS)


# Migrations whose SQL alone cannot express the change: parsing timestamps is
# not portable across SQLite and PostgreSQL, so a Python step owns it. Adding a
# column is not repeatable, so a step owns that too.
_MIGRATION_STEPS: dict[str, Callable[[Any, str], None]] = {
    "0020_posted_at_utc.sql": _apply_posted_at_utc,
    "0021_company_sort_keys.sql": _apply_company_sort_keys,
    "0037_automation.sql": _apply_automation,
    "0038_application_mail.sql": _apply_application_mail,
    "0039_internal_automation.sql": _apply_internal_automation,
    "0040_decline_thank_you.sql": _apply_decline_thank_you,
    "0041_outreach_reply_rules.sql": _apply_outreach_reply_rules,
    "0042_outreach_tech_brief.sql": _apply_tech_brief,
    "0043_gmail_reply_labels.sql": _apply_gmail_reply_labels,
    "0045_apply_agent.sql": _apply_apply_agent,
    "0046_apply_sensitive_company_name.sql": _apply_apply_sensitive_company_name,
    "0047_outreach_not_interested.sql": _apply_outreach_not_interested,
    "0049_outreach_discovery_agent_note.sql": _apply_outreach_discovery_agent_note,
    "0050_outreach_applied_directly.sql": _apply_outreach_applied_directly,
}


def ensure_product_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            name TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL
        )
        """
    )
    applied = {
        str(row[0])
        for row in conn.execute("SELECT name FROM schema_migrations").fetchall()
    }
    for migration in sorted(MIGRATIONS_DIR.glob("[0-9][0-9][0-9][0-9]_*.sql")):
        if migration.name in applied:
            continue
        sql = migration.read_text(encoding="utf-8")
        step = _MIGRATION_STEPS.get(migration.name)
        if step is None:
            conn.executescript(sql)
        else:
            step(conn, sql)
        conn.execute(
            "INSERT INTO schema_migrations(name, applied_at) VALUES(?, ?)",
            (migration.name, utc_now()),
        )
        conn.commit()
    # Tags are derived data: backfilled on first run, and rebuilt when the
    # tagging rules change, so an edit to them needs no migration.
    ensure_company_tags_current(conn)
