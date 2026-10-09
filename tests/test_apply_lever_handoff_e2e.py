"""Finish in browser on a Lever posting end to end: the real runner, a real child process and the real driver against the fictional Lever board.

Nothing reaches the network: the driver's route hook serves ``FakeLever`` (tests/apply_fake_ats.py) and aborts everything else. The student is a ``student_hook`` that
completes what the app left and presses Lever's own Submit in the window, the way the person would. This is the Lever twin of tests/test_apply_handoff_e2e.py, and it
proves the seam between the packages: the apply POST passes only after a committed hand-over, a refused hand-over aborts it, a run that ends on its time closes the
browser before its claim is settled, the résumé the app attached (or the student did) is said to be with Lever in every sentence that follows, and the record and the
email watch read a Lever run as they read a Greenhouse one.
(docs/phase5-lever-handoff-spec.md, milestone LV4: hand-over interception, the outcome table, the record, the watch, and 10.4 items 6, 7, 11 and 15.)
"""

import json
import sys
import time
import unittest
from contextlib import closing
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import realdata_guard

realdata_guard.install()

import apply_agent_fakes as fakes
from apply_fake_ats import LEVER_JOB_ID, LEVER_ROLE_ID, LEVER_SITE, FakeLeverPageClient, seed_lever_role
from browser_support import requires_chromium
from helpers_apply import setUpModule, tearDownModule  # noqa: F401 (module fixtures: unittest and pytest find them here)
from opportunity_app.apply import runner as apply_runner, runs as apply_runs, watch as apply_watch
from opportunity_app.apply.agent_types import HANDOFF_NOT_SUBMITTED
from opportunity_app.apply.runner import ApplyRunner
from opportunity_app.core.database import connect_product
from opportunity_app.core.timestamps import parse_app_instant, utc_now
from test_apply_handoff import HandoffCase

USER = "local-user"
LOCATION = "Springfield, Example State, United States"
RECEIVED = "Your application was not sent. Lever received your résumé."
RESUME = b"%PDF-1.4 a fictional resume"   # what tests/test_apply_api.py seed_student confirms


