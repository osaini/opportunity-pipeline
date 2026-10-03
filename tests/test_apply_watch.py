"""Apply for me's 24 hour confirmation watch (apply/watch.py, spec 6.16), the card's states and answers (10.5), and the
per-ATS statistics (8, 8.8's counting; the threshold is M6's).

No browser and no network: the watch reads application_mail_messages, which these tests fill the way the Phase 1 reader
does. Every company, address and message here is invented.
"""

import json
import sqlite3
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from opportunity_app import SERVER_INSTANCE
from opportunity_app.automation import ledger as automation, health as automation_health
from opportunity_app.apply import claims as apply_claims, runs as apply_runs, watch as apply_watch
from opportunity_app.applications import actions, inbox as application_inbox, urgent
from opportunity_app.core.database import connect_product
from opportunity_app.core.timestamps import parse_app_instant, utc_now
from opportunity_app.outreach.automation import AutomationWorker
from opportunity_app.student.profile import update_profile

import test_apply_api as api_tests
from helpers_apply import ApplyCase, BLUEFIN, USER, setUpModule, tearDownModule  # noqa: F401 (module fixtures: unittest and pytest find them here)

try:
    import realdata_guard
except ImportError:  # imported as tests.<module>, with tests/ not on sys.path
    from tests import realdata_guard
# Guards this module's own run against opening data/*.db (AGENTS.md hard rule 1); see tests/realdata_guard.py.
realdata_guard.install()

EMAIL = "sam.rivera@example.test"
SUBJECT = "Thank you for applying to Bluefin Robotics"
SECURITY_CODE = "Security code for your application to Bluefin Robotics"


def iso(moment):
    return moment.astimezone(timezone.utc).isoformat(timespec="microseconds")


class WatchCase(ApplyCase):
    """A claim handed over a while ago and a Gmail reader that is healthy, both written directly."""

    def setUp(self):
        super().setUp()
        self.mail_serial = 0
        update_profile(self.conn, {"contact": {"email": EMAIL}}, ["contact"], user_id=USER)
        self.reader()

    # --- the reader and the mail it recorded

    def switch(self, mode):
        with self.conn:
            if mode == "off":
                self.conn.execute("DELETE FROM user_settings WHERE user_id=? AND key=?", (USER, application_inbox.FEATURE))
            else:
                self.conn.execute(
                    "INSERT INTO user_settings(user_id, key, value, updated_at) VALUES(?, ?, ?, ?) "
                    "ON CONFLICT(user_id, key) DO UPDATE SET value=excluded.value", (USER, application_inbox.FEATURE, mode, utc_now()),
                )

    def reader(self, *, ok_at="default", pending=(), recovery="", error="", history="h1", mode="shadow", status="connected",
               account=EMAIL, scopes=("https://www.googleapis.com/auth/gmail.readonly",)):
        """The mail reader as the watch sees it: the switch, the connection, and the sync row."""
        self.switch(mode)
        last_ok = iso(self.at(-1)) if ok_at == "default" else (None if ok_at is None else iso(ok_at))
        with self.conn:
            self.conn.execute("DELETE FROM connector_accounts WHERE user_id=?", (USER,))
            if status is not None:
                self.conn.execute(
                    "INSERT INTO connector_accounts(id, user_id, provider, scopes_json, status, created_at, updated_at, account_email) "
                    "VALUES('connector-gmail', ?, 'gmail_drafts', ?, ?, ?, ?, ?)", (USER, json.dumps(list(scopes)), status, utc_now(), utc_now(), account),
                )
            self.conn.execute("DELETE FROM application_mail_sync WHERE user_id=?", (USER,))
            self.conn.execute(
                "INSERT INTO application_mail_sync(user_id, history_id, pending_ids_json, recovery_state, last_ok_at, last_error, updated_at) "
                "VALUES(?, ?, ?, ?, ?, ?, ?)", (USER, history, json.dumps(list(pending)), recovery, last_ok, error, utc_now()),
            )

    def mail(self, application_id, *, kind="application_confirmation", matched_by="job_id", verified=1, received=None, subject=SUBJECT,
             state="done", domain="greenhouse-mail.io", gmail_id=None, linked=True):
        self.mail_serial += 1
        gmail_id = gmail_id or f"gm-{self.mail_serial}"
        with self.conn:
            self.conn.execute(
                "INSERT INTO application_mail_messages(user_id, gmail_id, application_id, kind, matched_by, state, subject, sender_domain, "
                "received_at, recorded_at, sender_verified) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (USER, gmail_id, application_id if linked else "", kind, matched_by, state, subject, domain,
                 iso(received or self.at(-5)), utc_now(), verified),
            )
        return gmail_id

    # --- claims

    def submitted(self, *, handed=None, submitted=None, until=None, verification="awaiting_email", mode="one_click", policy="record",
                  recorded=1, detail=None, **kwargs):
        """A submitted claim: handed over 2 hours ago by default, its 24 hour watch running."""
        handed = handed or self.at(hours=-2)
        submitted = submitted or handed + timedelta(minutes=1)
        token = self.raw_claim(
            state="submitted", mode=mode, handed_over_at=iso(handed), submitted_at=iso(submitted), verification=verification,
            stage_policy=policy, stage_recorded=recorded, detail=detail, **kwargs,
        )
        with self.conn:
            self.conn.execute(
                "UPDATE application_submit_claims SET watch_until=?, verified_at=? WHERE token=?",
                (iso(until or submitted + timedelta(hours=24)) if verification == "awaiting_email" else None, iso(submitted), token),
            )
            if recorded:
                self.conn.execute("UPDATE applications SET stage='applied', applied_at=? WHERE id=?", (iso(submitted), self.claim_row(token)["application_id"]))
        return token

    def application_of(self, token):
        return self.claim_row(token)["application_id"]

    def watch(self, minutes=0, **kwargs):
        return apply_watch.watch(self.conn, USER, self.at(minutes, **kwargs))

    def detail(self, token):
        return json.loads(self.claim_row(token)["detail_json"])

    def event_rows(self, token, event_type):
        return [json.loads(row["detail_json"]) for row in self.conn.execute(
            "SELECT detail_json FROM application_events WHERE application_id=? AND event_type=? ORDER BY id", (self.application_of(token), event_type),
        ).fetchall()]

    def zero(self, **changed):
        return {**dict.fromkeys(apply_watch.WATCH_COUNTS, 0), **changed}


# --- What confirms ---------------------------------------------------------------------------


