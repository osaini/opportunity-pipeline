"""The action types the ledger knows how to read, make and take back: stage, intent, task, status, follow-up draft,
résumé pick and thank-you.

automation.py is the ledger itself (perform, approve, undo, the switches, the breaker); each class here is what one
action type does to its own records, and none of them opens a transaction. They reach into the outreach and
application modules (a status change, a saved draft), which the ledger must not depend on, so they live here and are
registered at startup (register(), called through bootstrap.register_all) instead of being built into the ledger's
registry when it is imported. application_inbox and outreach_thank_you register theirs the same way.

This module imports automation, never the other way round.
"""

from __future__ import annotations

import sqlite3
from typing import Any
from urllib.parse import urlsplit

from ..applications import actions
from .ledger import (
    HandlerBase,
    NotApplicable,
    Superseded,
    capitalized,
    changed_words,
    dumps,
    for_update_clause,
    now_utc,
    register_handler,
    same_as,
)
from ..core.timestamps import parse_app_instant
from ..core.user_time import user_timezone


UNDO_REMINDER_NOTES = {
    "restored": "Your follow-up reminder is scheduled again.",
    "no_date": "The follow-up reminder this change cancelled was not restored, because no follow-up date is set.",
    "passed": "The follow-up reminder this change cancelled was not restored, because its date has passed. Set a new follow-up date if you want one.",
    "changed": "The follow-up reminder this change cancelled was changed since, so it was left as it is. Set the follow-up date again if you want a reminder.",
}
REOPEN_REMINDER_NOTES = {
    "no_date": "The follow-up reminder the automatic archive cancelled was not restored, because no follow-up date is set.",
    "passed": "The follow-up reminder the automatic archive cancelled was not restored, because its date has passed. Set a new follow-up date if you want one.",
    "changed": "The follow-up reminder the automatic archive cancelled was changed since, so it was left as it is. Set the follow-up date again if you want a reminder.",
}


