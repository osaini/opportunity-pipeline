"""Apply for me's data layer (apply_runs.py, migration 0045): claims and their two locks, retry, hand-over, heartbeat,
recovery, limits, the rehearsal gate, the readers that learn about claims, the worker step, retention, deletion, export.

No browser and nothing that reaches a network: every company, board and posting here is fictional.
"""

import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from opportunity_app import SERVER_INSTANCE, actions, apply_runs, automation, schema, urgent
from opportunity_app.apply_runs import ClaimHeldError, ClaimRefused
from pipeline_core.identity import employer_key
from opportunity_app.operations import ACCOUNT_QUERIES, delete_account, export_account, run_retention
from opportunity_app.outreach_automation import AutomationWorker
from opportunity_app.schema import ensure_product_schema
from opportunity_app.database import connect_product
from opportunity_app.timestamps import utc_now

from helpers_platform import build_and_migrate
from helpers_apply import ApplyCase, BLUEFIN, USER, setUpModule, tearDownModule  # noqa: F401 (module fixtures: unittest and pytest find them here)

MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"
FOREIGN = "another-server-process"


class ClaimTests(ApplyCase):
    def test_a_claim_opens_the_application_and_says_so_without_touching_the_interactions(self):
        self.opportunity("job-1")
        before = self.conn.execute("SELECT COUNT(*) FROM opportunity_interactions").fetchone()[0]
        claim = self.start("job-1", "one_click", now=self.at(1))
        self.assertEqual(claim["application_id"], "app-job-1")
        self.assertEqual(self.stage("job-1")[0], "applying")
        self.assertEqual(self.events("app-job-1"), ["apply_agent_started"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM opportunity_interactions").fetchone()[0], before,
                         "the app started filling; the student did not open the posting")
        row = self.claim_row(claim["token"])
        self.assertEqual((row["state"], row["after_click"], row["instance"], row["stage_policy"]), ("claimed", 0, SERVER_INSTANCE, "record"))
        self.assertIn(claim["token"], apply_runs.RUNNING, "held from before the insert, so no recovery pass calls it stale")

    def test_the_stage_policy_is_fixed_when_the_claim_is_made(self):
        self.assertEqual(apply_runs.stage_policy_for("one_click"), "record")
        self.assertEqual(apply_runs.stage_policy_for("unattended"), "ledger")
        self.assertEqual(apply_runs.stage_policy_for("handoff"), "ask", "D1 B: the student presses Submit, so the card asks")
        self.assertEqual(apply_runs.stage_policy_for("handoff", one_click_approved=True), "record")
        claim = self.start("job-1", "handoff", now=self.at(1))
        self.assertEqual(self.claim_row(claim["token"])["stage_policy"], "ask")

    def test_a_refused_claim_leaves_nothing_behind(self):
        self.opportunity("job-1")
        self.raw_claim(mode="handoff", handed_over_at=utc_now(), state="submitted")  # spacing blocks the next one
        with self.assertRaises(ClaimRefused) as caught:
            self.start("job-1", "handoff", now=self.at(1))
        self.assertEqual(caught.exception.code, "spacing")
        self.assertIsNone(self.stage("job-1"), "looking, or being refused, never creates an application")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM application_events WHERE event_type='apply_agent_started'").fetchone()[0], 0)
        self.assertEqual(apply_runs.RUNNING, set(), "nothing is held for a claim that was never made")

    def test_unattended_is_refused_while_automation_is_paused(self):
        automation.set_paused(self.conn, USER, True)
        with self.assertRaises(automation.AutomationPaused):
            self.start("job-1", "unattended", now=self.at(1))
        self.assertIsNone(self.stage("job-1"))
        self.start("job-2", "handoff", now=self.at(2))  # a student-started attempt is not stopped at the claim

    def test_two_connections_claim_one_application_and_exactly_one_gets_it(self):
        self.opportunity("job-1")
        results = []
        barrier = threading.Barrier(2)

        def attempt(number):
            with closing(connect_product(self.path)) as conn:
                barrier.wait()
                try:
                    claim = apply_runs.claim(
                        conn, user_id=USER, opportunity_id="job-1", mode="handoff", ats="greenhouse", board_token="bluefin",
                        job_ref="bluefin/1", company=employer_key(BLUEFIN), now=self.at(number),
                    )
                    results.append(("claimed", claim["token"]))
                except ClaimRefused as refusal:
                    results.append(("refused", str(refusal)))

        threads = [threading.Thread(target=attempt, args=(number,)) for number in (1, 2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(kind for kind, _ in results), ["claimed", "refused"])
        self.assertEqual(dict(results)["refused"], apply_runs.LIVE_APPLICATION)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM application_submit_claims").fetchone()[0], 1)

    def test_the_partial_unique_indexes_are_the_locks(self):
        first = self.raw_claim(state="claimed", mode="handoff")
        row = self.claim_row(first)

        def insert(token, application_id, job_ref, state):
            with self.conn:
                self.conn.execute(
                    "INSERT INTO application_submit_claims(token, application_id, user_id, opportunity_id, instance, mode, state, ats, board_token, "
                    "job_ref, company_key, stage_policy, plan_hash, heartbeat_at, created_at, updated_at) "
                    "VALUES(?, ?, ?, 'x', 'i', 'handoff', ?, 'greenhouse', 'bluefin', ?, 'bluefin robotics', 'ask', '', 't', 't', 't')",
                    (token, application_id, USER, state, job_ref),
                )

        self.opportunity("other")
        with self.conn:
            self.conn.execute("INSERT INTO applications(id, opportunity_id, user_id, stage, created_at, updated_at) VALUES('app-other', 'other', ?, 'applying', 't', 't')", (USER,))
        with self.assertRaises(sqlite3.IntegrityError):
            insert("dup-application", row["application_id"], "bluefin/other-job", "claimed")
        with self.assertRaises(sqlite3.IntegrityError):
            insert("dup-job", "app-other", row["job_ref"], "claimed")
        insert("tombstone", row["application_id"], row["job_ref"], "released")  # a released row locks nothing
        with self.conn:
            self.conn.execute("UPDATE application_submit_claims SET state='submitted' WHERE token=?", (first,))
        with self.assertRaises(sqlite3.IntegrityError, msg="a submitted claim keeps both locks for good"):
            insert("after-submit", "app-other", row["job_ref"], "claimed")

    def test_the_job_lock_refuses_a_second_saved_copy_until_the_first_is_released(self):
        self.opportunity("copy-a", title="Controls Intern")
        self.opportunity("copy-b", title="Controls Intern (repost)")
        first = self.start("copy-a", "handoff", job="bluefin/1001", now=self.at(1))
        with self.assertRaises(ClaimRefused) as caught:
            self.start("copy-b", "handoff", job="bluefin/1001", now=self.at(2))
        self.assertEqual(caught.exception.code, "job")
        self.assertIn("Controls Intern", str(caught.exception))
        self.assertIn("another saved copy", str(caught.exception))
        self.assertIsNone(self.stage("copy-b"), "the refused copy was not opened")
        apply_runs.settle(self.conn, first["token"], user_id=USER, state="submitted", now=self.at(3))
        with self.assertRaises(ClaimRefused):
            self.start("copy-b", "handoff", job="bluefin/1001", now=self.at(60 * 30))  # submitted: never again, from any copy
        with self.conn:
            self.conn.execute("UPDATE application_submit_claims SET state='released' WHERE token=?", (first["token"],))
        self.start("copy-b", "handoff", job="bluefin/1001", now=self.at(60 * 24 * 40))

    def test_a_conflict_the_checks_missed_is_still_refused_by_the_index_with_the_same_sentences(self):
        self.opportunity("copy-a", title="Controls Intern")
        self.opportunity("copy-b", title="Controls Intern (repost)")
        self.start("copy-a", "handoff", job="bluefin/1001", now=self.at(1))
        with mock.patch.object(apply_runs, "duplicate_block", return_value=None):
            with self.assertRaises(ClaimRefused) as same:
                self.start("copy-a", "handoff", job="bluefin/1001", now=self.at(2))
            with self.assertRaises(ClaimRefused) as other:
                self.start("copy-b", "handoff", job="bluefin/1001", now=self.at(3))
        self.assertEqual((same.exception.code, str(same.exception)), ("application", apply_runs.LIVE_APPLICATION))
        self.assertEqual(other.exception.code, "job")
        self.assertIn("another saved copy of the role (Controls Intern)", str(other.exception))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM application_submit_claims").fetchone()[0], 1)
        self.assertEqual(len(apply_runs.RUNNING), 1, "only the claim that was made is held")

    def test_starting_again_releases_an_attempt_that_sent_nothing_and_never_one_that_did(self):
        first = self.start("job-1", "handoff", now=self.at(1))
        apply_runs.settle(self.conn, first["token"], user_id=USER, state="needs_you", note="A required question has no answer", now=self.at(2))
        second = self.start("job-1", "handoff", now=self.at(4))  # nothing was sent, so a student's next start releases it
        self.assertEqual(self.claim_row(first["token"])["state"], "released")
        self.assertEqual(self.claim_row(first["token"])["resolved_by"], "student")
        self.assertEqual(self.claim_row(second["token"])["state"], "claimed")
        # Now one that reached Greenhouse: a retry cannot release it.
        self.assertTrue(apply_runs.hand_over(self.conn, second["token"], user_id=USER, now=self.at(5)))
        apply_runs.settle(self.conn, second["token"], user_id=USER, state="needs_you", note="Greenhouse asked for a security code", now=self.at(6))
        row = self.claim_row(second["token"])
        self.assertEqual((row["state"], row["after_click"]), ("needs_you", 1))
        for retry in (True, False):
            with self.assertRaises(ClaimRefused) as caught:
                self.start("job-1", "handoff", retry=retry, now=self.at(60))
            self.assertEqual(str(caught.exception), "Greenhouse asked for a security code", "the claim's own note says why")
        self.assertEqual(self.claim_row(second["token"])["state"], "needs_you")

    def test_an_attempt_the_app_stopped_before_hand_over_does_not_block_the_next_start(self):
        first = self.start("job-1", "handoff", now=self.at(-10))
        apply_runs.forget(first["token"])
        apply_runs.recover_stale(self.conn, self.at())
        row = self.claim_row(first["token"])
        self.assertEqual((row["state"], row["after_click"]), ("failed", 0))
        self.assertIn("Nothing was sent", row["note"])
        self.assertIsNone(apply_runs.duplicate_block(self.conn, USER, opportunity_id="job-1", ats="greenhouse", job_ref="bluefin/job-1",
                                                     company=employer_key(BLUEFIN), application_id="app-job-1", now=self.at()))
        again = self.start("job-1", "one_click", now=self.at(1), confirmed_at=self.at().isoformat(), acknowledged=())
        self.assertEqual(self.claim_row(first["token"])["state"], "released")
        self.assertEqual(self.claim_row(again["token"])["state"], "claimed")

    def test_unattended_mode_never_releases_a_stopped_attempt_and_says_why_honestly(self):
        first = self.start("job-1", "handoff", now=self.at(1))
        apply_runs.settle(self.conn, first["token"], user_id=USER, state="needs_you", note="A required question has no answer", now=self.at(2))
        with self.assertRaises(ClaimRefused) as caught:
            self.start("job-1", "unattended", now=self.at(60))
        self.assertEqual((caught.exception.code, str(caught.exception)), ("stopped_earlier", apply_runs.STOPPED_EARLIER))
        self.assertNotIn("submitted", str(caught.exception))
        self.assertEqual(self.claim_row(first["token"])["state"], "needs_you", "rule 2: the worker releases nothing")

    def test_unattended_mode_cannot_tick_past_the_company_limit(self):
        self.raw_claim(state="submitted", handed_over_at=self.at(-120).isoformat(timespec="microseconds"), company=BLUEFIN)
        self.opportunity("job-2", BLUEFIN)
        with self.assertRaises(ClaimRefused) as caught:
            self.start("job-2", "unattended", job="bluefin/2002", now=self.at(), acknowledged=("company_limit",))
        self.assertEqual(caught.exception.code, "company_limit")
        self.start("job-2", "handoff", job="bluefin/2002", now=self.at(), acknowledged=("company_limit",))


class DuplicateChecks(ApplyCase):
    def check(self, opportunity_id, **kwargs):
        return apply_runs.duplicate_block(
            self.conn, USER, opportunity_id=opportunity_id, ats="greenhouse", job_ref=f"bluefin/{opportunity_id}",
            company=employer_key(self.companies[opportunity_id]), now=self.at(1), **kwargs,
        )

    def test_an_application_that_is_not_applying_is_not_submitted(self):
        self.opportunity("job-1")
        actions.record_intent(self.conn, "job-1", "apply_opened", user_id=USER)
        self.assertIsNone(self.check("job-1"))
        actions.update_application(self.conn, "app-job-1", stage="applied", user_id=USER)
        block = self.check("job-1")
        self.assertEqual((block.kind, block.code, block.message), ("failed", "stage", "This application is already applied"))

    def test_a_confirmation_email_or_a_marked_session_means_it_was_already_sent(self):
        self.opportunity("job-1")
        actions.record_intent(self.conn, "job-1", "apply_opened", user_id=USER)
        with self.conn:
            self.conn.execute(
                "INSERT INTO application_mail_messages(user_id, gmail_id, application_id, kind, received_at, recorded_at) "
                "VALUES(?, 'g1', 'app-job-1', 'application_confirmation', '2026-09-20T15:00:00+00:00', ?)", (USER, utc_now()),
            )
        block = self.check("job-1")
        self.assertEqual(block.message, "Greenhouse already confirmed an application from you on September 20")
        self.opportunity("job-2")
        actions.record_intent(self.conn, "job-2", "apply_opened", user_id=USER)
        with self.conn:
            self.conn.execute(
                "INSERT INTO application_form_sessions(id, user_id, application_id, page_url, ats_type, fields_json, status, created_at, updated_at) "
                "VALUES('s1', ?, 'app-job-2', 'https://boards.example.test/x', 'greenhouse', '[]', 'completed', '2026-09-21T15:00:00+00:00', '2026-09-21T15:00:00+00:00')",
                (USER,),
            )
        self.assertEqual(self.check("job-2").message, "You marked this application submitted on September 21")

    def test_a_tombstone_asks_before_the_same_job_is_tried_again_and_a_tick_allows_it(self):
        self.opportunity("job-1")
        self.raw_claim(state="released", handed_over_at=self.at(-5000).isoformat(), job_ref="bluefin/job-1", after_click=1)
        block = self.check("job-1")
        self.assertEqual((block.kind, block.code), ("ask", "released_job"))
        self.assertIn("didn't go through", block.message)
        self.assertIn("Greenhouse may still have it", block.message)
        self.assertIsNone(self.check("job-1", acknowledged=("released_job",)))

    def test_a_confirmation_the_reader_could_not_match_to_a_role_asks_and_it_needs_greenhouse_and_the_company(self):
        self.opportunity("job-1")
        actions.record_intent(self.conn, "job-1", "saved", user_id=USER)

        def mail(gmail_id, subject, domain):
            with self.conn:
                self.conn.execute(
                    "INSERT INTO application_mail_messages(user_id, gmail_id, application_id, kind, subject, sender_domain, received_at, recorded_at) "
                    "VALUES(?, ?, '', 'application_confirmation', ?, ?, ?, ?)", (USER, gmail_id, subject, domain, utc_now(), utc_now()),
                )

        mail("g-other-company", "Thank you for applying to Orbit Systems", "us.greenhouse-mail.io")
        mail("g-not-greenhouse", "Thank you for applying to Bluefin Robotics", "jobs.example.test")
        self.assertIsNone(self.check("job-1"))
        mail("g-match", "Thank you for applying to Bluefin Robotics", "us.greenhouse-mail.io")
        block = self.check("job-1")
        self.assertEqual((block.kind, block.code), ("ask", "unmatched_confirmation"))
        self.assertIn("couldn't match to a role", block.message)
        self.assertIn("I haven't applied to this role", block.message)
        self.assertIsNone(self.check("job-1", acknowledged=("unmatched_confirmation",)))

    def test_an_application_left_applying_for_more_than_a_day_asks_whether_it_was_done_by_hand(self):
        self.opportunity("job-1")
        actions.record_intent(self.conn, "job-1", "apply_opened", user_id=USER)
        self.assertIsNone(self.check("job-1"))
        with self.conn:
            self.conn.execute("UPDATE applications SET created_at=? WHERE id='app-job-1'", (self.at(-60 * 30).isoformat(timespec="microseconds"),))
        block = self.check("job-1")
        self.assertEqual((block.kind, block.code), ("ask", "applying_old"))
        self.assertEqual(block.message, "Did you already apply to this by hand? I haven't applied yet.")
        self.assertIsNone(self.check("job-1", acknowledged=("applying_old",)))

    def test_it_reads_only_and_never_opens_the_application(self):
        self.opportunity("job-1")
        events = self.conn.execute("SELECT COUNT(*) FROM application_events").fetchone()[0]
        for _ in range(3):
            self.check("job-1")
        self.assertIsNone(self.stage("job-1"))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM application_events").fetchone()[0], events)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM apply_runs").fetchone()[0], 0)