class ConfirmationTests(WatchCase):
    def test_a_strong_match_confirms_a_submitted_claim(self):
        for index, tier in enumerate(("job_id", "company_title")):
            with self.subTest(tier=tier):
                token = self.submitted()
                received = self.at(minutes=-30 + index)
                self.mail(self.application_of(token), matched_by=tier, received=received)
                self.assertEqual(self.watch(), self.zero(email_confirmed=1))
                row = self.claim_row(token)
                self.assertEqual((row["state"], row["verification"]), ("submitted", "email_confirmed"))
                self.assertTrue(row["verified_at"])
                [event] = self.event_rows(token, "apply_agent_verification")
                self.assertEqual((event["verification"], event["source"], event["matched_by"]), ("email_confirmed", "apply_agent:confirmation_email", tier))
                self.assertEqual(parse_app_instant(self.detail(token)["email_received_at"]), received)
                self.assertEqual(self.notices(), [], "6.16 names no notice for a submission the email confirms")

    def test_a_company_single_confirmation_never_confirms_and_sets_possible_email_at(self):
        token = self.submitted()
        received = self.at(minutes=-20)
        self.mail(self.application_of(token), matched_by="company_single", received=received)
        self.assertEqual(self.watch(), self.zero(possible_email=1))
        row = self.claim_row(token)
        self.assertEqual((row["verification"], row["resolved_by"]), ("awaiting_email", ""))
        self.assertEqual(parse_app_instant(self.detail(token)["possible_email_at"]), received)
        self.assertEqual(self.watch(), self.zero(), "noted once")
        self.assertEqual(apply_watch.card_states(self.conn, USER)[self.application_of(token)]["status"], "watching", "the clock goes on")

    def test_a_greenhouse_subject_with_every_company_token_is_only_possible(self):
        token = self.submitted()
        self.mail("", matched_by="", linked=False, received=self.at(minutes=-20), subject="Thanks for applying to Bluefin Robotics")
        self.assertEqual(self.watch(), self.zero(possible_email=1))
        self.assertEqual(self.claim_row(token)["verification"], "awaiting_email")
        with self.conn:
            self.conn.execute("DELETE FROM application_submit_claims")
            self.conn.execute("DELETE FROM application_mail_messages")
        other = self.submitted()
        self.mail("", matched_by="", linked=False, received=self.at(minutes=-20), subject="Thanks for applying to Bluefin", gmail_id="partial")
        self.mail("", matched_by="", linked=False, received=self.at(minutes=-20), subject="Thanks for applying to Bluefin Robotics",
                  domain="example.org", gmail_id="elsewhere")
        self.watch()
        self.assertNotIn("possible_email_at", self.detail(other), "a partial name, and a sender that is not Greenhouse, are no evidence")
        self.assertEqual(self.claim_row(other)["verification"], "awaiting_email")

    def test_an_unverified_sender_never_confirms(self):
        token = self.submitted()
        self.mail(self.application_of(token), verified=0, received=self.at(minutes=-20))
        self.assertEqual(self.watch(), self.zero(possible_email=1), "weak evidence: it may be for this application, and the card says so")
        self.assertEqual(self.claim_row(token)["verification"], "awaiting_email")

    def test_other_kinds_and_states_never_confirm(self):
        token = self.submitted()
        application = self.application_of(token)
        self.mail(application, kind="interview", received=self.at(minutes=-20))
        self.mail(application, state="skipped", received=self.at(minutes=-20))
        self.mail(application, state="gone", received=self.at(minutes=-20))
        self.assertEqual(self.watch(), self.zero())
        self.mail(application, state="awaiting_resume", received=self.at(minutes=-20))
        self.assertEqual(self.watch(), self.zero(email_confirmed=1), "read while paused counts: it was read, only deciding waited")

    def test_an_email_from_before_the_hand_over_does_not_count(self):
        handed = self.at(hours=-2)
        token = self.submitted(handed=handed)
        application = self.application_of(token)
        self.mail(application, received=handed - timedelta(minutes=5, seconds=1), gmail_id="early")
        self.assertEqual(self.watch(), self.zero())
        self.mail(application, received=handed - timedelta(minutes=5), gmail_id="edge")
        self.assertEqual(self.watch(), self.zero(email_confirmed=1), "exactly five minutes before still counts")
        self.assertEqual(self.detail(token)["email_gmail_id"], "edge")

    def test_the_security_code_subject_is_ignored(self):
        token = self.submitted()
        fixture = json.loads((Path(__file__).resolve().parent / "fixtures" / "application_mail_eval.json").read_text(encoding="utf-8"))
        application = self.application_of(token)
        for item in fixture["security_code"]:
            self.mail(application, subject=item["subject"], received=self.at(minutes=-20), gmail_id=f"code-{item['id']}")
        self.mail(application, subject=SECURITY_CODE.upper(), received=self.at(minutes=-20), gmail_id="shouting")
        self.assertEqual(self.watch(), self.zero(), "a strong-tier, verified email about a security code is no confirmation, strong or weak")
        self.assertEqual(self.claim_row(token)["verification"], "awaiting_email")
        self.assertNotIn("possible_email_at", self.detail(token))

    def test_one_email_confirms_one_attempt(self):
        old = self.submitted(handed=self.at(hours=-5))
        application = self.application_of(old)
        # A released tombstone and a live attempt for the same application: the newest attempt gets the one email.
        with self.conn:
            self.conn.execute("UPDATE application_submit_claims SET state='released', verification='', watch_until=NULL, submitted_at=NULL WHERE token=?", (old,))
        live = self.raw_claim(state="submitted", mode="one_click", handed_over_at=iso(self.at(hours=-2)), submitted_at=iso(self.at(hours=-2)),
                              verification="awaiting_email", stage_recorded=1)
        with self.conn:
            self.conn.execute("UPDATE application_submit_claims SET application_id=?, opportunity_id=(SELECT opportunity_id FROM application_submit_claims WHERE token=?) WHERE token=?",
                              (application, old, live))
        self.mail(application, received=self.at(minutes=-30), gmail_id="the-one")
        self.assertEqual(self.watch(), self.zero(email_confirmed=1))
        self.assertEqual(self.claim_row(live)["verification"], "email_confirmed")
        self.assertEqual(self.claim_row(old)["state"], "released", "the one email is already spoken for")
        self.assertEqual(self.watch(), self.zero())

    def test_a_student_answer_meanwhile_wins(self):
        token = self.raw_claim(state="unconfirmed", mode="handoff", handed_over_at=iso(self.at(minutes=-30)), stage_policy="ask")
        self.mail(self.application_of(token), received=self.at(minutes=-10))
        real = apply_runs.lock_user
        answered = []

        def lock_then_the_student_answers(conn, user_id):
            real(conn, user_id)
            if not answered:
                answered.append(True)
                conn.execute("UPDATE application_submit_claims SET state='released', resolved_by='student' WHERE token=?", (token,))

        with mock.patch.object(apply_runs, "lock_user", lock_then_the_student_answers):
            counts = self.watch()
        self.assertEqual(counts, self.zero(), "the answer given first stands")
        self.assertEqual(self.claim_row(token)["state"], "released")
        self.assertEqual(self.notices(), [])