class ApplicationStage(HandlerBase):
    """application.stage: an application's stage, and when the student applied.

    Undo restores both, only while both are still what this action left.

    A move to a closed stage also cancels the application's follow-up
    reminder (actions.update_application_tx). apply records when it did, and
    undo schedules that reminder again, but only while it is still the one
    this move cancelled (unchanged since) and the follow-up date is still set
    and still ahead. Otherwise undo says the reminder was not restored, so the
    student is not left with a follow-up date that will never remind them.

    A move out of an archive the app made after no reply
    (internal_automation.automatic_archive), to a stage that keeps its
    reminder, is the same as undoing that archive for the reminder: the one
    the archive cancelled is scheduled again under the same conditions, or
    the result's reminder_note says why not. Undoing that move cancels it
    again while it is unchanged.
    """

    fields = ("stage", "applied_at")

    def read(self, conn: sqlite3.Connection, user_id: str, subject_id: str) -> dict[str, Any]:
        row = conn.execute(
            f"SELECT stage, applied_at FROM applications WHERE id=? AND user_id=?{for_update_clause(conn)}", (subject_id, user_id),
        ).fetchone()
        if row is None:
            raise actions.ApplicationNotFoundError(subject_id)
        return {"stage": row["stage"], "applied_at": row["applied_at"]}

    def effective(self, before: dict[str, Any], after: dict[str, Any], timestamp: str) -> dict[str, Any]:
        """What the fields become, by the same rule _update_application_tx writes with.

        ``only_from`` makes the move conditional: when the stage is no longer
        that one (the student moved it on), nothing changes.
        """
        stage = after.get("stage")
        if stage not in actions.APPLICATION_STAGES:
            raise ValueError(f"Unsupported application stage: {stage}")
        if after.get("only_from") is not None and before["stage"] != after["only_from"]:
            return dict(before)
        return {"stage": stage, "applied_at": actions.applied_at_for_stage(before["applied_at"], stage, after.get("applied_at"), timestamp)}

    @staticmethod
    def _reminder(conn: sqlite3.Connection, user_id: str, subject_id: str) -> Any:
        return conn.execute(
            "SELECT status, updated_at FROM reminders WHERE application_id=? AND user_id=? AND reminder_type='follow_up'",
            (subject_id, user_id),
        ).fetchone()

    def apply(
        self, conn: sqlite3.Connection, user_id: str, subject_id: str, after: dict[str, Any], *, source: str, timestamp: str,
    ) -> dict[str, Any]:
        reminder = self._reminder(conn, user_id, subject_id)
        # Read before the move: once it is made, the archive is no longer the latest stage change.
        archive = None
        if after["stage"] not in actions.TERMINAL_APPLICATION_STAGES:
            from . import internal as internal_automation

            archive = internal_automation.automatic_archive(conn, subject_id)
        actions.update_application_tx(
            conn, subject_id, stage=after["stage"], applied_at=after.get("applied_at"), user_id=user_id,
            source=source, timestamp=timestamp,
        )
        now = self._reminder(conn, user_id, subject_id)
        result: dict[str, Any] = {}
        if reminder is not None and reminder["status"] == "scheduled" and now is not None and now["status"] == "cancelled":
            # What undo needs to put it back, and to know it is still the one this move cancelled.
            result["reminder_cancelled_at"] = now["updated_at"]
        if archive and archive.get("reminder_cancelled_at"):
            outcome = self._reschedule(conn, user_id, subject_id, archive["reminder_cancelled_at"], timestamp)
            if outcome == "restored":
                result["reminder_restored_at"] = timestamp
            else:
                result["reminder_note"] = REOPEN_REMINDER_NOTES[outcome]
        return result

    def undo(
        self, conn: sqlite3.Connection, user_id: str, subject_id: str, before: dict[str, Any], after: dict[str, Any],
        *, source: str, timestamp: str,
    ) -> dict[str, Any]:
        self._restore_stage(conn, user_id, subject_id, before, after, source=source, timestamp=timestamp)
        result = after.get("_result") or {}
        if result.get("reminder_restored_at"):
            # This move put back the reminder an automatic archive cancelled; back in Archived, it stops again.
            conn.execute(
                """
                UPDATE reminders SET status='cancelled', updated_at=?
                WHERE application_id=? AND user_id=? AND reminder_type='follow_up' AND status='scheduled' AND updated_at=?
                """,
                (timestamp, subject_id, user_id, result["reminder_restored_at"]),
            )
        cancelled_at = result.get("reminder_cancelled_at")
        if not cancelled_at:
            return {}
        return self._restore_reminder(conn, user_id, subject_id, cancelled_at, timestamp)

    def _reschedule(self, conn: sqlite3.Connection, user_id: str, subject_id: str, cancelled_at: str, timestamp: str) -> str:
        """Schedule again a follow-up reminder a move to a closed stage cancelled: restored, no_date, passed, or changed."""
        row = conn.execute(
            f"SELECT follow_up_at FROM applications WHERE id=? AND user_id=?{for_update_clause(conn)}", (subject_id, user_id),
        ).fetchone()
        due = parse_app_instant(row["follow_up_at"]) if row is not None else None
        if due is None:
            return "no_date"
        if due <= now_utc(None):
            return "passed"
        # Due on the follow-up date as it stands now: the student may have moved it while the stage was closed.
        restored = conn.execute(
            """
            UPDATE reminders SET status='scheduled', due_at=?, updated_at=?
            WHERE application_id=? AND user_id=? AND reminder_type='follow_up' AND status='cancelled' AND updated_at=?
            """,
            (row["follow_up_at"], timestamp, subject_id, user_id, cancelled_at),
        ).rowcount
        return "restored" if restored else "changed"

    def _restore_reminder(
        self, conn: sqlite3.Connection, user_id: str, subject_id: str, cancelled_at: str, timestamp: str,
    ) -> dict[str, Any]:
        """Schedule again the follow-up reminder this move cancelled, or say why it stays cancelled."""
        outcome = self._reschedule(conn, user_id, subject_id, cancelled_at, timestamp)
        return {"reminder_restored": outcome == "restored", "undo_note": UNDO_REMINDER_NOTES[outcome]}

    def _restore_stage(
        self, conn: sqlite3.Connection, user_id: str, subject_id: str, before: dict[str, Any], after: dict[str, Any],
        *, source: str, timestamp: str,
    ) -> None:
        restored = conn.execute(
            f"UPDATE applications SET stage=?, applied_at=?, updated_at=? WHERE id=? AND user_id=? AND stage=? AND {same_as(conn, 'applied_at')}",
            (before["stage"], before["applied_at"], timestamp, subject_id, user_id, after["stage"], after["applied_at"]),
        ).rowcount
        if not restored:
            try:
                current = self.read(conn, user_id, subject_id)
            except LookupError:
                raise Superseded("The application no longer exists, so there is nothing to undo") from None
            raise Superseded(f"{capitalized(changed_words({'stage': after['stage'], 'applied_at': after['applied_at']}, current))} "
                             "changed since, so it was left as it is")
        if before["stage"] != after["stage"]:
            # The undo direction: from what the action made back to what it replaced.
            actions.log_application_event(
                conn, subject_id, "stage_changed", {"source": source}, timestamp,
                from_stage=after["stage"], to_stage=before["stage"],
            )
        else:
            actions.log_application_event(
                conn, subject_id, "application_updated", {"source": source, "fields": ["applied_at"]}, timestamp,
            )