class HandOverTests(ApplyCase):
    def claimed(self, mode, opportunity_id="job-1", **kwargs):
        return self.start(opportunity_id, mode, now=self.at(-30), **kwargs)["token"]

    def rehearsal(self, minutes):
        run_id = self.make_run(started=self.at(minutes - 1))
        apply_runs.finish_run(self.conn, run_id, outcome="rehearsed", clean=True, now=self.at(minutes))
        return run_id

    def test_the_hand_over_moves_the_claim_to_clicking_once(self):
        token = self.claimed("handoff")
        self.assertTrue(apply_runs.hand_over(self.conn, token, user_id=USER, now=self.at(1)))
        row = self.claim_row(token)
        self.assertEqual((row["state"], row["after_click"]), ("clicking", 1))
        self.assertEqual(_parsed(row["handed_over_at"]), self.at(1))
        self.assertFalse(apply_runs.hand_over(self.conn, token, user_id=USER, now=self.at(2)), "only a claimed attempt is handed over")
        self.assertEqual(_parsed(self.claim_row(token)["handed_over_at"]), self.at(1), "and never twice")

    def test_it_refuses_a_token_that_is_not_ours_a_cancelled_claim_and_another_students(self):
        token = self.claimed("handoff")
        self.assertFalse(apply_runs.hand_over(self.conn, "not-a-token", user_id=USER, now=self.at(1)))
        self.assertTrue(apply_runs.request_cancel(self.conn, token, user_id=USER))
        self.assertFalse(apply_runs.hand_over(self.conn, token, user_id=USER, now=self.at(1)))
        row = self.claim_row(token)
        self.assertEqual((row["state"], row["after_click"], row["handed_over_at"]), ("claimed", 0, None), "nothing left")
        # Once handed over, a cancel is too late.
        other = self.claimed("handoff", "job-2")
        self.assertTrue(apply_runs.hand_over(self.conn, other, user_id=USER, now=self.at(1)))
        self.assertFalse(apply_runs.request_cancel(self.conn, other, user_id=USER))

    def test_unattended_is_refused_by_a_pause_and_finish_in_browser_is_not(self):
        unattended = self.claimed("unattended", "job-1")
        handoff = self.claimed("handoff", "job-2")
        automation.set_paused(self.conn, USER, True)
        self.assertFalse(apply_runs.hand_over(self.conn, unattended, user_id=USER, now=self.at(1)))
        self.assertEqual(self.claim_row(unattended)["state"], "claimed")
        self.assertTrue(apply_runs.hand_over(self.conn, handoff, user_id=USER, now=self.at(1)),
                        "the student's own press of Submit in the window is the confirm")
        self.assertEqual([item["action"] for item in automation.in_flight(self.conn, USER)], ["application"],
                         "and a pause reports it as past stopping")

    def test_one_click_the_newer_intent_wins_between_a_pause_and_the_confirm(self):
        rehearsal = self.rehearsal(-10)
        # A pause that began after the student confirmed stops it.
        confirmed = self.at(-5)
        token = self.claimed("one_click", "job-1", confirmed_at=confirmed.isoformat(timespec="microseconds"), rehearsal_run_id=rehearsal)
        automation.set_paused(self.conn, USER, True)   # now: after the confirm
        self.assertFalse(apply_runs.hand_over(self.conn, token, user_id=USER, now=self.at(1)))
        self.assertEqual(self.claim_row(token)["state"], "claimed")
        # A pause that was already on when the student confirmed does not.
        later = self.claimed("one_click", "job-2", confirmed_at=(datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat(timespec="microseconds"),
                             rehearsal_run_id=rehearsal)
        self.assertTrue(apply_runs.hand_over(self.conn, later, user_id=USER, now=self.at(1)))

    def test_one_click_the_confirmed_rehearsal_expires_after_fifteen_minutes(self):
        rehearsal = self.rehearsal(0)
        token = self.claimed("one_click", "job-1", confirmed_at=self.at(1).isoformat(), rehearsal_run_id=rehearsal)
        self.assertFalse(apply_runs.hand_over(self.conn, token, user_id=USER, now=self.at(15)), "15 minutes old or more")
        self.assertTrue(apply_runs.hand_over(self.conn, token, user_id=USER, now=self.at(14, seconds=59)))

    def test_one_click_without_a_confirm_or_a_rehearsal_is_refused(self):
        rehearsal = self.rehearsal(0)
        no_confirm = self.claimed("one_click", "job-1", rehearsal_run_id=rehearsal)
        no_rehearsal = self.claimed("one_click", "job-2", confirmed_at=self.at(1).isoformat())
        for token in (no_confirm, no_rehearsal):
            self.assertFalse(apply_runs.hand_over(self.conn, token, user_id=USER, now=self.at(2)), "when unsure, nothing is sent")


def _parsed(value):
    return datetime.fromisoformat(value)


class HeartbeatAndRecovery(ApplyCase):
    def test_a_heartbeat_keeps_a_claim_from_another_process_held_until_it_goes_quiet(self):
        token = self.raw_claim(state="clicking", instance=FOREIGN, heartbeat_at=self.at(-1).isoformat(timespec="microseconds"), handed_over_at=self.at(-20).isoformat())
        row = self.claim_row(token)
        self.assertTrue(apply_runs.claim_held(row, now=self.at()), "less than 2 minutes since its heartbeat")
        self.assertFalse(apply_runs.claim_held(row, now=self.at(2)))
        self.assertTrue(apply_runs.heartbeat(self.conn, token, now=self.at(2)))
        self.assertTrue(apply_runs.claim_held(self.claim_row(token), now=self.at(3)))
        self.assertEqual(apply_runs.recover_stale(self.conn, self.at(3)), {"failed": 0, "unconfirmed": 0, "stage_retried": 0, "runs_failed": 0})
        self.assertEqual(self.claim_row(token)["state"], "clicking")
        settled = self.raw_claim(state="submitted", instance=FOREIGN)
        self.assertFalse(apply_runs.heartbeat(self.conn, settled), "only a claimed or clicking attempt has a heartbeat")

    def test_a_claim_of_this_process_is_held_only_while_its_run_is_working(self):
        token = self.raw_claim(state="claimed", instance=SERVER_INSTANCE, heartbeat_at=self.at().isoformat(timespec="microseconds"))
        self.assertFalse(apply_runs.claim_held(self.claim_row(token), now=self.at()), "not in RUNNING: it ended without settling")
        apply_runs.RUNNING.add(token)
        self.assertTrue(apply_runs.claim_held(self.claim_row(token), now=self.at(60 * 24)), "however long it has been")
        apply_runs.forget(token)
        self.assertFalse(apply_runs.claim_held(self.claim_row(token), now=self.at()))

    def test_a_finish_in_browser_claim_nineteen_minutes_old_with_a_fresh_heartbeat_is_held(self):
        token = self.raw_claim(state="clicking", instance=FOREIGN, handed_over_at=self.at(-19).isoformat(timespec="microseconds"),
                               heartbeat_at=self.at(-0.5).isoformat(timespec="microseconds"), mode="handoff")
        self.assertEqual([item["action"] for item in automation.in_flight(self.conn, USER, now=self.at())], ["application"],
                         "by heartbeat, not by the claim's age")
        apply_runs.recover_stale(self.conn, self.at())
        self.assertEqual(self.claim_row(token)["state"], "clicking")

    def test_recovery_fails_a_claim_before_hand_over_and_leaves_a_clicking_one_unconfirmed(self):
        before = self.raw_claim(state="claimed", mode="handoff", instance=FOREIGN, heartbeat_at=self.at(-10).isoformat(timespec="microseconds"))
        during = self.raw_claim(state="clicking", mode="handoff", instance=FOREIGN, heartbeat_at=self.at(-10).isoformat(timespec="microseconds"),
                                handed_over_at=self.at(-12).isoformat(timespec="microseconds"))
        counts = apply_runs.recover_stale(self.conn, self.at())
        self.assertEqual((counts["failed"], counts["unconfirmed"]), (1, 1))
        row = self.claim_row(before)
        self.assertEqual((row["state"], row["after_click"], row["note"]),
                         ("failed", 0, "The app stopped before handing your application to Greenhouse. Nothing was sent."))
        row = self.claim_row(during)
        self.assertEqual((row["state"], row["after_click"], row["note"]), ("unconfirmed", 1, "The app stopped while submitting. Check whether it arrived."))
        self.assertEqual(len([title for title in self.notices() if "stopped while submitting" in title]), 1)
        self.assertEqual(apply_runs.recover_stale(self.conn, self.at(1)), {"failed": 0, "unconfirmed": 0, "stage_retried": 0, "runs_failed": 0},
                         "recovered once")

    def test_uncertain_attempts_are_never_picked_up_and_only_the_student_settles_them(self):
        unconfirmed = self.raw_claim(state="unconfirmed", mode="handoff", handed_over_at=self.at(-60).isoformat(timespec="microseconds"))
        needs = self.raw_claim(state="needs_you", mode="handoff", handed_over_at=self.at(-60).isoformat(timespec="microseconds"), after_click=1)
        apply_runs.recover_stale(self.conn, self.at())
        self.assertEqual([self.claim_row(unconfirmed)["state"], self.claim_row(needs)["state"]], ["unconfirmed", "needs_you"])
        self.assertEqual(apply_runs.students_to_watch(self.conn, self.at()), [USER], "watched, so an email can settle them")
        went = apply_runs.resolve_uncertain(self.conn, unconfirmed, user_id=USER, went_through=True, now=self.at(1))
        self.assertEqual((went["state"], went["resolved_by"], went["verification"]), ("submitted", "student", "awaiting_email"))
        self.assertIn("apply_agent_submitted", self.events(went["application_id"]))
        gone = apply_runs.resolve_uncertain(self.conn, needs, user_id=USER, went_through=False, now=self.at(2))
        self.assertEqual((gone["state"], gone["resolved_by"]), ("released", "student"))
        self.assertIsNotNone(gone["handed_over_at"], "the tombstone still counts toward the limits")
        with self.assertRaises(ValueError):
            apply_runs.resolve_uncertain(self.conn, needs, user_id=USER, went_through=True, now=self.at(3))

    def test_the_student_cannot_answer_while_the_claim_is_held(self):
        token = self.raw_claim(state="clicking", instance=FOREIGN, heartbeat_at=self.at().isoformat(timespec="microseconds"),
                               handed_over_at=self.at(-1).isoformat(timespec="microseconds"))
        for answer in (True, False):
            with self.assertRaises(ClaimHeldError):
                apply_runs.resolve_uncertain(self.conn, token, user_id=USER, went_through=answer, now=self.at(1))
        self.assertEqual(self.claim_row(token)["state"], "clicking")
        # Once it is not held (the other process went quiet) the same claim can be answered.
        result = apply_runs.resolve_uncertain(self.conn, token, user_id=USER, went_through=False, now=self.at(10))
        self.assertEqual(result["state"], "released")

    def test_the_stage_write_is_retried_for_record_and_ledger_but_never_for_ask(self):
        recorded = self.raw_claim(state="submitted", stage_policy="record", submitted_at=self.at(-5).isoformat(timespec="microseconds"))
        asked = self.raw_claim(state="submitted", stage_policy="ask", submitted_at=self.at(-5).isoformat(timespec="microseconds"))
        ledger = self.raw_claim(state="submitted", stage_policy="ledger", submitted_at=self.at(-5).isoformat(timespec="microseconds"))
        feature = automation.Feature("auto_apply", "Test auto apply", "A stand-in for the unattended switch", "applications", "external")
        automation.register(feature)
        self.addCleanup(automation.FEATURES.pop, "auto_apply", None)
        counts = apply_runs.recover_stale(self.conn, self.at())
        self.assertEqual(counts["stage_retried"], 1, "the ledger waits for its own switch")
        self.assertEqual(self.claim_row(recorded)["stage_recorded"], 1)
        self.assertEqual(self.stage(self.claim_row(recorded)["opportunity_id"])[0], "applied")
        self.assertEqual(self.claim_row(asked)["stage_recorded"], 0)
        self.assertEqual(self.stage(self.claim_row(asked)["opportunity_id"])[0], "applying", "ask: the card asks, nothing moves by itself")
        self.assertEqual(self.claim_row(ledger)["stage_recorded"], 0)
        automation.set_mode(self.conn, USER, "auto_apply", "on")
        automation.set_paused(self.conn, USER, True)
        self.assertEqual(apply_runs.recover_stale(self.conn, self.at(1))["stage_retried"], 0, "while paused the ledger writes nothing")
        automation.set_paused(self.conn, USER, False)
        self.assertEqual(apply_runs.recover_stale(self.conn, self.at(2))["stage_retried"], 1, "and it is retried after resume")
        self.assertEqual(self.claim_row(ledger)["stage_recorded"], 1)
        self.assertEqual(self.stage(self.claim_row(ledger)["opportunity_id"])[0], "applied")
        again = self.conn.execute("SELECT COUNT(*) FROM automation_actions WHERE idempotency_key=?", (f"apply:{ledger}",)).fetchone()[0]
        self.assertEqual(again, 1)

    def test_a_running_run_that_went_quiet_is_finished_and_one_that_is_working_is_not(self):
        quiet = self.make_run("rehearsal", started=self.at(-10))
        working = self.make_run("rehearsal", started=self.at(-10))
        fresh = self.make_run("lookup", started=self.at(-1))
        apply_runs.heartbeat_run(self.conn, fresh, now=self.at(-0.5))
        with apply_runs.running_run(working):
            counts = apply_runs.recover_stale(self.conn, self.at())
        self.assertEqual(counts["runs_failed"], 1)
        row = apply_runs.get_run(self.conn, quiet, user_id=USER)
        self.assertEqual((row["status"], row["outcome"], json.loads(row["reasons_json"])), ("finished", "failed", ["The app stopped during this run"]))
        self.assertEqual(apply_runs.get_run(self.conn, working, user_id=USER)["status"], "running")
        self.assertEqual(apply_runs.get_run(self.conn, fresh, user_id=USER)["status"], "running")
        self.assertIn("The app stopped during an Apply for me run", self.notices())

    def test_a_submit_run_whose_claim_was_handed_over_is_unconfirmed_not_failed(self):
        claim = self.start("job-1", "handoff", now=self.at(-10))
        run_id = self.make_run("handoff", opportunity_id="job-1", started=self.at(-10), claim_token=claim["token"])
        self.assertTrue(apply_runs.hand_over(self.conn, claim["token"], user_id=USER, now=self.at(-9)))
        apply_runs.forget(claim["token"])
        apply_runs.recover_stale(self.conn, self.at())
        self.assertEqual(self.claim_row(claim["token"])["state"], "unconfirmed")
        self.assertEqual(apply_runs.get_run(self.conn, run_id, user_id=USER)["outcome"], "unconfirmed")

    def notice_bodies(self):
        return {row["title"]: row["body"] for row in automation.list_notices(self.conn, USER, limit=50)}

    def test_a_run_made_before_its_claim_is_found_through_the_claims_run_id(self):
        run_id = self.make_run("handoff", opportunity_id="job-1", started=self.at(-10))
        claim = self.start("job-1", "handoff", now=self.at(-10), run_id=run_id)  # run first: the run's claim_token is still ''
        self.assertEqual(apply_runs.get_run(self.conn, run_id, user_id=USER)["claim_token"], "")
        self.assertTrue(apply_runs.hand_over(self.conn, claim["token"], user_id=USER, now=self.at(-9)))
        apply_runs.forget(claim["token"])
        apply_runs.recover_stale(self.conn, self.at())
        self.assertEqual(self.claim_row(claim["token"])["state"], "unconfirmed")
        self.assertEqual(apply_runs.get_run(self.conn, run_id, user_id=USER)["outcome"], "unconfirmed")
        bodies = self.notice_bodies()
        self.assertEqual(bodies["The app stopped during an Apply for me run"], "Check whether your application arrived.")
        self.assertNotIn("Nothing was sent.", bodies.values(), "it was handed over, so nobody can say that")

    def test_a_submit_run_says_nothing_was_sent_only_when_its_claim_was_never_handed_over(self):
        before = self.start("job-1", "handoff", now=self.at(-10))
        run_before = self.make_run("handoff", opportunity_id="job-1", started=self.at(-10), claim_token=before["token"])
        apply_runs.forget(before["token"])
        apply_runs.recover_stale(self.conn, self.at())
        self.assertEqual(apply_runs.get_run(self.conn, run_before, user_id=USER)["outcome"], "failed")
        self.assertEqual(self.notice_bodies()["The app stopped during an Apply for me run"], "Nothing was sent.")
        # A submit run with no claim at all: the app cannot say what left.
        orphan = self.make_run("submit", opportunity_id="job-9", started=self.at(-10))
        apply_runs.recover_stale(self.conn, self.at(1))
        self.assertEqual(apply_runs.get_run(self.conn, orphan, user_id=USER)["outcome"], "unconfirmed")

    def test_a_lookup_or_rehearsal_never_claims_that_nothing_was_sent(self):
        for kind in ("lookup", "rehearsal"):
            run_id = self.make_run(kind, opportunity_id=f"op-{kind}", started=self.at(-10))
            apply_runs.recover_stale(self.conn, self.at())
            self.assertEqual(apply_runs.get_run(self.conn, run_id, user_id=USER)["outcome"], "failed")
        bodies = set(self.notice_bodies().values())
        self.assertEqual(bodies, {"No application was sent."}, "typed text does reach Greenhouse's lookup service, so not 'nothing'")

    def test_a_run_whose_claim_is_still_held_is_not_recovered_because_only_its_own_heartbeat_went_quiet(self):
        claim = self.start("job-1", "handoff", now=self.at(-10))
        run_id = self.make_run("handoff", opportunity_id="job-1", started=self.at(-10), claim_token=claim["token"])
        apply_runs.recover_stale(self.conn, self.at())  # the claim is in RUNNING: held
        self.assertEqual(apply_runs.get_run(self.conn, run_id, user_id=USER)["status"], "running")
        self.assertEqual(self.claim_row(claim["token"])["state"], "claimed")

    def test_link_run_ties_both_rows_together(self):
        claim = self.start("job-1", "handoff", now=self.at(1))
        run_id = self.make_run("handoff", opportunity_id="job-1", started=self.at(1))
        self.assertTrue(apply_runs.link_run(self.conn, user_id=USER, run_id=run_id, token=claim["token"]))
        self.assertEqual(apply_runs.get_run(self.conn, run_id, user_id=USER)["claim_token"], claim["token"])
        self.assertEqual(self.claim_row(claim["token"])["run_id"], run_id)
        self.assertFalse(apply_runs.link_run(self.conn, user_id="someone-else", run_id=run_id, token=claim["token"]))


class SettleTests(ApplyCase):
    def test_a_settle_that_finds_the_claim_released_still_reports_a_seen_confirmation(self):
        claim = self.start("job-1", "handoff", now=self.at(1))
        apply_runs.hand_over(self.conn, claim["token"], user_id=USER, now=self.at(2))
        with self.conn:
            self.conn.execute("UPDATE application_submit_claims SET state='released' WHERE token=?", (claim["token"],))
        settled = apply_runs.settle(self.conn, claim["token"], user_id=USER, state="submitted", confirmation_seen=True,
                                    event_detail={"run_id": "run-x"}, now=self.at(3))
        self.assertFalse(settled, "it never overwrites what the student decided meanwhile")
        self.assertEqual(self.claim_row(claim["token"])["state"], "released")
        self.assertIn("apply_agent_submitted", self.events("app-job-1"))
        self.assertIn("Greenhouse showed its confirmation page for Bluefin Robotics, after this attempt was marked as not sent. Check it.", self.notices())

    def test_a_seen_confirmation_may_move_an_unconfirmed_row_but_an_unseen_one_may_not(self):
        token = self.raw_claim(state="unconfirmed", handed_over_at=self.at(-10).isoformat(timespec="microseconds"), stage_policy="ask")
        self.assertFalse(apply_runs.settle(self.conn, token, user_id=USER, state="submitted", now=self.at(1)))
        self.assertEqual(self.claim_row(token)["state"], "unconfirmed")
        self.assertTrue(apply_runs.settle(self.conn, token, user_id=USER, state="submitted", confirmation_seen=True, resolved_by="page", now=self.at(2)))
        row = self.claim_row(token)
        self.assertEqual((row["state"], row["resolved_by"], row["verification"]), ("submitted", "page", "awaiting_email"))
        self.assertEqual(_parsed(row["watch_until"]), self.at(2) + timedelta(hours=24), "the email is looked for for 24 hours")

    def test_a_settle_never_overwrites_a_newer_attempt(self):
        old = self.start("job-1", "handoff", now=self.at(1))
        apply_runs.settle(self.conn, old["token"], user_id=USER, state="failed", note="stopped", now=self.at(2))
        new = self.start("job-1", "handoff", retry=True, now=self.at(3))
        self.assertFalse(apply_runs.settle(self.conn, old["token"], user_id=USER, state="submitted", now=self.at(4)))
        self.assertEqual(self.claim_row(new["token"])["state"], "claimed")

    def test_a_watch_that_is_not_required_is_not_watched(self):
        claim = self.start("job-1", "handoff", now=self.at(1))
        apply_runs.settle(self.conn, claim["token"], user_id=USER, state="submitted", watch=False, now=self.at(2))
        row = self.claim_row(claim["token"])
        self.assertEqual((row["verification"], row["watch_until"]), ("not_watched", None))

    def test_a_result_settles_the_claim_and_the_run_together_and_moves_the_stage_by_policy(self):
        # one_click ('record'): the stage moves when the confirmation page was seen.
        claim = self.start("job-1", "one_click", now=self.at(1))
        run_id = self.make_run("submit", opportunity_id="job-1", started=self.at(1), claim_token=claim["token"], application_id=claim["application_id"])
        submitted = self.at(3).isoformat(timespec="microseconds")
        result = apply_runs.record_result(
            self.conn, user_id=USER, token=claim["token"], run_id=run_id, state="submitted", outcome="submitted", note="Greenhouse showed its confirmation page",
            reasons=[], after_click=True, submitted_at=submitted, confirmation_seen=True, evidence={"post_status": 302, "confirmation_path": "/confirmation"},
            screenshots=[{"step": "confirmation", "path": "x/y.png", "sha256": "ab" * 32, "masked": []}], requests=[{"method": "POST", "host": "boards.example.test", "path": "/apply", "status": 302}],
            event_detail={"run_id": run_id, "mode": "one_click"}, now=self.at(3),
        )
        self.assertEqual(result, {"settled": True, "stage_recorded": True})
        self.assertEqual(self.stage("job-1"), ("applied", submitted))
        run = apply_runs.get_run(self.conn, run_id, user_id=USER)
        self.assertEqual((run["status"], run["outcome"]), ("finished", "submitted"))
        self.assertEqual(json.loads(run["evidence_json"])["post_status"], 302)
        self.assertEqual(self.claim_row(claim["token"])["stage_recorded"], 1)
        self.assertEqual(self.claim_row(claim["token"])["resolved_by"], "page", "a seen confirmation page settled it")
        self.assertIn("apply_agent_submitted", self.events("app-job-1"))
        self.assertIn("Greenhouse showed its confirmation page for your application to Controls Intern at Bluefin Robotics", self.notices())
        # handoff under D1 B ('ask'): recorded as submitted, and the stage does not move by itself.
        ask = self.start("job-2", "handoff", now=self.at(60))
        run_two = self.make_run("handoff", opportunity_id="job-2", started=self.at(60), claim_token=ask["token"])
        outcome = apply_runs.record_result(self.conn, user_id=USER, token=ask["token"], run_id=run_two, state="submitted", outcome="submitted",
                                           after_click=True, confirmation_seen=True, now=self.at(62))
        self.assertEqual(outcome, {"settled": True, "stage_recorded": False})
        self.assertEqual(self.stage("job-2")[0], "applying")
        self.assertEqual(self.claim_row(ask["token"])["verification"], "awaiting_email")
        # ...until the student's "Mark as applied" makes the same write.
        self.assertTrue(apply_runs.record_stage(self.conn, ask["token"], user_id=USER, source="apply_agent:student_confirmed", now=self.at(70)))
        self.assertEqual(self.stage("job-2")[0], "applied")

    def test_the_stage_write_is_forward_only_and_a_stage_the_student_changed_wins(self):
        claim = self.start("job-1", "one_click", now=self.at(1))
        actions.update_application(self.conn, "app-job-1", stage="withdrawn", user_id=USER)
        apply_runs.settle(self.conn, claim["token"], user_id=USER, state="submitted", submitted_at=self.at(2).isoformat(), now=self.at(2))
        self.assertTrue(apply_runs.record_stage(self.conn, claim["token"], user_id=USER, now=self.at(3)))
        self.assertEqual(self.stage("job-1")[0], "withdrawn")
        self.assertEqual(self.claim_row(claim["token"])["stage_recorded"], 1, "settled: nothing left to retry")

    def test_a_failed_result_before_hand_over_says_nothing_was_sent(self):
        claim = self.start("job-1", "handoff", now=self.at(1))
        run_id = self.make_run("handoff", opportunity_id="job-1", started=self.at(1), claim_token=claim["token"])
        apply_runs.record_result(self.conn, user_id=USER, token=claim["token"], run_id=run_id, state="needs_you", outcome="needs_you",
                                 note="A required question has no saved answer", reasons=["A required question has no saved answer"], now=self.at(2))
        row = self.claim_row(claim["token"])
        self.assertEqual((row["state"], row["after_click"]), ("needs_you", 0))
        self.assertEqual(row["resolved_by"], "", "a stop for the student was not settled by the confirmation page")
        self.assertEqual(self.stage("job-1")[0], "applying")
        self.assertIn("Bluefin Robotics: your application needs you", self.notices())
        self.assertEqual(apply_runs.RUNNING, set())

    def test_only_a_seen_confirmation_page_is_recorded_as_settled_by_the_page(self):
        for number, (state, after_click, seen) in enumerate((("failed", False, False), ("unconfirmed", True, False), ("needs_you", True, False),
                                                              ("submitted", True, False))):
            opportunity = f"job-{number}"
            self.opportunity(opportunity, ("Alpha", "Bravo", "Charlie", "Delta")[number] + " Robotics")  # the company limit is per company
            claim = self.start(opportunity, "handoff", job=f"bluefin/{number}", board=f"board-{number}", now=self.at(number * 60))
            apply_runs.hand_over(self.conn, claim["token"], user_id=USER, now=self.at(number * 60 + 1)) if after_click else None
            run_id = self.make_run("handoff", opportunity_id=opportunity, started=self.at(number * 60), claim_token=claim["token"])
            apply_runs.record_result(self.conn, user_id=USER, token=claim["token"], run_id=run_id, state=state, outcome=state,
                                     after_click=after_click, confirmation_seen=seen, now=self.at(number * 60 + 2))
            with self.subTest(state=state, seen=seen):
                self.assertEqual(self.claim_row(claim["token"])["resolved_by"], "")

    def test_the_stage_change_after_it_went_through_says_who_settled_it(self):
        expected = {"student": "apply_agent:student_confirmed", "page": "apply_agent:confirmation_page", "email": "apply_agent:confirmation_email"}
        for number, (resolved_by, source) in enumerate(expected.items()):
            handed = self.at(number * 60 - 5).isoformat(timespec="microseconds")
            token = self.raw_claim(state="unconfirmed" if resolved_by == "student" else "submitted", stage_policy="record", handed_over_at=handed,
                                   submitted_at=handed)
            if resolved_by == "student":
                apply_runs.resolve_uncertain(self.conn, token, user_id=USER, went_through=True, now=self.at(number * 60))
            else:
                with self.conn:
                    self.conn.execute("UPDATE application_submit_claims SET resolved_by=? WHERE token=?", (resolved_by, token))
                self.assertTrue(apply_runs.record_stage(self.conn, token, user_id=USER, now=self.at(number * 60)))
            row = self.claim_row(token)
            detail = self.conn.execute(
                "SELECT detail_json FROM application_events WHERE application_id=? AND event_type='stage_changed'", (row["application_id"],),
            ).fetchall()
            with self.subTest(resolved_by=resolved_by):
                self.assertEqual(self.stage(row["opportunity_id"])[0], "applied")
                self.assertTrue(detail and source in detail[-1]["detail_json"], (source, [item["detail_json"] for item in detail]))

    def test_the_ledger_stage_is_not_recorded_when_perform_wrote_nothing(self):
        feature = automation.Feature("auto_apply", "Test auto apply", "A stand-in for the unattended switch", "applications", "external")
        automation.register(feature)
        self.addCleanup(automation.FEATURES.pop, "auto_apply", None)
        automation.set_mode(self.conn, USER, "auto_apply", "on")
        token = self.raw_claim(state="submitted", stage_policy="ledger", submitted_at=self.at(-5).isoformat(timespec="microseconds"))
        with mock.patch.object(automation, "perform", return_value=None):  # a pause landed between the check and the write
            self.assertEqual(apply_runs.recover_stale(self.conn, self.at())["stage_retried"], 0)
        self.assertEqual(self.claim_row(token)["stage_recorded"], 0, "nothing was written, so the next pass tries again")
        self.assertEqual(apply_runs.recover_stale(self.conn, self.at(1))["stage_retried"], 1)
        self.assertEqual(self.claim_row(token)["stage_recorded"], 1)
        # A stage that has already moved on (the student applied by hand) is settled, not retried for ever.
        other = self.raw_claim(state="submitted", stage_policy="ledger", submitted_at=self.at(-5).isoformat(timespec="microseconds"))
        actions.update_application(self.conn, self.claim_row(other)["application_id"], stage="applied", user_id=USER)
        self.assertEqual(apply_runs.recover_stale(self.conn, self.at(2))["stage_retried"], 1)
        self.assertEqual(self.claim_row(other)["stage_recorded"], 1)


class LimitTests(ApplyCase):
    def setUp(self):
        super().setUp()
        # A fixed noon, so no test here straddles a day boundary by accident.
        self.base = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)

    def block(self, *, mode="one_click", company=BLUEFIN, board="bluefin", now=None):
        return apply_runs.limits_block(self.conn, USER, employer_key(company), board, mode, now or self.at())

    def stamp(self, minutes):
        return self.at(minutes).isoformat(timespec="microseconds")

    def test_the_defaults_are_the_students_d3_and_d4_answers(self):
        self.assertEqual(apply_runs.limits(self.conn, USER), {
            "spacing_minutes": 10, "daily_cap": 5, "company_days": 30, "rehearsals_per_day": 20, "rehearsals_before_submit": 3,
            "unattended_per_hour": 1, "unattended_daily_cap": 3,
        })
        self.set_limits(spacing_minutes=15, daily_cap=True, company_days=0, rehearsals_per_day="9", rehearsals_before_submit=4)
        values = apply_runs.limits(self.conn, USER)
        self.assertEqual((values["spacing_minutes"], values["daily_cap"], values["company_days"], values["rehearsals_per_day"],
                          values["rehearsals_before_submit"]), (15, 5, 30, 20, 4), "a value that is not sound is the default")

    def test_spacing_between_two_hand_overs(self):
        self.raw_claim(state="submitted", handed_over_at=self.stamp(-10), company="Orbit Systems", board="orbit")
        self.assertIsNone(self.block(now=self.at(0)), "exactly 10 minutes: allowed")
        allowed = f"{self.at(0):%I:%M %p}".lstrip("0")
        self.assertEqual(self.block(now=self.at(-0.5)), f"The next agent submission is allowed at {allowed}")

    def test_the_daily_cap_counts_one_click_and_unattended_but_not_finish_in_browser(self):
        self.set_limits(daily_cap=2, spacing_minutes=1)
        self.raw_claim(state="submitted", mode="one_click", handed_over_at=self.stamp(-300), company="Orbit Systems", board="orbit")
        self.raw_claim(state="submitted", mode="handoff", handed_over_at=self.stamp(-200), company="Acme", board="acme")
        self.raw_claim(state="failed", mode="handoff", handed_over_at=self.stamp(-100), company="Nimbus", board="nimbus")
        self.assertIsNone(self.block(mode="one_click", now=self.at(0)), "finish in browser is not counted: one of two")
        self.raw_claim(state="unconfirmed", mode="unattended", handed_over_at=self.stamp(-50), company="Vega", board="vega")
        self.assertIn("today's limit of 2 agent submissions", self.block(mode="one_click", now=self.at(0)))
        self.assertIsNone(self.block(mode="handoff", now=self.at(0)), "the cap does not apply to Finish in browser")

    def test_the_cap_is_counted_in_the_students_own_day(self):
        with mock.patch.dict(os.environ, {"PIPELINE_TIMEZONE": "America/Chicago"}):
            self.set_limits(daily_cap=1, spacing_minutes=1)
            # 23:30 on Sep 29 in Chicago is 04:30 UTC on Sep 30; local midnight is 05:00 UTC.
            now = datetime(2026, 9, 30, 4, 30, tzinfo=timezone.utc)
            self.raw_claim(state="submitted", mode="one_click", handed_over_at="2026-09-29T04:59:59.000000+00:00", company="Orbit Systems", board="orbit")
            self.assertIsNone(self.block(mode="one_click", now=now), "23:59 the day before, local time")
            self.raw_claim(state="submitted", mode="one_click", handed_over_at="2026-09-29T05:00:00.000000+00:00", company="Acme", board="acme")
            self.assertIn("today's limit of 1", self.block(mode="one_click", now=now), "00:00 local time is today")
            self.assertIsNone(self.block(mode="one_click", now=datetime(2026, 9, 30, 5, 0, tzinfo=timezone.utc)), "the next local day starts over")

    def test_the_company_limit_matches_the_name_or_the_board(self):
        handed = self.stamp(-60 * 24 * 12)
        self.raw_claim(state="submitted", handed_over_at=handed, company="Bluefin Robotics, Inc.", board="bluefin-hire")
        self.assertEqual(self.block(company="BLUEFIN robotics"), "You applied to Bluefin Robotics, Inc. with Apply for me 12 days ago",
                         "the same company under another spelling")
        self.assertIn("with Apply for me 12 days ago", self.block(company="Bluefin Labs", board="bluefin-hire"), "the same Greenhouse board")
        self.assertIsNone(self.block(company="Orbit Systems", board="orbit"))
        self.assertIsNone(self.block(now=self.at(60 * 24 * 19 + 1)), "after 30 days")
        self.assertIsNotNone(self.block(now=self.at(60 * 24 * 17)))

    def test_the_company_limit_can_be_overridden_for_one_attempt_and_the_tick_is_recorded(self):
        self.raw_claim(state="submitted", mode="handoff", handed_over_at=self.stamp(-60 * 24 * 3), company=BLUEFIN, board="bluefin")
        self.opportunity("job-9")
        with self.assertRaises(ClaimRefused) as caught:
            self.start("job-9", "handoff", now=self.at(0))
        self.assertEqual((caught.exception.code, caught.exception.ask), ("company_limit", True))
        claim = self.start("job-9", "handoff", now=self.at(1), acknowledged=("company_limit",))
        self.assertEqual(json.loads(self.claim_row(claim["token"])["detail_json"])["acknowledged"], ["company_limit"])

    def test_a_retry_right_after_a_security_code_attempt_is_blocked_by_spacing(self):
        first = self.start("job-1", "handoff", now=self.at(0))
        apply_runs.hand_over(self.conn, first["token"], user_id=USER, now=self.at(1))
        apply_runs.settle(self.conn, first["token"], user_id=USER, state="needs_you", note="Greenhouse asked for a security code", now=self.at(2))
        with self.assertRaises(ClaimRefused) as caught:
            self.start("job-1", "handoff", retry=True, now=self.at(3))
        self.assertEqual(str(caught.exception), "Greenhouse asked for a security code", "it reached Greenhouse, so a retry cannot release it")
        apply_runs.resolve_uncertain(self.conn, first["token"], user_id=USER, went_through=False, now=self.at(4))
        with self.assertRaises(ClaimRefused) as caught:
            self.start("job-1", "handoff", retry=True, acknowledged=("released_job", "company_limit"), now=self.at(5))
        self.assertEqual(caught.exception.code, "spacing", "it counts toward the spacing whatever the student said about it")

    def test_didnt_go_through_still_counts_for_the_company_limit_and_asks_again_for_the_job(self):
        token = self.raw_claim(state="unconfirmed", mode="handoff", handed_over_at=self.stamp(-60 * 24), company=BLUEFIN, board="bluefin",
                               job_ref="bluefin/7")
        apply_runs.resolve_uncertain(self.conn, token, user_id=USER, went_through=False, now=self.at(0))
        self.assertEqual(self.claim_row(token)["state"], "released")
        self.assertIn("with Apply for me 1 day ago", self.block(mode="handoff"))
        self.opportunity("job-7")
        with self.assertRaises(ClaimRefused) as caught:
            self.start("job-7", "handoff", job="bluefin/7", now=self.at(1), acknowledged=("company_limit",))
        self.assertEqual(caught.exception.code, "released_job")

    def test_unattended_has_its_own_hourly_and_daily_caps(self):
        self.set_limits(spacing_minutes=1, daily_cap=50, company_days=1)
        self.raw_claim(state="submitted", mode="unattended", handed_over_at=self.stamp(-30), company="Orbit Systems", board="orbit")
        self.assertIn("at most one application an hour", self.block(mode="unattended", company="Acme", board="acme", now=self.at(0)))
        self.assertIsNone(self.block(mode="one_click", company="Acme", board="acme", now=self.at(0)))
        for offset, name in ((-200, "Nimbus"), (-100, "Vega")):
            self.raw_claim(state="submitted", mode="unattended", handed_over_at=self.stamp(offset), company=name, board=name.lower())
        self.assertIn("at most 3 applications a day", self.block(mode="unattended", company="Acme", board="acme", now=self.at(61)))

    def test_rehearsals_and_lookups_are_limited_per_local_day(self):
        self.set_limits(rehearsals_per_day=2)
        with mock.patch.dict(os.environ, {"PIPELINE_TIMEZONE": "America/Chicago"}):
            now = datetime(2026, 9, 30, 4, 30, tzinfo=timezone.utc)
            before_midnight = datetime(2026, 9, 29, 4, 59, tzinfo=timezone.utc)
            self.make_run("rehearsal", started=before_midnight)
            self.make_run("lookup", started=datetime(2026, 9, 29, 5, 0, tzinfo=timezone.utc))
            self.assertIsNone(apply_runs.rehearsal_block(self.conn, USER, now), "one today; the other was yesterday, local time")
            self.make_run("submit", started=datetime(2026, 9, 29, 6, 0, tzinfo=timezone.utc))
            self.assertIsNone(apply_runs.rehearsal_block(self.conn, USER, now), "a submit is not a rehearsal")
            self.make_run("rehearsal", started=datetime(2026, 9, 29, 7, 0, tzinfo=timezone.utc))
            self.assertIn("today's limit of 2 rehearsals and option lookups", apply_runs.rehearsal_block(self.conn, USER, now))
            self.assertIsNone(apply_runs.rehearsal_block(self.conn, USER, datetime(2026, 9, 30, 5, 30, tzinfo=timezone.utc)))