class LateAndNeverTests(WatchCase):
    def test_a_late_strong_match_turns_no_email_24h_into_email_confirmed(self):
        token = self.submitted(handed=self.at(hours=-30), until=self.at(hours=-6))
        self.reader(ok_at=self.at(-1))
        self.assertEqual(self.watch(), self.zero(no_email_24h=1))
        self.assertEqual(self.claim_row(token)["verification"], "no_email_24h")
        urgent_now = urgent.urgent_queue(self.conn, user_id=USER, now=self.at())
        self.assertIn(f"apply_no_email:{token}", [item["key"] for item in urgent_now["items"]])
        self.mail(self.application_of(token), received=self.at(minutes=-10))
        self.assertEqual(self.watch(1), self.zero(email_confirmed=1))
        self.assertEqual(self.claim_row(token)["verification"], "email_confirmed")
        gone = urgent.urgent_queue(self.conn, user_id=USER, now=self.at())
        self.assertNotIn(f"apply_no_email:{token}", [item["key"] for item in gone["items"]], "the Urgent row goes with the badge")

    def test_a_crash_in_clicking_then_an_email_resolves_it_with_the_stage_write(self):
        handed = self.at(minutes=-40)
        token = self.raw_claim(state="clicking", mode="one_click", handed_over_at=iso(handed), stage_policy="record")
        with self.conn:
            self.conn.execute("UPDATE application_submit_claims SET heartbeat_at=? WHERE token=?", (iso(self.at(minutes=-30)), token))
        self.assertEqual(apply_runs.recover_stale(self.conn, self.at())["unconfirmed"], 1)
        self.assertEqual(self.claim_row(token)["state"], "unconfirmed")
        received = handed + timedelta(minutes=3)
        self.mail(self.application_of(token), received=received)
        self.assertEqual(self.watch(), self.zero(resolved_by_email=1))
        row = self.claim_row(token)
        self.assertEqual((row["state"], row["verification"], row["resolved_by"], row["stage_recorded"]), ("submitted", "email_confirmed", "email", 1))
        self.assertEqual(parse_app_instant(row["submitted_at"]), received)
        self.assertIsNone(row["watch_until"])
        stage, applied_at = self.stage(row["opportunity_id"])
        self.assertEqual(stage, "applied")
        self.assertEqual(parse_app_instant(applied_at), received, "applied_at is the email's time")
        self.assertIn("Greenhouse confirmed your application to Bluefin Robotics by email", self.notices())
        [event] = self.event_rows(token, "apply_agent_resolved")
        self.assertEqual((event["resolved_by"], event["from_state"], event["source"]), ("email", "unconfirmed", "apply_agent:confirmation_email"))
        changes = [json.loads(item["detail_json"]) for item in self.conn.execute(
            "SELECT detail_json FROM application_events WHERE application_id=? AND event_type='stage_changed'", (row["application_id"],)).fetchall()]
        self.assertEqual([item.get("source") for item in changes], ["apply_agent:confirmation_email"])

    def test_phase_1_already_moved_the_stage_and_the_claim_is_reconciled(self):
        token = self.raw_claim(state="unconfirmed", mode="one_click", handed_over_at=iso(self.at(minutes=-40)), stage_policy="record")
        application = self.application_of(token)
        with self.conn:
            self.conn.execute("UPDATE applications SET stage='applied', applied_at=? WHERE id=?", (iso(self.at(minutes=-35)), application))
        self.mail(application, received=self.at(minutes=-35))
        self.assertEqual(self.watch(), self.zero(resolved_by_email=1))
        row = self.claim_row(token)
        self.assertEqual((row["state"], row["stage_recorded"]), ("submitted", 1), "reconciled rather than left may have been sent")
        self.assertEqual(self.events(application).count("stage_changed"), 0, "no second stage write")
        self.assertEqual(self.stage(row["opportunity_id"])[0], "applied")

    def test_an_ask_claim_resolved_by_email_waits_for_mark_as_applied(self):
        token = self.raw_claim(state="unconfirmed", mode="handoff", handed_over_at=iso(self.at(minutes=-40)), stage_policy="ask")
        self.mail(self.application_of(token), received=self.at(minutes=-35))
        self.assertEqual(self.watch(), self.zero(resolved_by_email=1))
        row = self.claim_row(token)
        self.assertEqual((row["state"], row["stage_recorded"]), ("submitted", 0))
        self.assertEqual(self.stage(row["opportunity_id"])[0], "applying", "the card asks")
        card = apply_watch.card_states(self.conn, USER)[row["application_id"]]
        self.assertTrue(card["ask_mark_applied"])
        self.assertEqual(card["status"], "email_confirmed")

    def test_a_released_tombstone_is_flipped_by_its_email_with_a_notice(self):
        token = self.raw_claim(state="released", mode="handoff", handed_over_at=iso(self.at(minutes=-40)), after_click=1, stage_policy="ask")
        self.mail(self.application_of(token), received=self.at(minutes=-35))
        self.assertEqual(self.watch(), self.zero(resolved_by_email=1))
        row = self.claim_row(token)
        self.assertEqual((row["state"], row["verification"], row["resolved_by"]), ("submitted", "email_confirmed", "email"))
        self.assertIn("Greenhouse confirmed your application to Bluefin Robotics by email", self.notices())

    def test_a_tombstone_whose_job_has_a_newer_live_attempt_is_not_flipped(self):
        tomb = self.raw_claim(state="released", mode="handoff", handed_over_at=iso(self.at(hours=-3)), after_click=1, job_ref="bluefin/same-job")
        live = self.raw_claim(state="needs_you", mode="handoff", job_ref="bluefin/same-job")
        self.mail(self.application_of(tomb), received=self.at(hours=-2))
        self.assertEqual(self.watch(), self.zero(held_back=1))
        self.assertEqual(self.claim_row(tomb)["state"], "released")
        self.assertEqual(self.claim_row(live)["state"], "needs_you")
        [notice] = [title for title in self.notices() if "after an attempt was marked as not sent" in title]
        self.assertIn("Bluefin Robotics", notice)
        self.assertEqual(self.watch(), self.zero(), "said once")

    def test_a_tombstone_waits_while_a_newer_retry_is_still_being_sent(self):
        """The retry's own confirmation can land while it is still clicking: no warning, and the retry takes the email."""
        tomb = self.raw_claim(state="released", mode="handoff", handed_over_at=iso(self.at(hours=-3)), after_click=1, job_ref="bluefin/same-job")
        for state in ("claimed", "clicking"):
            with self.subTest(state=state):
                with self.conn:
                    self.conn.execute("DELETE FROM application_submit_claims WHERE token<>?", (tomb,))
                self.raw_claim(state=state, mode="handoff", job_ref="bluefin/same-job", handed_over_at=iso(self.at(minutes=-10)) if state == "clicking" else None)
                self.mail(self.application_of(tomb), received=self.at(minutes=-5), gmail_id=f"retry-{state}")
                self.assertEqual(self.watch(), self.zero(), "nothing yet")
                self.assertEqual([title for title in self.notices() if "after an attempt was marked as not sent" in title], [])
                self.assertNotIn("email_after_release_at", self.detail(tomb))
                self.assertEqual(self.claim_row(tomb)["state"], "released")

    def test_a_unique_index_clash_while_flipping_is_held_back_not_raised(self):
        tomb = self.raw_claim(state="released", mode="handoff", handed_over_at=iso(self.at(hours=-3)), after_click=1, job_ref="bluefin/same-job")
        self.raw_claim(state="claimed", mode="handoff", job_ref="bluefin/same-job")
        self.mail(self.application_of(tomb), received=self.at(hours=-2))
        with mock.patch.object(apply_watch, "_newer_live_attempts", return_value=[]):
            counts = self.watch()
        self.assertEqual(counts, self.zero(held_back=1))
        self.assertEqual(self.claim_row(tomb)["state"], "released")


# --- The clock -------------------------------------------------------------------------------