class LeverRealDriverTests(HandoffCase):
    """``HandoffCase`` gives a throwaway database and a student; the saved role is made Harbor Demo Labs, a Lever posting, with both switches and the résumé setting on."""

    def setUp(self):
        super().setUp()
        self.record = self.root / "lever-e2e-record.json"
        self.runner = ApplyRunner(cancel_grace_s=10.0, timeouts=fakes.HANDOFF_TIMEOUTS, security_code_reader=self.reader)
        self.addCleanup(self.runner.shutdown, 30)
        self.settled_with = []
        self.pages = FakeLeverPageClient()
        with self.conn:
            seed_lever_role(self.conn, USER)
        self.switch("apply_agent", "on")
        self.switch("apply_agent_lever", "on")
        self.switch("apply_lever_resume_upload", "on")
        apply_runs.set_ats_label(self.conn, USER, "location", LOCATION, ats="lever")

    def switch(self, key, value):
        with self.conn:
            self.conn.execute(
                "INSERT INTO user_settings(user_id, key, value, updated_at) VALUES(?, ?, ?, ?) ON CONFLICT(user_id, key) DO UPDATE SET value=excluded.value",
                (USER, key, value, utc_now()),
            )

    def factory(self, student="", scenario="to_thanks", **more):
        return fakes.LeverBrowserAgentFactory(scenario, record_path=str(self.record), student=student, **more)

    def lever(self, factory, **kwargs):
        return self.handoff(factory, opportunity_id=LEVER_ROLE_ID, page_client=self.pages, **kwargs)

    def seen(self):
        return json.loads(self.record.read_text(encoding="utf-8"))

    def watch_settling(self):
        """Record, at the moment the claim is settled, whether any process the run started is still alive."""
        original = ApplyRunner._finish_handoff

        def finish(runner, conn, work, outcome, *, shutting_down):
            self.settled_with.append({
                "alive": [pid for pid in outcome.pids if apply_runner.process_alive(pid)], "pids": dict(outcome.pids),
                "confirmed": outcome.closed_confirmed, "stop": outcome.stop,
                "claim": dict(conn.execute("SELECT state FROM application_submit_claims WHERE token=?", (work.token,)).fetchone())["state"],
            })
            return original(runner, conn, work, outcome, shutting_down=shutting_down)

        patcher = mock.patch.object(ApplyRunner, "_finish_handoff", finish)
        patcher.start()
        self.addCleanup(patcher.stop)

    def evidence(self, row):
        return json.loads(row["evidence_json"])

    # --- the hand-over -------------------------------------------------------------------------------------------------------------------------

    @requires_chromium
    def test_the_students_press_passes_only_after_a_committed_hand_over_and_the_claim_ends_submitted(self):
        self.watch_settling()
        committed = []
        original = apply_runs.hand_over

        def spy(conn, token, *, user_id, deadline=None):
            granted = original(conn, token, user_id=user_id, deadline=deadline)
            if granted:
                # Read through another connection, which sees only what was committed (the writing one sees its own open write).
                with closing(connect_product(self.path)) as other:
                    row = dict(other.execute("SELECT state, after_click FROM application_submit_claims WHERE token=?", (token,)).fetchone())
                committed.append((time.monotonic(), row, conn.in_transaction))
            return granted

        with mock.patch.object(apply_runs, "hand_over", spy):
            run_id = self.lever(self.factory("complete_and_submit"))
            row = self.finished(run_id)
        self.assertEqual((row["outcome"], row["status"]), ("submitted", "finished"), row["reasons_json"])
        self.assertEqual(len(committed), 1)
        self.assertEqual(committed[0][1]["state"], "clicking", "another connection did not see 'clicking' when the hand-over returned: it was not committed")
        self.assertFalse(committed[0][2], "the hand-over returned with its transaction still open")
        seen = self.seen()
        self.assertEqual((len(seen["post_times"]), seen["apply_posts"]), (1, 1), "the form's POST reached Lever exactly once")
        self.assertLess(committed[0][0], seen["post_times"][0], "the POST reached Lever before the hand-over was committed")
        self.assertEqual(set(seen["agent_clicks"]) - {"option_pick"}, set(), "the app itself pressed something but an option of the location list")
        self.assertEqual(seen["pressed"].get("hiddenSubmit", 0) + seen["pressed"].get("cookie", 0) + seen["pressed"].get("challenge", 0), 0)
        # The résumé went to Lever's reader once, before the press, and was the confirmed file.
        self.assertEqual([item["status"] for item in seen["parse_posts"]], [200])
        self.assertEqual(seen["parse_posts"][0]["sha256"], __import__("hashlib").sha256(RESUME).hexdigest())
        claim = self.claim_of(run_id)
        self.assertEqual((claim["state"], claim["resolved_by"], claim["after_click"], claim["ats"]), ("submitted", "page", 1, "lever"))
        self.assertEqual(self.settled_with[0]["alive"], [], "a process of the run was still alive when the claim was settled")
        self.assertTrue(self.settled_with[0]["pids"], "no process was seen below the driver")
        evidence = self.evidence(row)
        self.assertEqual((evidence["handoff_end"], evidence["browser_closed"], evidence["runner"]["closed_confirmed"]), ("posted", True, True))
        self.assertEqual(evidence["confirmation_path"], f"/{LEVER_SITE}/{LEVER_JOB_ID}/thanks")
        self.assertIs(evidence["resume_sent_to_lever"], True)
        self.assert_settled()

    @requires_chromium
    def test_the_record_asks_the_student_and_names_lever_and_the_tracker_waits_for_their_word(self):
        run_id = self.lever(self.factory("complete_and_submit"))
        row = self.finished(run_id)
        self.assertEqual(row["outcome"], "submitted")
        claim = self.claim_of(run_id)
        self.assertEqual((claim["stage_recorded"], claim["verification"]), (0, "not_watched"), "no Gmail, so the app says it isn't watching for the email")
        application = self.conn.execute("SELECT stage FROM applications WHERE id=?", (claim["application_id"],)).fetchone()
        self.assertEqual(application["stage"], "applying", "Finish in browser never moves the tracker by itself: the card asks")
        events = self.events(claim["application_id"])
        self.assertEqual([name for name, _ in events], ["apply_agent_started", "apply_agent_submitted"])
        submitted = events[1][1]
        self.assertEqual((submitted["mode"], submitted["run_id"], submitted["by"], submitted["confirmation_path"]),
                         ("handoff", run_id, "student_in_window", f"/{LEVER_SITE}/{LEVER_JOB_ID}/thanks"))
        card = apply_watch.claim_card(self.conn, USER, claim["token"])
        self.assertEqual((card["status"], card["ask_mark_applied"], card["ats"], card["ats_name"]), ("not_watched", True, "lever", "Lever"))
        view = self.view(run_id)
        self.assertEqual((view["summary"], view["resume_sent_to_lever"]), ("Lever showed its confirmation page. Mark as applied?", True))
        self.assert_settled()

    @requires_chromium
    def test_the_watch_settles_a_submitted_lever_run_from_lever_s_own_email(self):
        with mock.patch.object(apply_watch, "watch_available", return_value=""):
            run_id = self.lever(self.factory("complete_and_submit"))
            row = self.finished(run_id)
        self.assertEqual(row["outcome"], "submitted")
        claim = self.claim_of(run_id)
        self.assertEqual((claim["state"], claim["verification"]), ("submitted", "awaiting_email"))
        self.assertTrue(claim["watch_until"])
        self.give_the_reader_an_email_from("hire.lever.co", claim["application_id"])
        counts = apply_watch.watch(self.conn, USER, parse_app_instant(utc_now()) + timedelta(minutes=5))
        self.assertEqual(counts["email_confirmed"], 1, counts)
        self.assertEqual(self.claim_of(run_id)["verification"], "email_confirmed")
        card = apply_watch.claim_card(self.conn, USER, claim["token"])
        self.assertEqual((card["status"], card["ats_name"]), ("email_confirmed", "Lever"))
        self.assertEqual(self.notices(), ["Lever showed its confirmation page for your application to Customer Success Lead at Harbor Demo Labs"],
                         "the record names the ATS the student applied on")

    def give_the_reader_an_email_from(self, domain, application_id):
        """A healthy Gmail reader and one confirmation email from ``domain`` that names the posting (the rows the mail reader would have written)."""
        stamp = utc_now()
        with self.conn:
            self.conn.execute("DELETE FROM connector_accounts WHERE user_id=?", (USER,))
            self.conn.execute(
                "INSERT INTO connector_accounts(id, user_id, provider, scopes_json, status, created_at, updated_at, account_email) "
                "VALUES('connector-gmail', ?, 'gmail_drafts', ?, 'connected', ?, ?, 'sam.rivera@example.test')",
                (USER, json.dumps(["https://www.googleapis.com/auth/gmail.readonly"]), stamp, stamp),
            )
            self.conn.execute("DELETE FROM application_mail_sync WHERE user_id=?", (USER,))
            self.conn.execute(
                "INSERT INTO application_mail_sync(user_id, history_id, pending_ids_json, recovery_state, last_ok_at, last_error, updated_at) "
                "VALUES(?, 'h1', '[]', '', ?, '', ?)", (USER, stamp, stamp),
            )
            self.conn.execute(
                "INSERT INTO application_mail_messages(user_id, gmail_id, application_id, kind, matched_by, state, subject, sender_domain, received_at, recorded_at, "
                "sender_verified) VALUES(?, 'gm-1', ?, 'application_confirmation', 'job_id', 'done', 'Thanks for applying', ?, ?, ?, 1)",
                (USER, application_id, domain, stamp, stamp),
            )
        self.switch("application_mail", "shadow")

    @requires_chromium
    def test_a_refused_hand_over_aborts_the_post_and_nothing_is_sent(self):
        self.watch_settling()
        with mock.patch.object(apply_runs, "hand_over", lambda *args, **kwargs: False):
            run_id = self.lever(self.factory("complete_and_submit"))
            row = self.finished(run_id)
        seen = self.seen()
        self.assertEqual((seen["post_times"], seen["apply_posts"]), ([], 0), "a POST reached Lever without a committed hand-over")
        self.assertNotEqual(row["outcome"], "submitted")
        claim = self.claim_of(run_id)
        self.assertNotEqual(claim["state"], "submitted")
        self.assertFalse(claim["handed_over_at"])
        self.assertEqual(claim["after_click"], 0)
        self.assertIn(RECEIVED, claim["note"], "the file went to Lever when the app attached it, and the sentence says so")
        self.assertEqual(self.settled_with[0]["alive"], [], self.settled_with)
        self.assert_settled()

    @requires_chromium
    def test_a_stop_before_the_press_means_the_post_never_leaves(self):
        run_id = self.lever(self.factory("do_nothing"))
        self.turn(run_id)
        # What the Stop route does: the database flag first, then the message to the agent.
        self.assertTrue(apply_runs.request_cancel(self.conn, self.claim_of(run_id)["token"], user_id=USER))
        self.assertTrue(self.runner.cancel(run_id))
        row = self.finished(run_id)
        self.assertEqual(self.seen()["apply_posts"], 0)
        self.assertEqual(row["outcome"], "needs_you")
        claim = self.claim_of(run_id)
        self.assertEqual((claim["state"], claim["after_click"], claim["note"]), ("needs_you", 0, f"{HANDOFF_NOT_SUBMITTED} Lever received your résumé."))
        self.assertEqual(json.loads(claim["detail_json"]).get("stopped_by"), "student")
        self.assert_settled()

    @requires_chromium
    def test_a_closed_window_settles_needs_you_with_nothing_sent(self):
        self.watch_settling()
        run_id = self.lever(self.factory("close_window"))
        row = self.finished(run_id)
        self.assertEqual(row["outcome"], "needs_you")
        self.assertEqual(self.evidence(row)["handoff_end"], "closed")
        claim = self.claim_of(run_id)
        self.assertEqual((claim["state"], claim["after_click"]), ("needs_you", 0))
        self.assertIn("Lever received your résumé", claim["note"])
        self.assertEqual(self.seen()["apply_posts"], 0)
        self.assertEqual(self.settled_with[0]["alive"], [])
        self.assert_settled()

    @requires_chromium
    def test_a_run_that_runs_out_of_time_closes_the_browser_before_the_claim_is_settled(self):
        self.watch_settling()
        self.runner = ApplyRunner(cancel_grace_s=10.0, timeouts=replace(fakes.HANDOFF_TIMEOUTS, handoff_s=6), security_code_reader=self.reader)
        self.addCleanup(self.runner.shutdown, 30)
        run_id = self.lever(self.factory("do_nothing"), runner=self.runner)
        row = self.finished(run_id, self.runner)
        self.assertNotEqual(row["outcome"], "submitted")
        self.assertEqual(self.seen()["apply_posts"], 0)
        evidence = self.evidence(row)
        self.assertEqual(evidence["handoff_end"], "timeout")
        self.assertTrue(evidence["browser_closed"])
        self.assertEqual(self.settled_with[0]["alive"], [], "the browser was still running when the claim was settled")
        self.assertEqual(self.settled_with[0]["claim"], "claimed", "the claim was settled before the browser was gone")
        claim = self.claim_of(run_id)
        self.assertNotEqual(claim["state"], "submitted")
        self.assertEqual(claim["after_click"], 0)
        self.assert_settled()

    # --- the outcome table ---------------------------------------------------------------------------------------------------------------------

    @requires_chromium
    def test_a_refusal_with_the_form_still_there_fails_the_claim_after_the_click_and_names_the_field(self):
        run_id = self.lever(self.factory("complete_and_submit", "refused_4xx"))
        row = self.finished(run_id)
        self.assertEqual((row["outcome"], row["status"]), ("failed", "finished"))
        claim = self.claim_of(run_id)
        self.assertEqual((claim["state"], claim["after_click"]), ("failed", 1))
        self.assertIn("Lever refused the form (HTTP 422)", claim["note"])
        self.assertEqual(self.evidence(row)["submit_status"], 422)
        self.assert_settled()

    @requires_chromium
    def test_a_server_error_after_the_press_is_unconfirmed_and_the_claim_is_kept_for_the_email(self):
        run_id = self.lever(self.factory("complete_and_submit", "server_5xx"))
        row = self.finished(run_id)
        self.assertEqual(row["outcome"], "unconfirmed")
        claim = self.claim_of(run_id)
        self.assertEqual((claim["state"], claim["after_click"]), ("unconfirmed", 1))
        self.assertIn("Your application may have been sent", claim["note"])
        self.assert_settled()

    # --- the résumé ----------------------------------------------------------------------------------------------------------------------------

    @requires_chromium
    def test_with_the_setting_off_the_app_sends_nothing_and_the_sentences_do_not_say_otherwise(self):
        self.switch("apply_lever_resume_upload", "off")
        run_id = self.lever(self.factory("do_nothing"))
        self.turn(run_id)
        self.assertTrue(apply_runs.request_cancel(self.conn, self.claim_of(run_id)["token"], user_id=USER))
        self.assertTrue(self.runner.cancel(run_id))
        row = self.finished(run_id)
        seen = self.seen()
        self.assertEqual((seen["parse_posts"], seen["non_get"] and [item["path"] for item in seen["non_get"] if item["host"] == "jobs.lever.co"]), ([], []),
                         "a request left the page for Lever's own host although the app was not to attach the résumé")
        self.assertIs(self.evidence(row)["resume_sent_to_lever"], False)
        claim = self.claim_of(run_id)
        self.assertEqual((claim["state"], claim["after_click"], claim["note"]), ("needs_you", 0, HANDOFF_NOT_SUBMITTED))
        self.assertNotIn("résumé", claim["note"])
        plan = {item["key"]: item for item in json.loads(row["plan_json"])}
        self.assertNotEqual(plan["resume"]["disposition"], "fill", "the app was not to attach the file")
        self.assertNotIn("resume_sent", {item["key"] for item in self.evidence(row)["left_for_you"]})
        self.assert_settled()

    @requires_chromium
    def test_a_file_the_student_attaches_is_read_by_the_page_named_in_the_record_and_part_of_the_submission(self):
        self.switch("apply_lever_resume_upload", "off")
        run_id = self.lever(self.factory("attach_complete_and_submit"))
        row = self.finished(run_id)
        self.assertEqual((row["outcome"], row["status"]), ("submitted", "finished"), row["reasons_json"])
        seen = self.seen()
        self.assertEqual([item["status"] for item in seen["parse_posts"]], [200], "the page's read of the student's own file was let through, once")
        evidence = self.evidence(row)
        self.assertIs(evidence["resume_sent_to_lever"], False, "the app attached nothing")
        record = evidence["student_attached_resume"]
        self.assertEqual((record["count"], record["sha256"]), (1, fakes.LEVER_STUDENT_SHA))
        self.assertIn("org", record["changed"])
        self.assertIs(self.view(run_id)["resume_sent_to_lever"], True, "the page says Lever holds the file, whoever attached it")
        steps = [item["step"] for item in json.loads(row["progress_json"])]
        self.assertIn("resume_attached", steps)
        self.assertIn("resume_changed", steps)
        self.assert_settled()

    @requires_chromium
    def test_a_file_the_student_attaches_and_a_window_that_then_closes_says_lever_received_it(self):
        self.switch("apply_lever_resume_upload", "off")
        run_id = self.lever(self.factory("attach_and_wait"))
        self.turn(run_id)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and "student_attached_resume" not in (self.run_row(run_id)["evidence_json"] or ""):
            time.sleep(0.1)
        self.assertIn("student_attached_resume", self.run_row(run_id)["evidence_json"], "the parent did not learn of the file while the turn went on")
        self.assertTrue(apply_runs.request_cancel(self.conn, self.claim_of(run_id)["token"], user_id=USER))
        self.assertTrue(self.runner.cancel(run_id))
        self.finished(run_id)
        claim = self.claim_of(run_id)
        self.assertEqual((claim["state"], claim["after_click"], claim["note"]), ("needs_you", 0, f"{HANDOFF_NOT_SUBMITTED} Lever received your résumé."))
        self.assert_settled()

    @requires_chromium
    def test_a_read_that_never_finishes_settles_needs_you_with_the_browser_closed_and_nothing_filled(self):
        self.watch_settling()
        self.runner = ApplyRunner(cancel_grace_s=10.0, timeouts=replace(fakes.HANDOFF_TIMEOUTS, parse_s=2), security_code_reader=self.reader)
        self.addCleanup(self.runner.shutdown, 30)
        run_id = self.lever(self.factory("do_nothing", parse_mode="timeout"), runner=self.runner)
        row = self.finished(run_id, self.runner)
        self.assertEqual(row["outcome"], "needs_you")
        evidence = self.evidence(row)
        self.assertEqual((evidence["handoff_end"], evidence["resume_parse"], evidence["browser_closed"]), ("parse", "timeout", True))
        claim = self.claim_of(run_id)
        self.assertEqual((claim["state"], claim["after_click"]), ("needs_you", 0))
        self.assertIn("Lever did not finish reading your résumé. Nothing was filled. Lever may still have the file.", claim["note"])
        self.assertEqual(self.settled_with[0]["alive"], [])
        filled = [item for item in json.loads(row["plan_json"]) if item["disposition"] == "fill" and item["key"] != "resume"]
        self.assertEqual(filled, [], "no field is described as filled")
        self.assert_settled()


if __name__ == "__main__":
    unittest.main()