class GateTests(ApplyCase):
    def test_the_gate_counts_distinct_companies_with_clean_rehearsals_marked_right(self):
        self.assertEqual(apply_runs.gate(self.conn, USER, "greenhouse"), (False, 0, 3))
        self.reviewed_rehearsal(BLUEFIN, 0)
        self.reviewed_rehearsal(BLUEFIN, 5)                        # the same company again: still one
        self.reviewed_rehearsal("Orbit Systems", 10, clean=False)  # not clean
        self.reviewed_rehearsal("Nimbus Labs", 15, verdict="wrong")
        self.reviewed_rehearsal("Vega Motors", 20, outcome="needs_you")
        unreviewed = self.make_run(company="Helio Works", started=self.at(25))
        apply_runs.finish_run(self.conn, unreviewed, outcome="rehearsed", clean=True, now=self.at(26))
        self.assertEqual(apply_runs.gate(self.conn, USER, "greenhouse"), (False, 1, 3))
        self.reviewed_rehearsal("Orbit Systems", 30)
        self.reviewed_rehearsal("Nimbus Labs", 35)
        self.assertEqual(apply_runs.gate(self.conn, USER, "greenhouse"), (True, 3, 3))
        self.assertEqual(apply_runs.gate(self.conn, USER, "lever"), (False, 0, 3), "per ATS")

    def test_only_the_current_adapter_version_counts_and_the_needed_number_is_the_students(self):
        self.set_limits(rehearsals_before_submit=1)
        old = self.make_run(company=BLUEFIN, started=self.at(0), adapter_version="greenhouse-0")
        apply_runs.finish_run(self.conn, old, outcome="rehearsed", clean=True, now=self.at(1))
        apply_runs.mark_review(self.conn, old, user_id=USER, verdict="right", now=self.at(2))
        self.assertEqual(apply_runs.gate(self.conn, USER, "greenhouse"), (False, 0, 1), "the adapter changed since")
        self.assertEqual(apply_runs.gate(self.conn, USER, "greenhouse", adapter_version="greenhouse-0"), (True, 1, 1))

    def test_only_a_finished_rehearsal_submit_or_handoff_can_be_marked(self):
        lookup = self.make_run("lookup", started=self.at(0))
        apply_runs.finish_run(self.conn, lookup, outcome="looked_up", now=self.at(1))
        running = self.make_run("rehearsal", started=self.at(2))
        for run_id in (lookup, running):
            with self.assertRaises(ValueError):
                apply_runs.mark_review(self.conn, run_id, user_id=USER, verdict="right", now=self.at(3))
        with self.assertRaises(LookupError):
            apply_runs.mark_review(self.conn, "run-missing", user_id=USER, verdict="right", now=self.at(3))
        with self.assertRaises(ValueError):
            apply_runs.mark_review(self.conn, lookup, user_id=USER, verdict="maybe", now=self.at(3))

    def test_two_wrong_marks_among_the_last_five_reset_the_gate(self):
        for minute, name in enumerate(("Bluefin Robotics", "Orbit Systems", "Nimbus Labs")):
            self.reviewed_rehearsal(name, minute * 10)
        self.assertEqual(apply_runs.gate(self.conn, USER, "greenhouse")[0], True)
        self.assertEqual(apply_runs.gate_reset_at(self.conn, USER, "greenhouse"), "")
        self.reviewed_rehearsal("Vega Motors", 40, verdict="wrong")
        self.assertEqual(apply_runs.gate(self.conn, USER, "greenhouse")[:2], (True, 3), "one wrong mark is not enough")
        wrong = self.make_run(company="Helio Works", started=self.at(50))
        apply_runs.finish_run(self.conn, wrong, outcome="submitted", now=self.at(51))
        result = apply_runs.mark_review(self.conn, wrong, user_id=USER, verdict="wrong", now=self.at(52))
        self.assertTrue(result["breaker_tripped"], "a submit marked wrong counts too")
        self.assertEqual(_parsed(apply_runs.gate_reset_at(self.conn, USER, "greenhouse")), self.at(52))
        self.assertEqual(apply_runs.gate(self.conn, USER, "greenhouse"), (False, 0, 3), "older rehearsals stop counting")
        self.assertTrue(any("2 of your last 5 reviews were wrong" in title for title in self.notices()))
        # New clean rehearsals after the reset count, and the two old wrong marks do not trip it again.
        self.reviewed_rehearsal("Bluefin Robotics", 60)
        self.assertEqual(apply_runs.gate(self.conn, USER, "greenhouse")[:2], (False, 1))
        self.assertEqual(_parsed(apply_runs.gate_reset_at(self.conn, USER, "greenhouse")), self.at(52))
        self.reviewed_rehearsal("Orbit Systems", 70)
        self.reviewed_rehearsal("Nimbus Labs", 80)
        self.assertEqual(apply_runs.gate(self.conn, USER, "greenhouse"), (True, 3, 3))