class ClockTests(WatchCase):
    def expired(self, **kwargs):
        """A submission handed over 26 hours ago: its 24 hours ended 1 hour ago... give or take a minute."""
        return self.submitted(handed=self.at(hours=-26), until=self.at(hours=-2), **kwargs)

    def test_no_email_after_24_hours_with_the_reader_healthy_gives_no_email_24h_and_a_notice(self):
        token = self.expired()
        self.assertEqual(self.watch(), self.zero(no_email_24h=1))
        row = self.claim_row(token)
        self.assertEqual(row["verification"], "no_email_24h")
        [event] = self.event_rows(token, "apply_agent_verification")
        self.assertEqual((event["verification"], event["source"]), ("no_email_24h", "apply_agent:watch"))
        [notice] = [item for item in automation.list_notices(self.conn, USER) if item["title"].startswith("No confirmation email yet")]
        self.assertEqual(notice["title"], "No confirmation email yet for your application to Controls Intern at Bluefin Robotics")
        self.assertIn("Some employers don't send one", notice["body"])
        queue = urgent.urgent_queue(self.conn, user_id=USER, now=self.at())
        found = {item["key"]: item for item in queue["items"]}
        self.assertIn(f"apply_no_email:{token}", found)
        self.assertEqual(self.claim_row(token)["stage_recorded"], 1, "never touched: the submission itself still stands")

    def test_a_stalled_reader_pauses_the_clock_and_extends_the_window(self):
        stalls = {
            "stale pass": dict(ok_at=self.at(minutes=-31)),
            "backlog": dict(pending=["gm-queued"]),
            "recovery": dict(recovery="running"),
            "error": dict(error="Gmail could not be reached"),
            "slow down": dict(error="Gmail asked the app to slow down"),
            "reconnect": dict(error="Gmail needs reconnecting"),
            "never ok": dict(ok_at=None),
            "not started": dict(history=""),
            "switch off": dict(mode="off"),
            "not connected": dict(status=None),
            "needs reconnect": dict(status="error"),
        }
        for name, stall in stalls.items():
            with self.subTest(stall=name):
                with self.conn:
                    self.conn.execute("DELETE FROM application_submit_claims")
                token = self.expired()
                self.reader(**stall)
                self.assertEqual(self.watch(), self.zero(paused=1))
                row = self.claim_row(token)
                self.assertEqual(row["verification"], "awaiting_email", "a stalled reader decides nothing")
                self.assertTrue(self.detail(token)["watch_paused"])
                self.assertEqual(apply_watch.card_states(self.conn, USER)[row["application_id"]]["status"], "watch_paused")
                self.assertEqual(self.watch(), self.zero(), "paused once")

    def test_the_stall_is_added_to_the_deadline_and_no_email_24h_comes_only_after_it(self):
        token = self.submitted(handed=self.at(hours=-23), until=self.at(minutes=10))
        self.reader(error="Gmail could not be reached", ok_at=self.at(minutes=-5))
        self.assertEqual(self.watch(), self.zero(paused=1))
        self.assertEqual(self.detail(token)["watch_paused"], apply_watch.READER_FAILING)
        # The reader recovers 30 minutes later: the clock was stopped for the stall, so 10 minutes left become 40.
        self.reader(ok_at=self.at(minutes=29))
        self.assertEqual(self.watch(30), self.zero(extended=1))
        row = self.claim_row(token)
        self.assertEqual(parse_app_instant(row["watch_until"]), self.at(minutes=40))
        self.assertNotIn("watch_paused_since", self.detail(token))
        self.assertEqual(row["verification"], "awaiting_email")
        # The deadline passed, but the reader has not finished a pass that began after it: wait, write nothing.
        self.reader(ok_at=self.at(minutes=35))
        before = self.claim_row(token)
        self.assertEqual(self.watch(50), self.zero())
        self.assertEqual(self.claim_row(token), before, "a healthy reader that has not passed the deadline is waited for")
        self.reader(ok_at=self.at(minutes=41))
        self.assertEqual(self.watch(50), self.zero(no_email_24h=1))

    def test_a_healthy_reader_that_has_not_passed_the_deadline_is_waited_for(self):
        token = self.expired()
        self.reader(ok_at=self.at(hours=-3))
        # last_ok is 3 hours old, so the reader is idle: that is a stall, not a wait; make it recent but before the deadline.
        self.reader(ok_at=self.at(hours=-2, minutes=-1))
        with mock.patch.object(apply_watch, "READER_STALE", timedelta(hours=5)):
            self.assertEqual(self.watch(), self.zero())
            self.assertEqual(self.claim_row(token)["verification"], "awaiting_email")
            self.reader(ok_at=self.at(minutes=-1))
            self.assertEqual(self.watch(), self.zero(no_email_24h=1))

    def test_a_watch_stalled_for_almost_14_days_stops_as_not_watched(self):
        token = self.submitted(handed=self.at(days=-13, hours=-2), until=self.at(days=-12, hours=-2))
        self.reader(status="error")
        self.assertEqual(self.watch(), self.zero(stopped=1))
        row = self.claim_row(token)
        self.assertEqual(row["verification"], "not_watched")
        detail = self.detail(token)
        self.assertEqual(detail["watch_stopped"], "reader_stalled")
        self.assertNotIn("watch_paused_since", detail)
        self.assertEqual(self.notices(), [], "no notice: the card says the app isn't checking")
        self.assertEqual(apply_watch.ats_statistics(self.conn, USER)["recent"]["finished"], 0, "a stalled watch never counts for 8.8")

    def test_a_strong_match_beats_a_stalled_reader(self):
        token = self.expired()
        self.reader(error="Gmail could not be reached")
        self.mail(self.application_of(token), received=self.at(hours=-20))
        self.assertEqual(self.watch(), self.zero(email_confirmed=1), "mail already read is still mail")


# --- D12 and the worker -----------------------------------------------------------------------


class AvailabilityTests(WatchCase):
    def test_not_watched_when_the_watch_is_not_required_and_not_available(self):
        update_profile(self.conn, {"contact": {"email": ""}}, ["contact"], user_id=USER)
        self.assertEqual(apply_watch.watch_available(self.conn, USER), apply_watch.WATCH_OTHER_ADDRESS, "no confirmed email yet")
        update_profile(self.conn, {"contact": {"email": EMAIL.upper()}}, ["contact"], user_id=USER)
        self.assertEqual(apply_watch.watch_available(self.conn, USER), "", "the address matches, whatever its case")
        steps = [
            (dict(mode="off"), apply_watch.WATCH_NEEDS_SWITCH),
            (dict(status=None), apply_watch.WATCH_NEEDS_GMAIL),
            (dict(status="error"), apply_watch.WATCH_NEEDS_GMAIL),
            (dict(account=""), apply_watch.WATCH_NEEDS_ADDRESS),
            (dict(account="someone.else@example.test"), apply_watch.WATCH_OTHER_ADDRESS),
            (dict(mode="on"), ""),
            (dict(), ""),
        ]
        for kwargs, expected in steps:
            with self.subTest(kwargs=kwargs):
                self.reader(**kwargs)
                self.assertEqual(apply_watch.watch_available(self.conn, USER), expected)
        self.reader(mode="off")
        self.assertEqual([apply_watch.watch_for(self.conn, USER, mode) for mode in ("one_click", "unattended", "handoff")], [True, True, False])
        self.reader()
        self.assertEqual([apply_watch.watch_for(self.conn, USER, mode) for mode in ("one_click", "unattended", "handoff")], [True, True, True])
        self.reader(mode="off")
        token = self.raw_claim(state="clicking", mode="handoff", handed_over_at=iso(self.at(minutes=-1)), stage_policy="ask")
        self.assertTrue(apply_runs.settle(self.conn, token, user_id=USER, state="submitted", confirmation_seen=True,
                                          watch=apply_watch.watch_for(self.conn, USER, "handoff"), now=self.at()))
        row = self.claim_row(token)
        self.assertEqual((row["verification"], row["watch_until"]), ("not_watched", None))
        card = apply_watch.card_states(self.conn, USER)[row["application_id"]]
        self.assertEqual(card["status"], "not_watched")

    def test_reader_health_names_its_reason(self):
        self.assertEqual(apply_watch.reader_health(self.conn, USER, self.at())[0], "")
        self.reader(mode="off")
        self.assertEqual(apply_watch.reader_health(self.conn, USER, self.at())[0], apply_watch.READER_OFF)
        self.reader(ok_at=self.at(minutes=-29))
        self.assertEqual(apply_watch.reader_health(self.conn, USER, self.at())[0], "", "inside the allowed gap")
        self.reader(ok_at=self.at(minutes=-31))
        self.assertEqual(apply_watch.reader_health(self.conn, USER, self.at())[0], apply_watch.READER_IDLE)


