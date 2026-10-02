"""Each email goes out at most once: the send-claims ledger.

A send or a draft first inserts a row in outreach_send_claims for its target and kind; the primary key makes that the
lock. The row is released only when Gmail certainly did nothing, kept as 'sent' when Gmail confirmed the send, and kept
as 'unconfirmed' when Gmail may or may not have acted. An unconfirmed row, like a draft that vanished from Drafts, asks the
student to look in Gmail before anything else is sent, since gmail.compose cannot read Sent.

The outreach send workflow (outreach_gmail), the contact-form submitter and the thank-you recovery all hold claims through
this module; none of them needs Gmail's REST client to do it.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterator
from uuid import uuid4

from .. import SERVER_INSTANCE
from ..core.database import is_unique_violation
from ..core.timestamps import utc_now


# The server runs as one process, so a claim from another instance was left by
# a process that has since died or been replaced. Its request may still have
# been finishing its one Gmail call, hence the grace period before it counts as
# stale. SERVER_INSTANCE is one id per process (opportunity_app/__init__.py), shared with Apply for me's claims.
FOREIGN_CLAIM_GRACE = timedelta(minutes=5)
IN_PROGRESS = "This email is already being sent or written to Gmail. Wait a moment, then reload"
_SEND_UNCERTAIN = (
    "Gmail may already have sent this email. Check your Gmail Sent folder: "
    "if it went out, use \"I sent it\"; if not, press Send again."
)
_DRAFT_UNCERTAIN = (
    "Gmail may have made a draft of this email that the app did not record. Check your Gmail Drafts and Sent "
    "folders: delete any draft of it, or send it from Gmail and use \"I sent it\". If nothing went out, press Send again."
)


class SendConflictError(Exception):
    """Another request holds this email, or a Gmail draft of it is still live."""


def send_claim_row(conn: sqlite3.Connection, target_id: str, user_id: str, kind: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM outreach_send_claims WHERE target_id=? AND user_id=? AND kind=?", (target_id, user_id, kind)
    ).fetchone()


def send_claim_held(row: sqlite3.Row) -> bool:
    """Whether a request may still be working under this claim."""
    if row["state"] not in {"drafting", "sending"}:
        return False
    if row["instance"] == SERVER_INSTANCE:
        # A claim this process left behind (its request ended without being
        # able to settle it) is as uncertain as one from a dead process.
        return row["token"] in _RUNNING
    age = datetime.now(timezone.utc) - datetime.fromisoformat(row["claimed_at"])
    return age < FOREIGN_CLAIM_GRACE


def claim_reason(row: sqlite3.Row) -> str:
    return _DRAFT_UNCERTAIN if row["action"] == "draft" else _SEND_UNCERTAIN


# Tokens of the claims whose requests are running in this process.
_RUNNING: set[str] = set()


@contextmanager
def claimed_send(
    conn: sqlite3.Connection, target_id: str, user_id: str, kind: str, action: str,
    revalidate: Callable[[], Any], *, stale_token: str = "",
) -> Iterator[tuple[str, Any]]:
    """Hold the claim for the body; the checks are re-run on a fresh read as it is taken.

    The insert comes first so SQLite holds the write lock while the target is
    re-read, and the checks raising rolls the claim back. The body settles the
    claim; one it could not settle is treated as uncertain once the body ends.
    """
    token = uuid4().hex
    _RUNNING.add(token)
    try:
        try:
            with conn:
                if stale_token and not conn.execute(
                    "DELETE FROM outreach_send_claims WHERE target_id=? AND kind=? AND token=?", (target_id, kind, stale_token)
                ).rowcount:
                    raise SendConflictError(IN_PROGRESS)
                conn.execute(
                    """
                    INSERT INTO outreach_send_claims(target_id, user_id, kind, token, state, action, instance, claimed_at)
                    VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (target_id, user_id, kind, token, "sending" if action == "send" else "drafting", action, SERVER_INSTANCE, utc_now()),
                )
                result = revalidate()
        except Exception as exc:
            if is_unique_violation(exc):
                raise SendConflictError(IN_PROGRESS) from exc
            raise
        yield token, result
    finally:
        _RUNNING.discard(token)


def settle_send_claim(conn: sqlite3.Connection, target_id: str, kind: str, token: str, state: str | None) -> None:
    """Move our own claim to ``state``, or drop it when ``state`` is None. Never raises."""
    try:
        with conn:
            if state is None:
                conn.execute("DELETE FROM outreach_send_claims WHERE target_id=? AND kind=? AND token=?", (target_id, kind, token))
            else:
                conn.execute("UPDATE outreach_send_claims SET state=? WHERE target_id=? AND kind=? AND token=?", (state, target_id, kind, token))
    except Exception:
        # Left as it was, the claim still blocks another send, which is the safe side.
        pass