class ReaderTests(ApplyCase):
    def test_in_flight_lists_a_held_clicking_claim_and_not_a_claimed_one(self):
        held = self.raw_claim(state="clicking", instance=SERVER_INSTANCE, mode="handoff", handed_over_at=self.at(-2).isoformat(timespec="microseconds"))
        self.raw_claim(state="claimed", mode="handoff")
        self.raw_claim(state="submitted", mode="one_click", handed_over_at=self.at(-2).isoformat(timespec="microseconds"))
        self.assertEqual(automation.in_flight(self.conn, USER), [], "not held: this process is not running it")
        apply_runs.RUNNING.add(held)
        items = automation.in_flight(self.conn, USER)
        self.assertEqual([(item["action"], item["source"], item["company"], item["kind"]) for item in items],
                         [("application", "apply_claim", BLUEFIN, "handoff")])

    def test_unconfirmed_lists_attempts_that_may_have_reached_greenhouse(self):
        stamp = self.at(-30).isoformat(timespec="microseconds")
        self.raw_claim(state="unconfirmed", mode="handoff", handed_over_at=stamp, updated_at=stamp)
        self.raw_claim(state="needs_you", mode="handoff", handed_over_at=stamp, after_click=1, updated_at=stamp)
        self.raw_claim(state="failed", mode="one_click", handed_over_at=stamp, after_click=1, updated_at=stamp)
        self.raw_claim(state="needs_you", mode="handoff", after_click=0)                # nothing was sent
        self.raw_claim(state="released", mode="handoff", handed_over_at=stamp, after_click=1)  # the student settled it
        self.raw_claim(state="submitted", mode="handoff", handed_over_at=stamp)
        self.raw_claim(state="clicking", instance=FOREIGN, handed_over_at=stamp, heartbeat_at=self.at(-20).isoformat(timespec="microseconds"))
        self.raw_claim(state="clicking", instance=FOREIGN, handed_over_at=stamp, heartbeat_at=utc_now())  # held: in flight, not unconfirmed
        items = automation.unconfirmed(self.conn, USER, now=self.at())
        self.assertEqual([item["action"] for item in items], ["application"] * 4)
        self.assertEqual({item["company"] for item in items}, {BLUEFIN})

    def test_the_pause_text_reads_correctly_with_one_two_and_three_kinds(self):
        base = automation.PAUSED_BANNER
        email = {"action": "send"}
        form = {"action": "form"}
        application = {"action": "application"}
        self.assertEqual(automation.paused_text([application]), f"{base} 1 application was already being submitted and can't be stopped.")
        self.assertEqual(automation.paused_text([application, application]), f"{base} 2 applications were already being submitted and can't be stopped.")
        self.assertEqual(automation.paused_text([email, application]),
                         f"{base} 1 email was already handed to Gmail and 1 application was already being submitted, and neither can be stopped.")
        self.assertEqual(
            automation.paused_text([email, form, application]),
            f"{base} 1 email was already handed to Gmail, 1 contact form was already being sent, and 1 application was already being submitted, "
            "and none of them can be stopped.",
        )
        self.assertEqual(automation.paused_text([]), base)

    def test_pausing_reports_an_application_already_handed_over(self):
        held = self.raw_claim(state="clicking", instance=SERVER_INSTANCE, mode="handoff", handed_over_at=self.at(-1).isoformat(timespec="microseconds"))
        apply_runs.RUNNING.add(held)
        result = automation.set_paused(self.conn, USER, True)
        self.assertEqual([item["action"] for item in result["in_flight"]], ["application"])
        summary = automation.health_summary(self.conn, USER)
        self.assertIn("1 application was already being submitted and can't be stopped.", summary["banner"][0]["text"])

    def test_the_urgent_kinds_exist_in_both_registries(self):
        for kind in ("apply_needs_you", "apply_no_email"):
            self.assertIn(kind, urgent.DATE_SOURCE_LABELS)
            self.assertIn(kind, urgent.KIND_PRIORITY)
        self.assertEqual(set(urgent.DATE_SOURCE_LABELS), set(urgent.KIND_PRIORITY), "a kind in only one raises KeyError in the queue")

    def test_urgent_calls_an_attempt_that_may_have_reached_greenhouse_uncertain_and_lets_a_stopped_one_age_out(self):
        old = self.at(-60 * 24 * 90).isoformat(timespec="microseconds")
        handed = self.at(-60 * 24 * 90 - 5).isoformat(timespec="microseconds")
        after = self.raw_claim(state="needs_you", mode="handoff", handed_over_at=handed, updated_at=old)  # a security code, after the POST
        before = self.raw_claim(state="needs_you", mode="handoff", updated_at=self.at(-60).isoformat(timespec="microseconds"))
        stale = self.raw_claim(state="needs_you", mode="handoff", updated_at=old)
        moved = self.raw_claim(state="needs_you", mode="handoff", updated_at=self.at(-60).isoformat(timespec="microseconds"))
        with self.conn:
            self.conn.execute("UPDATE applications SET stage='rejected' WHERE id=?", (self.claim_row(moved)["application_id"],))
        found = {item["key"]: item for item in urgent.urgent_queue(self.conn, user_id=USER, now=self.at())["items"] if item["kind"].startswith("apply_")}
        self.assertEqual(found[f"apply_needs_you:{after}"]["subtitle"], "It may or may not have gone through")
        self.assertEqual(found[f"apply_needs_you:{before}"]["subtitle"], "It needs you")
        self.assertNotIn(f"apply_needs_you:{stale}", found, "an attempt that sent nothing ages out like any other row")
        self.assertNotIn(f"apply_needs_you:{moved}", found, "the role moved on, so there is nothing left to ask")

    def test_urgent_lists_attempts_that_need_the_student_and_submissions_no_email_came_for(self):
        old = self.at(-60 * 24 * 90).isoformat(timespec="microseconds")
        needs = self.raw_claim(state="needs_you", mode="handoff", handed_over_at=old, updated_at=old)
        unconfirmed = self.raw_claim(state="unconfirmed", mode="handoff", handed_over_at=old, updated_at=self.at(-60 * 25).isoformat(timespec="microseconds"))
        silent = self.raw_claim(state="submitted", mode="handoff", handed_over_at=old, verification="no_email_24h", stage_recorded=1,
                                updated_at=self.at(-60 * 31).isoformat(timespec="microseconds"))
        self.raw_claim(state="submitted", mode="handoff", handed_over_at=old, verification="awaiting_email", stage_recorded=1)
        self.raw_claim(state="claimed", mode="handoff")
        with self.conn:
            self.conn.execute("UPDATE application_submit_claims SET verified_at=? WHERE token=?", (self.at(-60 * 30).isoformat(timespec="microseconds"), silent))
        queue = urgent.urgent_queue(self.conn, user_id=USER, now=self.at())
        found = {item["key"]: item for item in queue["items"] if item["kind"].startswith("apply_")}
        self.assertEqual(set(found), {f"apply_needs_you:{needs}", f"apply_needs_you:{unconfirmed}", f"apply_no_email:{silent}"},
                         "a 90 day old attempt that needs the student is still there")
        self.assertEqual(found[f"apply_no_email:{silent}"]["date_source"], "Confirmation email watch")
        self.assertEqual(found[f"apply_no_email:{silent}"]["subtitle"], "No confirmation email yet. Some employers don't send one")
        self.assertTrue(all(item["overdue"] for item in found.values()))