class WorkerTests(WatchCase):
    def worker(self):
        return AutomationWorker(self.path, fetcher_factory=lambda: None, apply_root=None)

    def test_the_worker_step_runs_recover_stale_then_watch_for_a_student_with_apply_for_me_off(self):
        self.assertEqual(automation.mode(self.conn, USER, "apply_agent"), "off")
        token = self.submitted()
        self.mail(self.application_of(token), received=self.at(minutes=-30))
        report = self.worker().run_once()
        self.assertEqual(report["apply"]["watched"], {USER: self.zero(email_confirmed=1)})
        self.assertEqual(self.claim_row(token)["verification"], "email_confirmed")
        health = {row["component"]: row for row in automation_health.health_summary(self.conn, USER)["components"]}
        self.assertEqual(health["apply_agent.watch"]["last_error"], "")
        self.assertNotIn("apply", self.worker().run_once(), "nothing left to change: the pass reports nothing")

    def test_a_watch_that_raises_records_its_component_and_the_next_student_still_runs(self):
        self.submitted()
        seen = []

        def failing(conn, user_id, now):
            seen.append(user_id)
            raise RuntimeError("the database went away")

        report = apply_runs.run_worker_step(self.conn, watch=failing, now=self.at())
        self.assertEqual((seen, report["watched"]), ([USER], {}))
        row = self.conn.execute("SELECT last_error FROM automation_health WHERE user_id=? AND component='apply_agent.watch'", (USER,)).fetchone()
        self.assertIn("the database went away", row["last_error"])
        runner = self.conn.execute("SELECT last_error_at FROM automation_health WHERE user_id=? AND component='apply_agent.runner'", (USER,)).fetchone()
        self.assertIsNone(runner["last_error_at"], "the runner's status is not mixed with the watch's")
        apply_runs.run_worker_step(self.conn, watch=lambda conn, user_id, now: {}, now=self.at())
        row = self.conn.execute("SELECT last_ok_at, last_error_at FROM automation_health WHERE user_id=? AND component='apply_agent.watch'", (USER,)).fetchone()
        self.assertTrue(row["last_ok_at"] > row["last_error_at"], "the next good pass clears the Error chip")

    def test_the_watch_runs_even_when_recovery_raised(self):
        self.submitted()
        seen = []
        with mock.patch.object(apply_runs, "recover_stale", side_effect=RuntimeError("the ledger went away")):
            report = apply_runs.run_worker_step(self.conn, watch=lambda conn, user_id, now: seen.append(user_id) or {}, now=self.at())
        self.assertEqual((seen, report["recovered"]), ([USER], {}), "one claim failing to recover must not stop the watch")
        rows = {row["component"]: row for row in self.conn.execute("SELECT component, last_error, last_ok_at FROM automation_health WHERE user_id=?", (USER,)).fetchall()}
        self.assertIn("the ledger went away", rows["apply_agent.runner"]["last_error"], "the recovery failure is still recorded")
        self.assertEqual(rows["apply_agent.watch"]["last_error"], "")
        self.assertTrue(rows["apply_agent.watch"]["last_ok_at"])

    def test_without_a_watch_the_step_only_recovers(self):
        self.submitted()
        report = apply_runs.run_worker_step(self.conn, now=self.at())
        self.assertEqual(report["watched"], {})


class HonestClockTests(WatchCase):
    """no_email_24h means the reader looked, in the right mailbox, and no email came; anything less pauses or stops the watch."""

    def expired(self, **kwargs):
        return self.submitted(handed=self.at(hours=-26), until=self.at(hours=-2), **kwargs)

    def aside(self, received):
        """An email the reader set aside unread (Phase 1 state 'error'): no subject, no sender, recorded when it was read."""
        return self.mail("", kind="", state="error", subject="", domain="", matched_by="", verified=0, received=received, linked=False)

    def test_an_email_set_aside_unread_in_the_window_pauses_the_watch_instead_of_ending_it(self):
        token = self.expired()
        self.aside(self.at(hours=-20))
        self.assertEqual(self.watch(), self.zero(paused=1))
        row = self.claim_row(token)
        self.assertEqual(row["verification"], "awaiting_email", "an email did arrive that the reader never read: not 'no email came'")
        self.assertEqual(self.detail(token)["watch_paused"], apply_watch.READER_SET_ASIDE)
        self.assertEqual([title for title in self.notices() if title.startswith("No confirmation email yet")], [])
        self.assertEqual(apply_watch.ats_statistics(self.conn, USER)["no_email_24h"], 0)

    def test_an_email_set_aside_long_before_the_hand_over_does_not_stall(self):
        token = self.expired()
        self.aside(self.at(hours=-30))
        self.assertEqual(self.watch(), self.zero(no_email_24h=1))
        self.assertEqual(self.claim_row(token)["verification"], "no_email_24h")

    def test_a_gmail_account_that_is_not_the_applications_address_pauses_the_watch(self):
        token = self.expired()
        self.reader(account="someone.else@example.test")
        self.assertEqual(self.watch(), self.zero(paused=1))
        self.assertEqual(self.claim_row(token)["verification"], "awaiting_email", "the app is not reading the mailbox the email went to")
        self.assertEqual(self.detail(token)["watch_paused"], apply_watch.READER_OTHER_ADDRESS)
        self.reader(account="")
        self.watch()
        self.assertEqual(self.detail(token)["watch_paused"], apply_watch.READER_UNKNOWN_ADDRESS)
        self.reader()
        self.assertEqual(self.watch()["extended"], 1, "back on the right account: the stall is added to the deadline")

    def test_a_stall_that_would_push_the_deadline_past_the_14_days_ends_the_watch_as_not_watched(self):
        handed = self.at(days=-12, hours=-23, minutes=-30)  # 12.98 days ago
        token = self.submitted(handed=handed, until=handed + timedelta(hours=24, minutes=1),
                               detail={"watch_paused_since": iso(handed + timedelta(hours=1)), "watch_paused": apply_watch.READER_FAILING})
        self.assertEqual(self.watch(), self.zero(stopped=1))
        row = self.claim_row(token)
        self.assertEqual(row["verification"], "not_watched", "it would otherwise sit 'watching' past the day it can still be selected")
        detail = self.detail(token)
        self.assertEqual((detail["watch_stopped"], detail["watch_stopped_reason"]), ("reader_stalled", apply_watch.READER_FAILING))
        self.assertNotIn("watch_paused_since", detail)
        self.assertEqual(apply_watch.ats_statistics(self.conn, USER)["recent"]["finished"], 0, "never counts")

    def test_a_submission_still_awaiting_its_email_after_14_days_is_ended_not_left_watching(self):
        token = self.submitted(handed=self.at(days=-15), until=self.at(days=-14))
        self.assertEqual(apply_runs.students_to_watch(self.conn, self.at()), [USER], "the worker still comes for it")
        self.assertEqual(self.watch(), self.zero(stopped=1))
        self.assertEqual(self.claim_row(token)["verification"], "not_watched")
        self.assertEqual(self.detail(token)["watch_stopped"], "window_ended")
        card = apply_watch.card_states(self.conn, USER)[self.application_of(token)]
        self.assertEqual(card["status"], "not_watched")
        stats = apply_watch.ats_statistics(self.conn, USER)
        self.assertEqual((stats["watching"], stats["watch_paused"], stats["not_watched"]), (0, 0, 1))
        self.assertEqual(apply_runs.students_to_watch(self.conn, self.at()), [], "nothing left to watch")
        self.assertEqual(self.watch(), self.zero())
        # A claim that already finished its watch is left as it is.
        silent = self.submitted(handed=self.at(days=-15), verification="no_email_24h")
        self.assertEqual(self.watch(), self.zero())
        self.assertEqual(self.claim_row(silent)["verification"], "no_email_24h")

    def test_the_five_minute_floor_is_compared_at_the_precision_the_reader_stores(self):
        handed = self.at(hours=-2).replace(microsecond=500000)
        token = self.submitted(handed=handed)
        # Gmail stamped the email 0.9 s after the floor's whole second; Phase 1 stores it to the second, below the floor.
        stored = (handed - timedelta(minutes=5)).replace(microsecond=0)
        self.assertLess(stored, handed - timedelta(minutes=5))
        self.mail(self.application_of(token), received=stored)
        self.assertEqual(self.watch(), self.zero(email_confirmed=1))
        # Still a floor: a second earlier is outside it.
        other = self.submitted(handed=handed)
        self.mail(self.application_of(other), received=stored - timedelta(seconds=1))
        self.assertEqual(self.watch(), self.zero(), "the other claim's email is too early")