INTENTS = ("", "saved", "passed")


class OpportunityIntent(HandlerBase):
    """opportunity.intent: whether the student saved or passed on an opportunity ('' for neither).

    Interactions are a history, so undo adds a row restoring the earlier
    choice, and only while the latest row is still the one this action added.

    On PostgreSQL the opportunity row is locked before the history is read:
    recording a save or pass checks that row's key (the foreign key) with a
    lock this one conflicts with, so a student's choice waits for this
    transaction instead of landing between the read and the write.
    """

    fields = ("intent",)

    def _lock(self, conn: sqlite3.Connection, subject_id: str) -> bool:
        return conn.execute(f"SELECT 1 FROM opportunities WHERE id=?{for_update_clause(conn)}", (subject_id,)).fetchone() is not None

    def read(self, conn: sqlite3.Connection, user_id: str, subject_id: str) -> dict[str, Any]:
        if not self._lock(conn, subject_id):
            raise actions.OpportunityNotFoundError(subject_id)
        return {"intent": actions.intent_state(conn, subject_id, user_id)}

    def effective(self, before: dict[str, Any], after: dict[str, Any], timestamp: str) -> dict[str, Any]:
        if after.get("intent") not in INTENTS:
            raise ValueError(f"Unsupported intent: {after.get('intent')!r}")
        return {"intent": after["intent"]}

    def apply(
        self, conn: sqlite3.Connection, user_id: str, subject_id: str, after: dict[str, Any], *, source: str, timestamp: str,
    ) -> dict[str, Any]:
        # ``only_if_untouched`` (auto_triage): act only on a role the student has never saved, passed,
        # or opened. Checked here, inside the write, so a choice they made meanwhile always stands.
        if after.get("only_if_untouched") and conn.execute(
            "SELECT 1 FROM opportunity_interactions WHERE opportunity_id=? AND user_id=? "
            "UNION ALL SELECT 1 FROM applications WHERE opportunity_id=? AND user_id=? LIMIT 1",
            (subject_id, user_id, subject_id, user_id),
        ).fetchone() is not None:
            raise NotApplicable("You already acted on this role, so it was left as it is")
        response = actions.record_intent_tx(
            conn, subject_id, after["intent"] or "undo", user_id=user_id, source=source, timestamp=timestamp,
        )
        if response["unchanged"]:
            return {}
        row = conn.execute(
            "SELECT id FROM opportunity_interactions WHERE opportunity_id=? AND user_id=? AND action=? AND created_at=?",
            (subject_id, user_id, after["intent"] or "undo", timestamp),
        ).fetchone()
        return {"interaction_id": int(row["id"])}

    def undo(
        self, conn: sqlite3.Connection, user_id: str, subject_id: str, before: dict[str, Any], after: dict[str, Any],
        *, source: str, timestamp: str,
    ) -> None:
        added = (after.get("_result") or {}).get("interaction_id")
        self._lock(conn, subject_id)
        latest = conn.execute(
            "SELECT id FROM opportunity_interactions WHERE opportunity_id=? AND user_id=? ORDER BY id DESC LIMIT 1",
            (subject_id, user_id),
        ).fetchone()
        if added is None or latest is None or int(latest["id"]) != int(added):
            raise Superseded("The saved or passed choice changed since, so it was left as it is")
        actions.record_intent_tx(conn, subject_id, before["intent"] or "undo", user_id=user_id, source=source, timestamp=timestamp)