class WorkerStepTests(ApplyCase):
    def worker(self, apply_root=None):
        return AutomationWorker(self.path, fetcher_factory=lambda: None, apply_root=apply_root)

    def test_a_student_with_apply_for_me_off_still_gets_an_open_claim_finished(self):
        self.assertEqual(automation.mode(self.conn, USER, "form_submission"), "off")
        token = self.raw_claim(state="claimed", mode="handoff", instance=SERVER_INSTANCE)
        report = self.worker().run_once()
        self.assertEqual(report["apply"]["recovered"], {USER: {"failed": 1, "unconfirmed": 0, "stage_retried": 0, "runs_failed": 0}})
        self.assertEqual(self.claim_row(token)["state"], "failed")
        health = {row["component"]: row for row in automation.health_summary(self.conn, USER)["components"]}
        self.assertIn("apply_agent.runner", health)
        self.assertEqual(health["apply_agent.runner"]["last_error"], "")

    def test_a_pass_with_nothing_open_reports_nothing_and_a_stopped_claim_is_not_recovered_twice(self):
        report = self.worker().run_once()
        self.assertNotIn("apply", report)
        self.raw_claim(state="unconfirmed", mode="handoff", handed_over_at=utc_now())
        self.assertNotIn("apply", self.worker().run_once(), "an uncertain attempt is watched, never touched")

    def test_a_student_is_watched_only_while_something_of_theirs_is_open(self):
        def watched():
            return apply_runs.students_to_watch(self.conn, self.at())

        def clear():
            with self.conn:
                self.conn.execute("DELETE FROM application_submit_claims")
                self.conn.execute("DELETE FROM apply_runs")

        recent = self.at(-60 * 24 * 3).isoformat(timespec="microseconds")
        old = self.at(-60 * 24 * 15).isoformat(timespec="microseconds")
        self.assertEqual(watched(), [])
        cases = [
            (dict(state="claimed", mode="handoff"), True),
            (dict(state="clicking", mode="handoff", handed_over_at=recent), True),
            (dict(state="unconfirmed", mode="handoff", handed_over_at=recent), True),
            (dict(state="unconfirmed", mode="handoff", handed_over_at=old), False),
            (dict(state="released", mode="handoff", handed_over_at=recent, after_click=1), True),
            (dict(state="released", mode="handoff", handed_over_at=old, after_click=1), False),
            (dict(state="needs_you", mode="handoff", after_click=0), False),
            (dict(state="submitted", mode="handoff", handed_over_at=recent, verification="awaiting_email", submitted_at=recent, stage_recorded=1), True),
            (dict(state="submitted", mode="handoff", handed_over_at=old, verification="awaiting_email", submitted_at=old, stage_recorded=1), False),
            (dict(state="submitted", mode="one_click", handed_over_at=old, submitted_at=old, stage_policy="record", stage_recorded=0), True),
            (dict(state="submitted", mode="handoff", handed_over_at=old, submitted_at=old, stage_policy="ask", stage_recorded=0), False),
        ]
        for fields, expected in cases:
            with self.subTest(fields=fields):
                clear()
                self.raw_claim(**fields)
                self.assertEqual(watched(), [USER] if expected else [])
        clear()
        self.make_run("rehearsal", started=self.at(-1))
        self.assertEqual(watched(), [USER], "a run that is still running")

    def test_a_failure_for_one_student_is_recorded_and_does_not_stop_the_pass(self):
        self.raw_claim(state="claimed", mode="handoff", instance=SERVER_INSTANCE)
        with mock.patch.object(apply_runs, "recover_stale", side_effect=RuntimeError("the database went away")):
            report = self.worker().run_once()
        self.assertNotIn("apply", report)
        row = self.conn.execute("SELECT last_error FROM automation_health WHERE user_id=? AND component='apply_agent.runner'", (USER,)).fetchone()
        self.assertIn("the database went away", row["last_error"])

    def test_old_screenshots_are_deleted_once_per_local_day(self):
        apply_root = self.root / "apply"
        old = self.screenshot(apply_root, "op-1", days_ago=200)
        self.worker(apply_root).run_once()
        self.assertFalse(old.exists())
        second = self.screenshot(apply_root, "op-2", days_ago=200)
        report = self.worker(apply_root).run_once()
        self.assertTrue(second.exists(), "once a local day")
        self.assertNotIn("apply", report)
        with self.conn:
            self.conn.execute("DELETE FROM user_settings WHERE user_id=? AND key=?", (USER, apply_runs.PURGE_LAST_RUN_KEY))
        report = self.worker(apply_root).run_once()
        self.assertFalse(second.exists())
        self.assertEqual(report["apply"]["purged"], [USER])

    def test_a_purge_that_works_again_records_its_own_health_after_a_failure(self):
        apply_root = self.root / "apply"
        with mock.patch.object(apply_runs, "purge_evidence", side_effect=RuntimeError("the disk went away")):
            self.worker(apply_root).run_once()
        health = {row["component"]: row for row in automation.health_summary(self.conn, USER)["components"]}
        self.assertIn("the disk went away", health["apply_agent.retention"]["last_error"])
        self.worker(apply_root).run_once()
        row = self.conn.execute("SELECT last_ok_at, last_error_at FROM automation_health WHERE user_id=? AND component='apply_agent.retention'", (USER,)).fetchone()
        self.assertTrue(row["last_ok_at"] and row["last_ok_at"] > row["last_error_at"], "the next good purge clears the Error chip")
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM automation_health WHERE user_id=? AND component='apply_agent.runner' AND last_error_at IS NOT NULL", (USER,)).fetchone(),
            "the runner's own status is not mixed with retention")

    def test_without_an_apply_root_no_file_is_ever_touched(self):
        apply_root = self.root / "apply"
        old = self.screenshot(apply_root, "op-1", days_ago=200)
        self.worker(None).run_once()
        self.assertTrue(old.exists())

    def screenshot(self, apply_root, opportunity_id, *, days_ago, minutes_ago=0):
        folder = apply_root / apply_runs.user_folder(USER) / opportunity_id
        folder.mkdir(parents=True, exist_ok=True)
        run_id = self.make_run(opportunity_id=opportunity_id, started=self.at(-60 * 24 * days_ago - minutes_ago))
        path = folder / f"{run_id}-fill.png"
        path.write_bytes(b"fake image bytes")
        apply_runs.finish_run(self.conn, run_id, outcome="rehearsed", now=self.at(-60 * 24 * days_ago),
                              screenshots=[{"step": "fill", "path": str(path), "sha256": "cd" * 32, "masked": ["phone"]}])
        return path