class FinishInBrowserWatchTests(WatchCase):
    """D12 C on the student's answer: Finish in browser watches only when the watch is available; one-click always does."""

    def resolve(self, claim_mode, **reader):
        with self.conn:
            self.conn.execute("DELETE FROM application_submit_claims")
        token = self.raw_claim(state="unconfirmed", mode=claim_mode, handed_over_at=iso(self.at(minutes=-40)), stage_policy="ask")
        self.reader(**reader)
        card = apply_watch.resolve_by_student(self.conn, token, user_id=USER, went_through=True, now=self.at())
        return card["verification"], self.claim_row(token)["watch_until"] is not None

    def test_the_answer_watches_by_the_mode_and_the_availability(self):
        cases = [
            ("handoff", dict(), ("awaiting_email", True)),
            ("handoff", dict(mode="off"), ("not_watched", False)),
            ("handoff", dict(account="someone.else@example.test"), ("not_watched", False)),
            ("handoff", dict(status=None), ("not_watched", False)),
            ("one_click", dict(mode="off"), ("awaiting_email", True)),
            ("one_click", dict(account="someone.else@example.test"), ("awaiting_email", True)),
            ("unattended", dict(status=None), ("awaiting_email", True)),
        ]
        for mode, reader, expected in cases:
            with self.subTest(mode=mode, reader=reader):
                self.assertEqual(self.resolve(mode, **reader), expected)

    def test_a_failed_timeline_write_still_returns_the_recorded_answer(self):
        token = self.raw_claim(state="unconfirmed", mode="handoff", handed_over_at=iso(self.at(minutes=-40)), stage_policy="ask")
        with mock.patch.object(actions, "log_application_event", side_effect=sqlite3.OperationalError("database is locked")), \
                self.assertLogs(apply_watch.LOGGER, "ERROR"):
            card = apply_watch.resolve_by_student(self.conn, token, user_id=USER, went_through=False, now=self.at())
        self.assertEqual((card["state"], self.claim_row(token)["state"]), ("released", "released"), "the answer is recorded, not an error")


class NoValuesTests(WatchCase):
    def test_notices_and_events_hold_no_subject_or_gmail_id(self):
        subject = "Thank you for applying to Bluefin Robotics (req 8841-secret)"
        token = self.raw_claim(state="unconfirmed", mode="handoff", handed_over_at=iso(self.at(minutes=-40)), stage_policy="ask")
        gmail_id = self.mail(self.application_of(token), received=self.at(minutes=-30), subject=subject, gmail_id="gmail-id-77f3")
        self.assertEqual(self.watch(), self.zero(resolved_by_email=1))
        expired = self.submitted(handed=self.at(hours=-26), until=self.at(hours=-2))
        self.assertEqual(self.watch(), self.zero(no_email_24h=1))
        texts = [json.dumps(automation.list_notices(self.conn, USER, limit=50))]
        for application in (self.application_of(token), self.application_of(expired)):
            texts += [row["detail_json"] for row in self.conn.execute("SELECT detail_json FROM application_events WHERE application_id=?", (application,)).fetchall()]
        text = "\n".join(texts)
        for forbidden in (subject, "8841", gmail_id, "http", "@"):
            self.assertNotIn(forbidden, text)
        self.assertIn(gmail_id, self.claim_row(token)["detail_json"], "the id lives on the claim, which is exported and deleted with the account")


# --- Card states and statistics -----------------------------------------------------------------