class ApplicationTask(HandlerBase):
    """application.task: a task added to an application (the subject).

    Undo deletes it only while it is still open with the title and due date it
    was created with; a task the student edited, finished, or deleted stays as they left it.
    """

    fields = ("task",)

    def read(self, conn: sqlite3.Connection, user_id: str, subject_id: str) -> dict[str, Any]:
        if conn.execute("SELECT 1 FROM applications WHERE id=? AND user_id=?", (subject_id, user_id)).fetchone() is None:
            raise actions.ApplicationNotFoundError(subject_id)
        # Each task is new; the idempotency key is what stops a second copy.
        return {"task": None}

    def effective(self, before: dict[str, Any], after: dict[str, Any], timestamp: str) -> dict[str, Any]:
        task = after.get("task")
        if not isinstance(task, dict) or not str(task.get("title") or "").strip():
            raise ValueError("Task title is required")
        return {"task": task}

    def apply(
        self, conn: sqlite3.Connection, user_id: str, subject_id: str, after: dict[str, Any], *, source: str, timestamp: str,
    ) -> dict[str, Any]:
        task = after["task"]
        due_at = user_timezone(conn, user_id).normalize_instant(task.get("due_at"), field="due_at")
        row = actions.add_application_task_tx(
            conn, subject_id, title=str(task["title"]), due_at=due_at, user_id=user_id,
            origin=str(task.get("origin") or "automation"), origin_ref=str(task.get("origin_ref") or ""),
            source=source, timestamp=timestamp, link=str(task.get("link") or ""),
        )
        return {"task_id": row["id"], "title": row["title"], "due_at": row["due_at"]}

    def ledger(self, after: dict[str, Any]) -> dict[str, Any]:
        """The task's link (an assessment login, a scheduling page) stays on the task; the ledger keeps its host.

        That holds for a proposal too: perform() keeps the link in automation_held, and approve() adds it back.
        """
        task = after.get("task")
        if not isinstance(task, dict) or not task.get("link"):
            return after
        kept = {key: value for key, value in task.items() if key != "link"}
        try:
            kept["link_host"] = (urlsplit(str(task["link"])).hostname or "").lower()
        except ValueError:
            kept["link_host"] = ""
        return {**after, "task": kept}

    def undo(
        self, conn: sqlite3.Connection, user_id: str, subject_id: str, before: dict[str, Any], after: dict[str, Any],
        *, source: str, timestamp: str,
    ) -> None:
        created = after.get("_result") or {}
        deleted = conn.execute(
            f"DELETE FROM application_tasks WHERE id=? AND user_id=? AND application_id=? AND status='open' AND title=? AND {same_as(conn, 'due_at')}",
            (created.get("task_id"), user_id, subject_id, created.get("title"), created.get("due_at")),
        ).rowcount
        if not deleted:
            row = conn.execute("SELECT status FROM application_tasks WHERE id=? AND user_id=?", (created.get("task_id"), user_id)).fetchone()
            if row is None:
                raise Superseded("The task was already deleted")
            if row["status"] != "open":
                raise Superseded("The task was marked done since, so it was left as it is")
            raise Superseded("The task was edited since, so it was left as it is")
        actions.log_application_event(
            conn, subject_id, "task_removed",
            {"task_id": created.get("task_id"), "title": created.get("title"), "source": source}, timestamp,
        )