class RetentionTests(ApplyCase):
    def shot(self, apply_root, opportunity_id, *, days_ago, status="finished", stored=None):
        folder = apply_root / apply_runs.user_folder(USER) / opportunity_id
        folder.mkdir(parents=True, exist_ok=True)
        run_id = self.make_run(opportunity_id=opportunity_id, started=self.at(-60 * 24 * days_ago))
        path = folder / f"{run_id}-fill.png"
        path.write_bytes(b"x")
        if status == "finished":
            apply_runs.finish_run(self.conn, run_id, outcome="rehearsed", now=self.at(-60 * 24 * days_ago),
                                  screenshots=[{"step": "fill", "path": stored or str(path), "sha256": "ef" * 32, "masked": []}])
        return run_id, path

    def screenshots(self, run_id):
        return json.loads(apply_runs.get_run(self.conn, run_id, user_id=USER)["screenshots_json"])

    def test_screenshots_older_than_the_window_go_and_their_hashes_stay(self):
        apply_root = self.root / "apply"
        old_id, old = self.shot(apply_root, "op-old", days_ago=91)
        edge_id, edge = self.shot(apply_root, "op-edge", days_ago=89)
        counts = apply_runs.purge_evidence(self.conn, apply_root=apply_root, now=self.at())
        self.assertEqual(counts["apply_screenshots_removed"], 1)
        self.assertFalse(old.exists())
        self.assertTrue(edge.exists())
        self.assertEqual(self.screenshots(old_id), [{"step": "fill", "path": "", "sha256": "ef" * 32, "masked": []}], "the hash is kept for good")
        self.assertEqual(self.screenshots(edge_id)[0]["path"], str(edge))
        self.assertEqual(apply_runs.purge_evidence(self.conn, apply_root=apply_root, now=self.at())["apply_screenshots_removed"], 0, "counted once")

    def test_the_window_is_the_students_setting(self):
        apply_root = self.root / "apply"
        _, path = self.shot(apply_root, "op-1", days_ago=40)
        with mock.patch.dict(os.environ, {"PIPELINE_APPLY_EVIDENCE_DAYS": "30"}):
            self.assertEqual(apply_runs.evidence_days(), 30)
            apply_runs.purge_evidence(self.conn, apply_root=apply_root, now=self.at())
        self.assertFalse(path.exists())
        for bad in ("soon", "0", "-5", ""):
            with mock.patch.dict(os.environ, {"PIPELINE_APPLY_EVIDENCE_DAYS": bad}):
                self.assertEqual(apply_runs.evidence_days(), 90, bad)

    def test_a_file_no_run_names_is_removed_but_never_a_working_runs_or_the_hash_key(self):
        apply_root = self.root / "apply"
        _, kept = self.shot(apply_root, "op-1", days_ago=1)
        _, running = self.shot(apply_root, "op-2", days_ago=0, status="running")
        folder = apply_root / apply_runs.user_folder(USER) / "op-3"
        folder.mkdir(parents=True)
        orphan = folder / f"run-{'0' * 32}-fill.png"
        orphan.write_bytes(b"crashed mid-screenshot")
        os.utime(orphan, (self.at(-60).timestamp(), self.at(-60).timestamp()))  # a crash orphan is old
        (apply_root / "hash-key").write_bytes(b"k" * 32)
        elsewhere = apply_root / "not-a-user-folder"
        elsewhere.mkdir()
        (elsewhere / "note.txt").write_text("keep", encoding="utf-8")
        counts = apply_runs.purge_evidence(self.conn, apply_root=apply_root, now=self.at())
        self.assertEqual(counts["apply_orphan_files_removed"], 1)
        self.assertFalse(orphan.exists())
        self.assertFalse(folder.exists(), "an emptied folder goes with it")
        self.assertTrue(kept.exists() and running.exists())
        self.assertEqual((apply_root / "hash-key").read_bytes(), b"k" * 32)
        self.assertTrue((elsewhere / "note.txt").exists())

    def test_a_file_made_moments_ago_is_left_even_when_no_run_names_it_yet(self):
        # A run can save a screenshot and finish between the two reads of the sweep; a fresh file is never a crash orphan.
        apply_root = self.root / "apply"
        folder = apply_root / apply_runs.user_folder(USER) / "op-1"
        folder.mkdir(parents=True)
        fresh = folder / f"run-{'2' * 32}-fill.png"
        fresh.write_bytes(b"just saved")
        self.assertEqual(apply_runs.purge_evidence(self.conn, apply_root=apply_root, now=datetime.now(timezone.utc))["apply_orphan_files_removed"], 0)
        self.assertTrue(fresh.exists())
        later = datetime.now(timezone.utc) + timedelta(minutes=10)
        self.assertEqual(apply_runs.purge_evidence(self.conn, apply_root=apply_root, now=later)["apply_orphan_files_removed"], 1)

    def test_a_path_outside_the_folder_is_cleared_but_the_file_is_left_alone(self):
        apply_root = self.root / "apply"
        outside = self.root / "outside.png"
        outside.write_bytes(b"not ours")
        run_id, _ = self.shot(apply_root, "op-1", days_ago=200, stored=str(outside))
        apply_runs.purge_evidence(self.conn, apply_root=apply_root, now=self.at())
        self.assertTrue(outside.exists())
        self.assertEqual(self.screenshots(run_id)[0]["path"], "")

    def test_progress_is_trimmed_to_its_last_step_once_a_run_is_thirty_days_old(self):
        apply_root = self.root / "apply"
        steps = [{"at": "t", "step": name, "text": name} for name in ("open", "fill", "read back")]
        old = self.make_run(started=self.at(-60 * 24 * 31))
        recent = self.make_run(started=self.at(-60 * 24 * 5))
        for run_id in (old, recent):
            apply_runs.finish_run(self.conn, run_id, outcome="rehearsed", now=self.at(-1))
            with self.conn:
                self.conn.execute("UPDATE apply_runs SET progress_json=? WHERE id=?", (json.dumps(steps), run_id))
        self.assertEqual(apply_runs.purge_evidence(self.conn, apply_root=apply_root, now=self.at())["apply_progress_trimmed"], 1)
        self.assertEqual(json.loads(apply_runs.get_run(self.conn, old, user_id=USER)["progress_json"]), steps[-1:])
        self.assertEqual(json.loads(apply_runs.get_run(self.conn, recent, user_id=USER)["progress_json"]), steps)

    def test_run_retention_does_the_same_when_given_the_folder_and_nothing_when_not(self):
        apply_root = self.root / "apply"
        _, old = self.shot(apply_root, "op-1", days_ago=120)
        without = run_retention(self.conn, now=self.at())
        self.assertNotIn("apply_screenshots_removed", without)
        self.assertTrue(old.exists(), "a test or sandbox that names no folder never purges one")
        counts = run_retention(self.conn, now=self.at(), apply_root=apply_root)
        self.assertEqual(counts["apply_screenshots_removed"], 1)
        self.assertFalse(old.exists())

    def test_one_students_purge_leaves_another_students_folder(self):
        apply_root = self.root / "apply"
        other = apply_root / apply_runs.user_folder("someone-else")
        other.mkdir(parents=True)
        (other / f"run-{'1' * 32}-fill.png").write_bytes(b"theirs")
        apply_runs.purge_evidence(self.conn, apply_root=apply_root, now=self.at(), user_id=USER)
        self.assertTrue(any(other.iterdir()))