class CardTests(WatchCase):
    def test_a_held_handoff_claim_waiting_for_the_security_code_is_not_called_submitting(self):
        waiting = {"waiting": "security_code", "security_code_reader": {"reader": "fallback", "prompted_at": iso(self.at(minutes=-1))}}
        asked = self.raw_claim(state="clicking", mode="handoff", handed_over_at=iso(self.at(minutes=-2)), detail=waiting)
        pressed = self.raw_claim(state="clicking", mode="handoff", handed_over_at=iso(self.at(minutes=-2)), detail={"waiting": ""})
        other = self.raw_claim(state="clicking", mode="one_click", handed_over_at=iso(self.at(minutes=-2)), detail=waiting)
        stale = self.raw_claim(state="clicking", mode="handoff", handed_over_at=iso(self.at(minutes=-30)), detail=waiting,
                               instance="another-process", heartbeat_at=iso(self.at(minutes=-30)))
        for token in (asked, pressed, other):
            apply_claims.RUNNING.add(token)
            self.addCleanup(apply_claims.RUNNING.discard, token)
        states = {state["token"]: state for state in apply_watch.card_states(self.conn, USER, now=self.at()).values()}
        self.assertEqual(
            {token: states[token]["status"] for token in (asked, pressed, other, stale)},
            {asked: "security_code", pressed: "submitting", other: "submitting", stale: "may_have_been_sent"},
        )
        script = (Path(__file__).resolve().parents[1] / "opportunity_app" / "static" / "app-applications.js").read_text(encoding="utf-8")
        self.assertIn('apply.status === "security_code"', script, "the tracker card has words for the status")
        self.assertIn('["filling", "your_turn", "security_code"].includes(apply.status)', script, "and offers Open, as for the other turns of the window")

    def test_card_states_cover_every_10_5_status(self):
        held = self.raw_claim(state="clicking", mode="one_click", handed_over_at=iso(self.at(minutes=-1)))
        apply_claims.RUNNING.add(held)
        stale = self.raw_claim(state="clicking", mode="one_click", handed_over_at=iso(self.at(minutes=-30)),
                               instance="another-process", heartbeat_at=iso(self.at(minutes=-30)))
        unconfirmed = self.raw_claim(state="unconfirmed", mode="handoff", handed_over_at=iso(self.at(minutes=-30)))
        needs_after = self.raw_claim(state="needs_you", mode="handoff", handed_over_at=iso(self.at(minutes=-30)), after_click=1)
        stopped = self.raw_claim(state="needs_you", mode="handoff", after_click=0, note="Add your phone number first")
        watching = self.submitted()
        paused = self.submitted(detail={"watch_paused_since": iso(self.at(minutes=-5)), "watch_paused": apply_watch.READER_RECONNECT})
        confirmed = self.submitted(verification="email_confirmed", detail={"email_received_at": iso(self.at(minutes=-20))})
        silent = self.submitted(verification="no_email_24h")
        unwatched = self.submitted(verification="not_watched")
        asking = self.submitted(mode="handoff", policy="ask", recorded=0, detail={"possible_email_at": iso(self.at(minutes=-9))})
        with self.conn:  # the "ask" claim's application is still applying
            self.conn.execute("UPDATE applications SET stage='applying' WHERE id=?", (self.application_of(asking),))
        self.raw_claim(state="released", mode="handoff", handed_over_at=iso(self.at(-3)), after_click=1)
        states = apply_watch.card_states(self.conn, USER, now=self.at())
        by_token = {state["token"]: state for state in states.values()}
        self.assertEqual({token: by_token[token]["status"] for token in by_token}, {
            held: "submitting", stale: "may_have_been_sent", unconfirmed: "may_have_been_sent", needs_after: "may_have_been_sent",
            stopped: "stopped", watching: "watching", paused: "watch_paused", confirmed: "email_confirmed", silent: "no_email",
            unwatched: "not_watched", asking: "watching",
        }, "a released tombstone has no card")
        self.assertEqual({token: by_token[token]["can_resolve"] for token in (held, stale, unconfirmed, needs_after, stopped, watching)},
                         {held: False, stale: True, unconfirmed: True, needs_after: True, stopped: False, watching: False})
        self.assertEqual(by_token[stopped]["note"], "Add your phone number first")
        self.assertEqual(by_token[paused]["paused_reason"], apply_watch.READER_RECONNECT)
        self.assertEqual(by_token[confirmed]["email_received_at"], iso(self.at(minutes=-20)))
        self.assertTrue(by_token[asking]["ask_mark_applied"])
        self.assertEqual(by_token[asking]["possible_email_at"], iso(self.at(minutes=-9)))
        self.assertFalse(by_token[watching]["ask_mark_applied"])
        self.assertEqual(by_token[watching]["ats_name"], "Greenhouse")
        # Only while the application is still applying.
        with self.conn:
            self.conn.execute("UPDATE applications SET stage='applied' WHERE id=?", (self.application_of(asking),))
        self.assertFalse({state["token"]: state for state in apply_watch.card_states(self.conn, USER).values()}[asking]["ask_mark_applied"])


class StatisticsTests(WatchCase):
    def test_ats_statistics_count_submissions_prompts_emails_and_never_a_stalled_watch(self):
        empty = apply_watch.ats_statistics(self.conn, USER)
        self.assertEqual((empty["handed_over"], empty["lines"]), (0, ["No applications submitted with Apply for me on Greenhouse yet."]))
        prompted = {"security_code_reader": {"prompted_at": iso(self.at(-100)), "reader": "typed"}}
        self.submitted(verification="email_confirmed", handed=self.at(hours=-10), detail=prompted)
        self.submitted(verification="email_confirmed", handed=self.at(hours=-9))
        self.submitted(verification="no_email_24h", handed=self.at(hours=-8), detail={"security_code_reader": {"prompted_at": iso(self.at(-90)), "reader": "fallback"}})
        self.submitted(verification="awaiting_email", handed=self.at(hours=-7))
        self.submitted(verification="awaiting_email", handed=self.at(hours=-6), detail={"watch_paused_since": iso(self.at(-10)), "watch_paused": "x"})
        self.submitted(verification="not_watched", handed=self.at(hours=-5))
        self.raw_claim(state="unconfirmed", mode="handoff", handed_over_at=iso(self.at(hours=-4)), detail={"security_code_reader": {"prompted_at": iso(self.at(-80)), "reader": "waiting"}})
        self.raw_claim(state="failed", mode="handoff", after_click=0)  # never handed over: counts nowhere
        stats = apply_watch.ats_statistics(self.conn, USER)
        self.assertEqual({key: stats[key] for key in ("handed_over", "submitted", "email_confirmed", "no_email_24h", "watching", "watch_paused", "not_watched",
                                                       "security_code_prompts", "security_code_typed")},
                         {"handed_over": 7, "submitted": 6, "email_confirmed": 2, "no_email_24h": 1, "watching": 1, "watch_paused": 1, "not_watched": 1,
                          "security_code_prompts": 3, "security_code_typed": 1})
        self.assertEqual(stats["recent"], {"window": 10, "finished": 3, "no_email_24h": 1, "security_code_prompts": 2},
                         "awaiting (running or paused) and not_watched watches never count")
        self.assertEqual(stats["lines"], [
            "Greenhouse: 7 applications handed over, 6 submitted.",
            "Confirmation emails: 2 arrived, 1 didn't come within 24 hours, 2 still being looked for.",
            "Security codes: Greenhouse asked for a code 3 times; the app typed it 1 time.",
            "Of your last 3 submissions whose email watch finished, 1 got no confirmation email and 2 asked for a security code.",
        ])

    def test_the_lines_read_right_with_one(self):
        self.submitted(verification="email_confirmed")
        lines = apply_watch.ats_statistics(self.conn, USER)["lines"]
        self.assertEqual(lines[0], "Greenhouse: 1 application handed over, 1 submitted.")
        self.assertEqual(lines[2], "Security codes: Greenhouse asked for a code 0 times; the app typed it 0 times.")
        self.assertEqual(lines[3], "Of your last 1 submission whose email watch finished, 0 got no confirmation email and 0 asked for a security code.")

    def test_the_recent_window_is_the_last_ten_finished_watches(self):
        for index in range(12):
            self.submitted(verification="no_email_24h" if index < 2 else "email_confirmed", handed=self.at(hours=-30 + index))
        recent = apply_watch.ats_statistics(self.conn, USER)["recent"]
        self.assertEqual((recent["finished"], recent["no_email_24h"]), (10, 0), "the two oldest fell out of the window")


# --- The card's routes -----------------------------------------------------------------------------