class OutreachStatus(HandlerBase):
    """outreach.status: a cold-outreach company's status (outreach_auto_close closes one as no_response).

    The change runs through outreach.update_target_tx, so it has every side
    effect a status change made by hand has: a follow-up date that no longer
    applies is cleared, and the change is logged. Undo puts the status back only
    while it is still what this action left, and the follow-up date too, only
    while it is still what this action left.
    """

    fields = ("status",)

    def read(self, conn: sqlite3.Connection, user_id: str, subject_id: str) -> dict[str, Any]:
        row = conn.execute(
            f"SELECT status FROM outreach_targets WHERE id=? AND user_id=?{for_update_clause(conn)}", (subject_id, user_id),
        ).fetchone()
        if row is None:
            from ..outreach import OutreachNotFoundError

            raise OutreachNotFoundError(subject_id)
        return {"status": row["status"]}

    def effective(self, before: dict[str, Any], after: dict[str, Any], timestamp: str) -> dict[str, Any]:
        from ..outreach import OUTREACH_STATUSES

        if after.get("status") not in OUTREACH_STATUSES:
            raise ValueError(f"Unsupported outreach status: {after.get('status')!r}")
        # ``only_from``: change it only from that status, so a reply recorded meanwhile stands.
        if after.get("only_from") is not None and before["status"] != after["only_from"]:
            return dict(before)
        return {"status": after["status"]}

    def apply(
        self, conn: sqlite3.Connection, user_id: str, subject_id: str, after: dict[str, Any], *, source: str, timestamp: str,
    ) -> dict[str, Any]:
        from ..outreach import update_target_tx

        written = update_target_tx(
            conn, subject_id, {"status": after["status"]}, user_id=user_id, status_detail="Changed automatically",
        )
        previous = written["previous"]
        return {
            "follow_up_at_before": previous.get("follow_up_at"),
            "follow_up_at_after": written["values"].get("follow_up_at", previous.get("follow_up_at")),
        }

    def undo(
        self, conn: sqlite3.Connection, user_id: str, subject_id: str, before: dict[str, Any], after: dict[str, Any],
        *, source: str, timestamp: str,
    ) -> dict[str, Any] | None:
        from ..outreach import log_event

        row = conn.execute(
            f"SELECT status, follow_up_at FROM outreach_targets WHERE id=? AND user_id=?{for_update_clause(conn)}", (subject_id, user_id),
        ).fetchone()
        if row is None:
            raise Superseded("The company is no longer in your outreach list, so there is nothing to undo")
        if row["status"] != after["status"]:
            raise Superseded("The status changed since, so it was left as it is")
        result = after.get("_result") or {}
        restore_date = row["follow_up_at"] == result.get("follow_up_at_after")
        follow_up_at = result.get("follow_up_at_before") if restore_date else row["follow_up_at"]
        conn.execute(
            f"UPDATE outreach_targets SET status=?, follow_up_at=?, updated_at=? WHERE id=? AND user_id=? AND status=? AND {same_as(conn, 'follow_up_at')}",
            (before["status"], follow_up_at, timestamp, subject_id, user_id, after["status"], row["follow_up_at"]),
        )
        log_event(conn, subject_id, user_id, "status", from_status=after["status"], to_status=before["status"], detail="Undone by you")
        notes = []
        if not restore_date and result.get("follow_up_at_before") != row["follow_up_at"]:
            notes.append("The follow-up date was changed since, so it was left as it is.")
        # A thank-you planned with this change (decline_thank_you) is an email, which this Undo does not stop.
        waiting = conn.execute(
            "SELECT to_name, to_email, state FROM outreach_thank_yous WHERE target_id=? AND user_id=? "
            "AND state IN ('planned', 'scheduled', 'sending', 'transmitting')",
            (subject_id, user_id),
        ).fetchone()
        if waiting is not None:
            from ..outreach_greeting import contact_first_name

            who = contact_first_name(waiting["to_name"]) or waiting["to_email"]
            notes.append(
                f"The thank-you to {who} is being sent now, so it could not be stopped." if waiting["state"] in ("sending", "transmitting")
                else f"The thank-you to {who} is still scheduled; cancel it on the company's card in Outreach."
            )
        return {"undo_note": " ".join(notes)} if notes else None