class DeletionAndExport(ApplyCase):
    def populate(self, apply_root):
        claim = self.start("job-1", "handoff", now=self.at(1))
        folder = apply_root / apply_runs.user_folder(USER) / "job-1"
        folder.mkdir(parents=True)
        shot = folder / "run-x-fill.png"
        shot.write_bytes(b"pixels")
        run_id = self.make_run(opportunity_id="job-1", started=self.at(1), claim_token=claim["token"])
        apply_runs.finish_run(self.conn, run_id, outcome="rehearsed", now=self.at(2), screenshots=[
            {"step": "fill", "path": str(shot), "sha256": "ab" * 32, "masked": ["phone"]}, {"step": "done", "path": "", "sha256": "cd" * 32, "masked": []},
        ], plan=[{"key": "first_name", "value_mac": "f" * 64}])
        stamp = utc_now()
        with self.conn:
            self.conn.execute(
                "INSERT INTO apply_ats_labels(user_id, ats, field, label, confirmed_at) VALUES(?, 'greenhouse', 'school', 'Example University - Springfield', ?)",
                (USER, stamp),
            )
            self.conn.execute(
                "INSERT INTO apply_sensitive_answers(id, user_id, category, question_text, question_key, question_hash, answer_kind, answer, "
                "consent_scope, consented_at, created_at, updated_at) VALUES('sa-1', ?, 'work_authorization', 'Are you authorized to work?', "
                "'are you authorized to work', 'h', 'option', 'Decline to self-identify', 'confirmed', ?, ?, ?)", (USER, stamp, stamp, stamp),
            )
        return claim, run_id, shot

    def test_the_export_holds_the_new_tables_with_screenshot_paths_redacted_and_never_the_key(self):
        apply_root = self.root / "apply"
        claim, run_id, shot = self.populate(apply_root)
        (apply_root / "hash-key").write_bytes(bytes(range(32)))
        exported = export_account(self.conn, user_id=USER)
        for key in ("application_submit_claims", "apply_runs", "apply_sensitive_answers", "apply_ats_labels"):
            self.assertIn(key, ACCOUNT_QUERIES)
            self.assertEqual(len(exported[key]), 1, key)
        self.assertEqual(exported["application_submit_claims"][0]["token"], claim["token"])
        shots = json.loads(exported["apply_runs"][0]["screenshots_json"])
        self.assertEqual([item["path"] for item in shots], ["[private-file-reference-redacted]", ""], "only a path that is there is redacted")
        self.assertEqual([item["sha256"] for item in shots], ["ab" * 32, "cd" * 32], "the hashes go with it")
        text = json.dumps(exported)
        self.assertNotIn(str(shot), text)
        self.assertNotIn(str(apply_root), text)
        self.assertNotIn(bytes(range(32)).hex(), text, "the key that hashes values is never exported")
        self.assertEqual(exported["apply_ats_labels"][0]["label"], "Example University - Springfield")
        self.assertEqual(exported["apply_sensitive_answers"][0]["answer"], "Decline to self-identify")
        self.assertEqual(exported["apply_runs"][0]["id"], run_id)

    def test_deleting_the_account_removes_the_students_folder_and_cascades_the_rows(self):
        apply_root = self.root / "apply"
        _, _, shot = self.populate(apply_root)
        other = apply_root / apply_runs.user_folder("someone-else")
        other.mkdir()
        (other / "keep.png").write_bytes(b"theirs")
        (apply_root / "hash-key").write_bytes(b"k" * 32)
        result = delete_account(self.conn, [self.root / "r", self.root / "c", self.root / "i"], user_id=USER, apply_root=apply_root)
        self.assertEqual(result["files_removed"], 1)
        self.assertFalse(shot.exists())
        self.assertFalse((apply_root / apply_runs.user_folder(USER)).exists())
        self.assertTrue((other / "keep.png").exists() and (apply_root / "hash-key").exists())
        for table in ("application_submit_claims", "apply_runs", "apply_sensitive_answers", "apply_ats_labels"):
            with self.subTest(table=table):
                self.assertEqual(self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0, "cascaded with the account")

    def test_the_existing_callers_that_pass_no_folder_still_work(self):
        self.populate(self.root / "apply")
        result = delete_account(self.conn, [self.root / "r", self.root / "c", self.root / "i"], user_id=USER)
        self.assertTrue(result["deleted"])
        self.assertTrue((self.root / "apply" / apply_runs.user_folder(USER) / "job-1" / "run-x-fill.png").exists(), "no folder named, none touched")

    def test_the_api_deletes_the_folder_it_was_given(self):
        from fastapi.testclient import TestClient
        from opportunity_app.api import create_app

        apply_root = self.root / "apply"
        _, _, shot = self.populate(apply_root)
        app = create_app(db_path=self.path, access_token="t", resume_storage=self.root / "r", capture_storage=self.root / "c",
                         interview_storage=self.root / "i", apply_storage=apply_root, start_call_prep_worker=False,
                         start_inbox_watcher=False, start_automation_worker=False)
        with TestClient(app) as client:
            response = client.delete("/api/v1/account", headers={"Authorization": "Bearer t", "X-Confirm-Delete": "DELETE"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(shot.exists())


class StartupRecovery(ApplyCase):
    def test_starting_the_server_recovers_what_a_stopped_one_left(self):
        from fastapi.testclient import TestClient
        from opportunity_app.api import create_app

        token = self.raw_claim(state="clicking", instance=FOREIGN, mode="handoff", heartbeat_at=self.at(-10).isoformat(timespec="microseconds"),
                               handed_over_at=self.at(-12).isoformat(timespec="microseconds"))
        app = create_app(db_path=self.path, access_token="t", resume_storage=self.root / "r", capture_storage=self.root / "c",
                         interview_storage=self.root / "i", start_call_prep_worker=False, start_inbox_watcher=False, start_automation_worker=False)
        with TestClient(app):
            pass
        self.assertEqual(self.claim_row(token)["state"], "unconfirmed")


def _columns(conn, table):
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


NEW_COLUMNS = (
    ("application_mail_messages", "sender_verified"), ("generated_document_artifacts", "content_sha256"),
)
NEW_TABLES = ("application_submit_claims", "apply_runs", "apply_sensitive_answers", "apply_ats_labels")
NEW_INDEXES = (
    "ux_submit_claims_live_application", "ux_submit_claims_live_job", "idx_submit_claims_user_state", "idx_submit_claims_handed_over",
    "idx_submit_claims_company", "idx_submit_claims_board", "idx_submit_claims_verification", "idx_apply_runs_opportunity",
    "idx_apply_runs_ats", "idx_apply_runs_status",
)


def schema_before_0045(path):
    """A database as it stands on main before this migration, built the way ensure_product_schema builds one."""
    conn = connect_product(path)
    conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)")
    for migration in sorted(MIGRATIONS.glob("[0-9][0-9][0-9][0-9]_*.sql")):
        if migration.name >= "0045":
            break
        sql = migration.read_text(encoding="utf-8")
        step = schema._MIGRATION_STEPS.get(migration.name)
        step(conn, sql) if step else conn.executescript(sql)
        conn.execute("INSERT INTO schema_migrations(name, applied_at) VALUES(?, ?)", (migration.name, utc_now()))
    conn.commit()
    return conn


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)

    def assert_migrated(self, conn):
        for table in NEW_TABLES:
            self.assertEqual(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0, table)
        for table, column in NEW_COLUMNS:
            self.assertIn(column, _columns(conn, table), f"{table}.{column}")
        names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        self.assertTrue(set(NEW_INDEXES) <= names, set(NEW_INDEXES) - names)
        self.assertIn("0045_apply_agent.sql", {row[0] for row in conn.execute("SELECT name FROM schema_migrations")})

    def test_a_fresh_database_gets_the_tables_the_indexes_and_the_columns(self):
        _, path = build_and_migrate(Path(self.tempdir.name))
        conn = connect_product(path)
        self.addCleanup(conn.close)
        self.assert_migrated(conn)
        for name in ("ux_submit_claims_live_application", "ux_submit_claims_live_job"):
            sql = conn.execute("SELECT sql FROM sqlite_master WHERE name=?", (name,)).fetchone()[0]
            self.assertIn("UNIQUE", sql.upper())
            self.assertIn("state <> 'released'", sql, "partial: a released row locks nothing")

    def test_a_database_at_the_latest_earlier_migration_upgrades_and_keeps_its_rows(self):
        conn = schema_before_0045(Path(self.tempdir.name) / "platform.db")
        self.addCleanup(conn.close)
        stamp = utc_now()
        with conn:
            conn.execute("INSERT INTO users(id, email, display_name, role, created_at, updated_at) VALUES('student-1', NULL, 'S', 'student', ?, ?)", (stamp, stamp))
            conn.execute(
                "INSERT INTO connector_accounts(id, user_id, provider, created_at, updated_at) VALUES('c1', 'student-1', 'gmail_drafts', ?, ?)", (stamp, stamp),
            )
            conn.execute(
                "INSERT INTO application_mail_messages(user_id, gmail_id, received_at, recorded_at) VALUES('student-1', 'g1', ?, ?)", (stamp, stamp),
            )
        self.assertNotIn("sender_verified", _columns(conn, "application_mail_messages"))
        ensure_product_schema(conn)
        self.assert_migrated(conn)
        self.assertEqual(conn.execute("SELECT sender_verified FROM application_mail_messages WHERE gmail_id='g1'").fetchone()[0], 0,
                         "a message read before this phase was never vouched for")

    def test_a_crash_after_some_columns_were_added_still_upgrades_and_the_step_is_safe_to_rerun(self):
        conn = schema_before_0045(Path(self.tempdir.name) / "platform.db")
        self.addCleanup(conn.close)
        # What a crash between the ALTERs and the marker leaves behind.
        conn.execute("ALTER TABLE application_mail_messages ADD COLUMN sender_verified INTEGER NOT NULL DEFAULT 0")
        conn.commit()
        ensure_product_schema(conn)
        self.assert_migrated(conn)
        with conn:
            conn.execute("DELETE FROM schema_migrations WHERE name='0045_apply_agent.sql'")
        ensure_product_schema(conn)
        schema._apply_apply_agent(conn, (MIGRATIONS / "0045_apply_agent.sql").read_text(encoding="utf-8"))
        conn.commit()
        self.assert_migrated(conn)

    def test_the_check_constraints_refuse_a_state_the_code_never_writes(self):
        _, path = build_and_migrate(Path(self.tempdir.name))
        conn = connect_product(path)
        self.addCleanup(conn.close)
        with self.assertRaises(sqlite3.IntegrityError):
            with conn:
                conn.execute(
                    "INSERT INTO apply_runs(id, user_id, opportunity_id, kind, started_by, ats, adapter_version, company_key, board_token, page_url, status, "
                    "heartbeat_at, deadline_at, started_at) VALUES('r', 'local-user', 'o', 'submit_all', 'student', 'greenhouse', 'v', 'k', 'b', 'u', 'running', 't', 't', 't')"
                )


if __name__ == "__main__":
    unittest.main()