class CardRouteCase(api_tests.ApplyApiCase):
    BASE = "/api/v1/apply-agent"

    def setUp(self):
        super().setUp()
        self.browser = self.enterContext(api_tests.TestClient(self.app))
        signed = self.browser.post("/api/v1/session", json={"token": api_tests.TOKEN})
        self.assertEqual(signed.status_code, 200, signed.text)
        self.csrf = {"X-CSRF-Token": self.browser.cookies.get("pipeline_csrf")}
        self.serial = 0

    def send(self, method, path, body=None):
        return self.browser.request(method, path, json=body, headers=self.csrf)

    def claim(self, *, state="unconfirmed", mode="handoff", policy="ask", verification="", stage="applying", job="job-a", after_click=1,
              handed=None, instance=SERVER_INSTANCE):
        """A claim on the seeded Acme application (created here), written directly."""
        self.serial += 1
        now = utc_now()
        handed = handed or iso(datetime.now(timezone.utc) - timedelta(minutes=30))
        with self.conn:
            self.conn.execute(
                "INSERT INTO applications(id, opportunity_id, user_id, stage, created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET stage=excluded.stage", (f"app-{job}", job, api_tests.USER, stage, now, now),
            )
            self.conn.execute(
                "INSERT INTO application_submit_claims(token, application_id, user_id, opportunity_id, instance, mode, state, after_click, ats, board_token, "
                "job_ref, company_key, stage_policy, plan_hash, handed_over_at, heartbeat_at, verification, submitted_at, created_at, updated_at) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, 'greenhouse', 'acme', ?, 'acme robotics', ?, '', ?, ?, ?, ?, ?, ?)",
                (f"tok-{self.serial}", f"app-{job}", api_tests.USER, job, instance, mode, state, after_click, f"acme/{self.serial}", policy,
                 handed, handed, verification, handed if state == "submitted" else None, now, now),
            )
        return f"tok-{self.serial}"

    def row(self, token):
        return dict(self.conn.execute("SELECT * FROM application_submit_claims WHERE token=?", (token,)).fetchone())


class CardRouteTests(CardRouteCase):
    def test_resolve_and_mark_applied_need_the_browser_session(self):
        token = self.claim()
        routes = (("POST", f"{self.BASE}/claims/{token}/resolve", {"went_through": True}), ("POST", f"{self.BASE}/claims/{token}/mark-applied", None))
        for method, path, body in routes:
            with self.subTest(route=path):
                bearer = self.client.request(method, path, json=body, headers=api_tests.AUTH)
                self.assertEqual(bearer.status_code, 403, bearer.text)
                both = self.browser.request(method, path, json=body, headers={**api_tests.AUTH, **self.csrf})
                self.assertEqual(both.status_code, 403, both.text)
                bare = self.browser.request(method, path, json=body)
                self.assertEqual((bare.status_code, bare.json()["detail"]), (403, "CSRF validation failed"))
                self.assertEqual(self.client.request(method, path, json=body).status_code, 401)
        self.assertEqual(self.row(token)["state"], "unconfirmed")

    def test_it_went_through_and_it_did_not_go_through_from_the_card(self):
        went = self.claim()
        response = self.send("POST", f"{self.BASE}/claims/{went}/resolve", {"went_through": True})
        self.assertEqual(response.status_code, 200, response.text)
        card = response.json()["claim"]
        self.assertEqual((card["state"], card["resolved_by"], card["status"]), ("submitted", "student", "watching" if card["verification"] == "awaiting_email" else "not_watched"))
        self.assertEqual(self.row(went)["state"], "submitted")
        self.assertIn("apply_agent_submitted", [row["event_type"] for row in self.conn.execute("SELECT event_type FROM application_events WHERE application_id='app-job-a'")])

        # An attempt that did not go through becomes a tombstone, with its own event.
        with self.conn:
            self.conn.execute("DELETE FROM application_submit_claims")
        denied = self.claim()
        response = self.send("POST", f"{self.BASE}/claims/{denied}/resolve", {"went_through": False})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.row(denied)["state"], "released")
        events = [json.loads(row["detail_json"]) for row in self.conn.execute("SELECT detail_json FROM application_events WHERE event_type='apply_agent_resolved'")]
        self.assertEqual([(item["resolved_by"], item["went_through"], item["source"]) for item in events], [("student", False, "apply_agent:student_confirmed")])

    def test_the_answers_are_refused_while_held_when_not_uncertain_and_for_an_unknown_attempt(self):
        held = self.claim(state="clicking", job="job-a")
        apply_claims.RUNNING.add(held)
        self.addCleanup(apply_claims.RUNNING.discard, held)
        response = self.send("POST", f"{self.BASE}/claims/{held}/resolve", {"went_through": True})
        self.assertEqual((response.status_code, response.json()["detail"]), (409, "This application is still being submitted"))
        with self.conn:
            self.conn.execute("DELETE FROM application_submit_claims")
        done = self.claim(state="submitted", verification="not_watched", after_click=1)
        response = self.send("POST", f"{self.BASE}/claims/{done}/resolve", {"went_through": False})
        self.assertEqual((response.status_code, response.json()["detail"]), (409, "This attempt does not need an answer"))
        self.assertEqual(self.row(done)["state"], "submitted")
        missing = self.send("POST", f"{self.BASE}/claims/nope/resolve", {"went_through": True})
        self.assertEqual((missing.status_code, missing.json()["detail"]), (404, "This attempt was not found"))
        self.assertEqual(self.send("POST", f"{self.BASE}/claims/nope/mark-applied").status_code, 404)

    def test_mark_as_applied_moves_an_ask_claim_forward_only(self):
        token = self.claim(state="submitted", policy="ask", verification="not_watched", after_click=1)
        response = self.send("POST", f"{self.BASE}/claims/{token}/mark-applied")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.row(token)["stage_recorded"], 1)
        stage = self.conn.execute("SELECT stage FROM applications WHERE id='app-job-a'").fetchone()[0]
        self.assertEqual(stage, "applied")
        changed = [json.loads(row["detail_json"]) for row in self.conn.execute("SELECT detail_json FROM application_events WHERE event_type='stage_changed'")]
        self.assertEqual(changed[-1]["source"], "apply_agent:student_confirmed")
        again = self.send("POST", f"{self.BASE}/claims/{token}/mark-applied")
        self.assertEqual((again.status_code, again.json()["detail"]), (409, "This attempt has nothing to mark"))
        # Forward only: a stage the student already moved past applying is left alone, but the claim is settled.
        with self.conn:
            self.conn.execute("DELETE FROM application_submit_claims")
        other = self.claim(state="submitted", policy="ask", verification="not_watched", stage="interview")
        self.assertEqual(self.send("POST", f"{self.BASE}/claims/{other}/mark-applied").status_code, 200)
        self.assertEqual(self.conn.execute("SELECT stage FROM applications WHERE id='app-job-a'").fetchone()[0], "interview")
        # Only an 'ask' claim has anything to mark.
        with self.conn:
            self.conn.execute("DELETE FROM application_submit_claims")
        recorded = self.claim(state="submitted", policy="record", verification="not_watched")
        self.assertEqual(self.send("POST", f"{self.BASE}/claims/{recorded}/mark-applied").status_code, 409)

    def test_the_applications_list_carries_each_cards_apply_state(self):
        before = self.get("/api/v1/applications").json()["items"]
        self.assertTrue(all(item["apply"] is None for item in before))
        token = self.claim()
        items = {item["id"]: item for item in self.get("/api/v1/applications").json()["items"]}
        card = items["app-job-a"]["apply"]
        self.assertEqual((card["token"], card["status"], card["can_resolve"]), (token, "may_have_been_sent", True))
        export = self.get("/api/v1/applications/export?format=json").text
        self.assertNotIn(token, export, "the export is untouched")

    def test_the_settings_carry_the_statistics(self):
        stats = self.get("/api/v1/apply-agent/settings").json()["ats_statistics"]
        self.assertEqual([item["ats"] for item in stats], ["greenhouse"])
        self.assertEqual(stats[0]["lines"], ["No applications submitted with Apply for me on Greenhouse yet."])


if __name__ == "__main__":
    unittest.main()