class OutreachFollowUpDraft(HandlerBase):
    """outreach.follow_up_draft: a follow-up draft auto_follow_up_drafts wrote for a company.

    ``after['draft']`` is what outreach_drafting.compose_draft wrote; apply stores
    it the way a draft written on request is stored (the history keeps it), and
    only while the company is still waiting on a follow-up, with none written.
    Undo discards the draft from the editor only while it is exactly as written
    (its fingerprint) and not approved; the draft stays in the history.
    """

    fields = ("follow_up",)

    @staticmethod
    def _row(conn: sqlite3.Connection, user_id: str, subject_id: str) -> Any:
        return conn.execute(
            "SELECT status, contact_email, contact_cc, follow_up_subject, follow_up_body, follow_up_claims_json, "
            f"follow_up_generated_by, follow_up_status FROM outreach_targets WHERE id=? AND user_id=?{for_update_clause(conn)}",
            (subject_id, user_id),
        ).fetchone()

    @staticmethod
    def _fingerprint(row: Any) -> str:
        from ..outreach import compute_draft_fingerprint

        # The same inputs outreach._record fingerprints the follow-up with.
        return compute_draft_fingerprint(
            "follow_up", row["follow_up_subject"] or "", row["follow_up_body"] or "", row["contact_email"] or "",
            row["follow_up_claims_json"] or "[]", row["follow_up_generated_by"] or "", row["contact_cc"] or "",
        )

    def read(self, conn: sqlite3.Connection, user_id: str, subject_id: str) -> dict[str, Any]:
        row = self._row(conn, user_id, subject_id)
        if row is None:
            from ..outreach import OutreachNotFoundError

            raise OutreachNotFoundError(subject_id)
        written = bool(str(row["follow_up_body"] or "").strip() or str(row["follow_up_subject"] or "").strip())
        return {"follow_up": self._fingerprint(row) if written else ""}

    def effective(self, before: dict[str, Any], after: dict[str, Any], timestamp: str) -> dict[str, Any]:
        draft = after.get("draft")
        if not isinstance(draft, dict) or draft.get("kind") != "follow_up" or not str(draft.get("body") or "").strip():
            raise ValueError("An automatic follow-up needs a written follow-up draft")
        if before["follow_up"]:
            # A follow-up is already written (the student's own, most likely): it stays.
            return dict(before)
        return {"follow_up": "generated"}

    def apply(
        self, conn: sqlite3.Connection, user_id: str, subject_id: str, after: dict[str, Any], *, source: str, timestamp: str,
    ) -> dict[str, Any]:
        from ..outreach import get_target, heard_back
        from ..outreach_drafting import save_draft_tx

        target = get_target(conn, subject_id, user_id=user_id)
        if (
            target["status"] != "sent" or target["follow_up_body"] or heard_back(target)
            or target["contact_bounced"] or target.get("bounced_at")
        ):
            raise NotApplicable("The company is no longer waiting on a follow-up, so no draft was saved")
        prepared = {**after["draft"], "target_status": target["status"], "target_draft_status": target["follow_up_status"]}
        version_id = save_draft_tx(conn, subject_id, user_id=user_id, prepared=prepared)
        return {"fingerprint": self._fingerprint(self._row(conn, user_id, subject_id)), "version_id": version_id}

    def undo(
        self, conn: sqlite3.Connection, user_id: str, subject_id: str, before: dict[str, Any], after: dict[str, Any],
        *, source: str, timestamp: str,
    ) -> dict[str, Any]:
        from ..outreach import log_event

        result = after.get("_result") or {}
        row = self._row(conn, user_id, subject_id)
        if row is None:
            raise Superseded("The company is no longer in your outreach list, so there is nothing to undo")
        if row["follow_up_status"] == "approved":
            raise Superseded("You approved the follow-up draft since, so it was left as it is")
        if not (row["follow_up_body"] or row["follow_up_subject"]):
            raise Superseded("The follow-up draft was already cleared")
        if row["follow_up_status"] != "generated" or self._fingerprint(row) != result.get("fingerprint"):
            raise Superseded("The follow-up draft or its recipient changed since, so it was left as it is")
        conn.execute(
            """
            UPDATE outreach_targets SET follow_up_subject='', follow_up_body='', follow_up_claims_json='[]',
                follow_up_generated_by='', follow_up_status='none', updated_at=?
            WHERE id=? AND user_id=? AND follow_up_status='generated'
            """,
            (timestamp, subject_id, user_id),
        )
        log_event(conn, subject_id, user_id, "follow_up_discarded",
             detail="The automatic follow-up draft was undone. It stays in the follow-up history.")
        return {"undo_note": "The draft stays in the follow-up's history if you want it back."}


class ResumePick(HandlerBase):
    """resume.pick: which résumé variant to use for a role (resume_variant_pick).

    A pick the student made (picked_by 'student') is never overwritten: the
    change counts as already in place. Undo clears the automatic pick, only
    while it is still the one this action made.
    """

    fields = ("resume_pick",)

    def read(self, conn: sqlite3.Connection, user_id: str, subject_id: str) -> dict[str, Any]:
        # No row lock on the role: a save's pick runs in the web request, and on
        # PostgreSQL FOR UPDATE here would wait for a running sync, which holds
        # every role's row until it commits. The insert in apply is what guards
        # against a student pick landing meanwhile.
        if conn.execute("SELECT 1 FROM opportunities WHERE id=?", (subject_id,)).fetchone() is None:
            raise actions.OpportunityNotFoundError(subject_id)
        row = conn.execute(
            f"SELECT resume_file_id, picked_by FROM opportunity_resume_picks WHERE user_id=? AND opportunity_id=?{for_update_clause(conn)}",
            (user_id, subject_id),
        ).fetchone()
        return {"resume_pick": None if row is None else {"resume_file_id": row["resume_file_id"], "picked_by": row["picked_by"]}}

    def effective(self, before: dict[str, Any], after: dict[str, Any], timestamp: str) -> dict[str, Any]:
        file_id = after.get("resume_file_id")
        if not isinstance(file_id, str) or not file_id:
            raise ValueError("A résumé pick needs a résumé")
        current = before.get("resume_pick")
        if current and current.get("picked_by") == "student":
            return dict(before)  # the student's choice sticks
        return {"resume_pick": {"resume_file_id": file_id, "picked_by": "automatic"}}

    def apply(
        self, conn: sqlite3.Connection, user_id: str, subject_id: str, after: dict[str, Any], *, source: str, timestamp: str,
    ) -> dict[str, Any]:
        if conn.execute(
            "SELECT 1 FROM resume_files WHERE id=? AND user_id=?", (after["resume_file_id"], user_id),
        ).fetchone() is None:
            raise NotApplicable("That résumé was deleted, so nothing was picked")
        written = conn.execute(
            """
            INSERT INTO opportunity_resume_picks(user_id, opportunity_id, resume_file_id, picked_by, matched_json, created_at, updated_at)
            VALUES(?, ?, ?, 'automatic', ?, ?, ?)
            ON CONFLICT(user_id, opportunity_id) DO UPDATE SET
                resume_file_id=excluded.resume_file_id, matched_json=excluded.matched_json, updated_at=excluded.updated_at
            WHERE opportunity_resume_picks.picked_by='automatic'
            """,
            (user_id, subject_id, after["resume_file_id"], dumps(after.get("matched") or {}), timestamp, timestamp),
        ).rowcount
        if not written:
            # The student picked one for this role after it was read: theirs sticks, and no row claims a pick.
            raise NotApplicable("You picked a résumé for this role yourself, so it was left as it is")
        return {}

    def undo(
        self, conn: sqlite3.Connection, user_id: str, subject_id: str, before: dict[str, Any], after: dict[str, Any],
        *, source: str, timestamp: str,
    ) -> None:
        pick = after.get("resume_pick") or {}
        deleted = conn.execute(
            "DELETE FROM opportunity_resume_picks WHERE user_id=? AND opportunity_id=? AND resume_file_id=? AND picked_by='automatic'",
            (user_id, subject_id, pick.get("resume_file_id")),
        ).rowcount
        if not deleted:
            raise Superseded("The résumé choice changed since, so it was left as it is")


class OutreachThankYou(HandlerBase):
    """outreach.thank_you: a thank-you after a decline, written and scheduled (decline_thank_you).

    ``after['thank_you']`` is the thank-you outreach_thank_you.plan wrote: its
    recipient, words, thread, fingerprint, and when it goes. apply stores it
    (outreach_thank_yous, state 'scheduled') and queues it
    (outreach_scheduled_sends, kind 'thank_you'), only while the company has
    none: one per company, ever. An email cannot be taken back once it goes, so
    the action has no Undo; the card's Cancel stops it while it waits, and
    undoing the status change made with it leaves it scheduled.
    """

    fields = ("thank_you",)
    undoable = False
    _COLUMNS = ("reply_gmail_id", "reply_message_id", "thread_id", "to_email", "to_name", "subject", "body",
                "generated_by", "fingerprint", "send_at", "label")

    def read(self, conn: sqlite3.Connection, user_id: str, subject_id: str) -> dict[str, Any]:
        if conn.execute(
            f"SELECT 1 FROM outreach_targets WHERE id=? AND user_id=?{for_update_clause(conn)}", (subject_id, user_id),
        ).fetchone() is None:
            from ..outreach import OutreachNotFoundError

            raise OutreachNotFoundError(subject_id)
        row = conn.execute("SELECT state FROM outreach_thank_yous WHERE target_id=? AND user_id=?", (subject_id, user_id)).fetchone()
        return {"thank_you": None if row is None else str(row["state"])}

    def effective(self, before: dict[str, Any], after: dict[str, Any], timestamp: str) -> dict[str, Any]:
        planned = after.get("thank_you")
        if not isinstance(planned, dict) or not str(planned.get("body") or "").strip() or not planned.get("send_at"):
            raise ValueError("A thank-you needs its words and a time to go")
        if before.get("thank_you") is not None:
            return dict(before)  # one per company: whatever became of the first, there is no second
        return {"thank_you": "scheduled"}

    def ledger(self, after: dict[str, Any]) -> dict[str, Any]:
        """The words stay on the thank-you itself; the ledger keeps who it went to, when, and its fingerprint."""
        planned = after.get("thank_you")
        if not isinstance(planned, dict):
            return after
        kept = {key: planned.get(key) for key in ("to_name", "fingerprint", "send_at", "label", "generated_by")}
        return {**after, "thank_you": kept}

    def apply(
        self, conn: sqlite3.Connection, user_id: str, subject_id: str, after: dict[str, Any], *, source: str, timestamp: str,
    ) -> dict[str, Any]:
        planned = after["thank_you"]
        values = [str(planned.get(column) or "") for column in self._COLUMNS]
        conn.execute(
            f"""
            INSERT INTO outreach_thank_yous(target_id, user_id, {', '.join(self._COLUMNS)}, state, note, created_at, updated_at)
            VALUES(?, ?, {', '.join('?' for _ in self._COLUMNS)}, 'scheduled', '', ?, ?)
            """,
            (subject_id, user_id, *values, timestamp, timestamp),
        )
        conn.execute(
            """
            INSERT INTO outreach_scheduled_sends(target_id, user_id, kind, fingerprint, send_at, timezone, label, state, error, attempts, created_at, updated_at)
            VALUES(?, ?, 'thank_you', ?, ?, ?, ?, 'scheduled', '', 0, ?, ?)
            ON CONFLICT(target_id, kind) DO UPDATE SET fingerprint=excluded.fingerprint, send_at=excluded.send_at,
                timezone=excluded.timezone, label=excluded.label, state='scheduled', error='', attempts=0, updated_at=excluded.updated_at
            """,
            (subject_id, user_id, planned["fingerprint"], planned["send_at"], str(planned.get("timezone") or ""),
             planned["label"], timestamp, timestamp),
        )
        return {"send_at": planned["send_at"]}

    def undo(
        self, conn: sqlite3.Connection, user_id: str, subject_id: str, before: dict[str, Any], after: dict[str, Any],
        *, source: str, timestamp: str,
    ) -> None:
        raise ValueError("This action can't be undone")


def register() -> None:
    """Add this module's action types to the ledger's registry. Called once at startup (bootstrap.register_all)."""
    for action_type, handler in (
        ("application.stage", ApplicationStage()),
        ("opportunity.intent", OpportunityIntent()),
        ("application.task", ApplicationTask()),
        ("outreach.status", OutreachStatus()),
        ("outreach.follow_up_draft", OutreachFollowUpDraft()),
        ("resume.pick", ResumePick()),
        ("outreach.thank_you", OutreachThankYou()),
    ):
        register_handler(action_type, handler)
