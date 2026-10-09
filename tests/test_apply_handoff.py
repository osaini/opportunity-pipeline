"""Finish in browser, the parent's half: the claim, the runner, the hand-over, the settlement, the views and the routes.

No browser and no network. The agents here are fakes (tests/apply_fake_ats.py): a canned handoff in a thread, and the same
script in a real child process with a sleeping grandchild standing in for Chromium, so the tests that kill a tree see real
pids. What is proven is the machinery around the agent (spec 6.0 to 6.15, docs/phase5-apply-agent-spec.md): that nothing
is written when a start is refused, that the claim is taken before the run, that the hand-over is committed before the
agent is told it may continue, that every way a run can end settles the claim (and never calls a handed-over attempt "not
sent"), that the window is confirmed gone before "nothing was sent" is said, and that the routes need the student's browser.
"""

import hashlib
import inspect
import json
import logging
import os
import sqlite3
import subprocess
import sys
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import realdata_guard

realdata_guard.install()

import multiprocessing

from fastapi.testclient import TestClient

from opportunity_app.api import create_app
from opportunity_app.applications import urgent
from opportunity_app.apply import (
    claims as apply_claims, policy as apply_policy, preflight as apply_preflight, runner as apply_runner, runs as apply_runs,
    security_code as apply_security_code, watch as apply_watch,
)
from opportunity_app.apply.agent_types import (
    HANDOFF_CRASHED, HANDOFF_NOT_SUBMITTED, HANDOFF_UNRECORDED, OP_FRONT, OP_HAND_OVER_REPLY, OP_SECURITY_CODE_REPLY, PROGRESS_STEPS, WINDOW_CLOSED,
    WINDOW_UNCONFIRMED as WINDOW_UNCONFIRMED_TEMPLATE, YOUR_TURN, YOUR_TURN_NONE_LEFT, ApplyTimeouts, RunResult, progress_text,
)
from opportunity_app.apply.agent import ApplyAgent
from opportunity_app.apply.checks import UNCONFIRMED_NOTE as UNCONFIRMED_NOTE_TEMPLATE
from opportunity_app.apply.runner import ApplyRunner, RunnerBusy, RunRefused, SupervisorHandlers, handoff_settlement
from opportunity_app.apply.runner_child import ChildChannel
from opportunity_app.automation import ledger as automation
from opportunity_app.core.timestamps import parse_app_instant, utc_now
from opportunity_app.student import preparation

import test_apply_api as api_tests
import test_apply_runner as runner_tests
from apply_fake_ats import (
    CrashingAgentFactory, FakeApplyAgentFactory, FakeSchemaClient, JOB_URL, ProcessCannedFactory, kill_if_same_process, still_running,
)
from helpers_apply import BLUEFIN, ApplyCase, setUpModule, tearDownModule  # noqa: F401 (module fixtures: unittest and pytest find them here)
from pipeline_core.identity import employer_key

# The two notes name the ATS; every run in this module is Greenhouse's.
UNCONFIRMED_NOTE = UNCONFIRMED_NOTE_TEMPLATE.format(ats="Greenhouse")
WINDOW_UNCONFIRMED = WINDOW_UNCONFIRMED_TEMPLATE.format(ats="Greenhouse")
USER = "local-user"
AUTH = api_tests.AUTH
ACME = "job-a"
GRACE = 5.0
CODE = "X7KQ2M9P"
HANDOFF_URL = "https://job-boards.greenhouse.io/examplerobotics/jobs/4000000001"
OTHER_URL = "https://job-boards.greenhouse.io/examplerobotics/jobs/4000000002"


def wait_until(check, seconds=15.0):
    return runner_tests.wait_until(check, seconds)


def handoff_factory(outcome="submitted", wait=0.3, step_delay=0.0, **extra):
    return FakeApplyAgentFactory(step_delay=step_delay, handoff={"wait": wait, "outcome": outcome, **extra})


class FakeReader:
    """Stands in for the security-code reader: records who it was asked about and on which thread, answers from a script."""

    def __init__(self, answers=None, delay=0.0):
        self.answers = list(answers or [apply_security_code.CodeAnswer("found", code=CODE)])
        self.delay = delay
        self.asked, self.confirmed, self.forgotten, self.threads = [], [], [], []

    def answer(self, conn, *, user_id, token, now=None):
        self.asked.append((user_id, token))
        self.threads.append(threading.current_thread().name)
        if self.delay:
            time.sleep(self.delay)
        return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]

    def confirm(self, conn, *, user_id, token, typed, reason="", now=None):
        self.confirmed.append((user_id, token, typed, reason))

    def forget(self, token):
        self.forgotten.append(token)


class HandoffCase(runner_tests.RunnerCase):
    """A throwaway database with a confirmed student and the saved role made a Greenhouse role whose posting matches it."""

    def setUp(self):
        super().setUp()
        with self.conn:
            # The fictional board answers every job with Example Robotics' listing, so the saved role must read as that role.
            self.conn.execute("UPDATE opportunities SET company='Example Robotics, Inc.', title='Robotics Intern', url=? WHERE id=?", (HANDOFF_URL, ACME))
        self.reader = FakeReader()
        self.runner = ApplyRunner(cancel_grace_s=GRACE, security_code_reader=self.reader)
        self.addCleanup(self.runner.shutdown, 30)
        self.records = []
        handler = logging.Handler()
        handler.emit = self.records.append
        logging.getLogger().addHandler(handler)
        self.addCleanup(logging.getLogger().removeHandler, handler)
        old = logging.getLogger().level
        logging.getLogger().setLevel(logging.DEBUG)
        self.addCleanup(logging.getLogger().setLevel, old)

    def handoff(self, factory=None, *, runner=None, opportunity_id=ACME, **kwargs):
        return self.start(factory or handoff_factory(), kind="handoff", runner=runner, opportunity_id=opportunity_id, **kwargs)

    def finished(self, run_id, runner=None):
        """Wait for the run to be finished (its row written, the slot free) and return its row."""
        return self.finish(run_id, runner)

    def notices(self):
        return [row["title"] for row in automation.list_notices(self.conn, USER, limit=50)]

    def run_row(self, run_id):
        return dict(self.conn.execute("SELECT * FROM apply_runs WHERE id=?", (run_id,)).fetchone())

    def claim_of(self, run_id):
        row = self.conn.execute("SELECT * FROM application_submit_claims WHERE run_id=?", (run_id,)).fetchone()
        return None if row is None else dict(row)

    def detail(self, run_id):
        return json.loads(self.claim_of(run_id)["detail_json"])

    def view(self, run_id):
        return apply_runner.run_view(self.conn, self.run_row(run_id), local=self.runner.busy())

    def turn(self, run_id):
        """Wait for the student's turn to begin: the agent said ready and the claim says the student is working."""
        self.assertTrue(wait_until(lambda: self.detail(run_id).get("waiting") == "student"), "the turn never began")

    def events(self, application_id="app-job-a"):
        return [(row["event_type"], json.loads(row["detail_json"] or "{}")) for row in self.conn.execute(
            "SELECT event_type, detail_json FROM application_events WHERE application_id=? ORDER BY id", (application_id,)).fetchall()]

    def tables(self):
        return self.counts("applications", "application_events", "application_submit_claims", "apply_runs", "automation_notices")

    def assert_settled(self):
        """I4: nothing is left claimed once the run is over, and nothing is held in this process."""
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM application_submit_claims WHERE state='claimed'").fetchone()[0], 0)
        self.assertEqual(apply_claims.RUNNING, set())
        self.assertIsNone(self.runner.busy())

    def everywhere(self):
        """Every piece of text the database holds, and every log line."""
        texts = []
        for (name,) in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
            for row in self.conn.execute(f'SELECT * FROM "{name}"').fetchall():
                texts += [str(value) for value in tuple(row)]
        texts += [record.getMessage() for record in self.records]
        return "\n".join(texts)


class HappyPathTests(HandoffCase):
    def test_a_handoff_goes_from_the_claim_through_the_students_turn_to_a_submitted_attempt(self):
        run_id = self.handoff(handoff_factory(wait=3.0))
        claim = self.claim_of(run_id)
        self.assertEqual((claim["state"], claim["mode"], claim["stage_policy"], claim["run_id"], claim["after_click"]), ("claimed", "handoff", "ask", run_id, 0))
        self.assertEqual(claim["board_token"], "examplerobotics")
        run = self.run_row(run_id)
        self.assertEqual((run["status"], run["kind"], run["claim_token"], run["application_id"]), ("running", "handoff", claim["token"], claim["application_id"]))
        self.assertEqual([name for name, _ in self.events()], ["apply_agent_started"])
        self.assertEqual(self.conn.execute("SELECT stage FROM applications WHERE id='app-job-a'").fetchone()[0], "applying")
        self.turn(run_id)
        claim, run = self.claim_of(run_id), self.run_row(run_id)
        self.assertTrue(claim["plan_hash"] and run["plan_hash"] == claim["plan_hash"], "the claim carries the plan that was filled")
        self.assertTrue(json.loads(run["plan_json"]), "ready stored the plan")
        view = self.view(run_id)
        self.assertEqual((view["phase"], view["summary"], view["can_cancel"], view["can_front"], view["status"]), ("your_turn", YOUR_TURN, True, True, "running"))
        self.assertTrue(view["left_for_you"], "the form's required questions have no saved answers: they are left for the student")
        self.assertTrue(all(set(item) == {"key", "question", "reason"} for item in view["left_for_you"]))
        self.assertTrue(view["handoff_until"], "the window's closing time")
        self.assertEqual(view["claim"]["status"], "your_turn")
        self.assertEqual(view["claim"]["run_id"], run_id)
        first = next(item for item in view["fields"] if item["key"] == "first_name")
        self.assertEqual((first["disposition_text"], first["source_kind"], first["control"], first["links"], first["note"]),
                         ("Filled in the window", "profile", "text", [], ""))
        self.assertEqual(self.finished(run_id)["outcome"], "submitted")
        claim = self.claim_of(run_id)
        self.assertEqual((claim["state"], claim["resolved_by"], claim["stage_recorded"], claim["after_click"], claim["verification"]),
                         ("submitted", "page", 0, 1, "not_watched"), "no Gmail, so the app says it isn't watching for the email")
        self.assertTrue(claim["handed_over_at"])
        self.assertEqual(json.loads(claim["detail_json"])["waiting"], "", "a settled claim never says the student is still working")
        self.assertEqual(self.conn.execute("SELECT stage FROM applications WHERE id='app-job-a'").fetchone()[0], "applying",
                         "Finish in browser never moves the tracker by itself: the card asks")
        events = self.events()
        self.assertEqual([name for name, _ in events], ["apply_agent_started", "apply_agent_submitted"])
        submitted = events[1][1]
        self.assertEqual((submitted["mode"], submitted["run_id"], submitted["confirmation_path"], submitted["by"]),
                         ("handoff", run_id, "/examplerobotics/jobs/4000000001/confirmation", "student_in_window"))
        card = apply_watch.claim_card(self.conn, USER, claim["token"])
        self.assertEqual((card["status"], card["ask_mark_applied"], card["run_id"]), ("not_watched", True, run_id))
        done = self.view(run_id)
        self.assertEqual((done["status"], done["outcome"], done["phase"], done["can_cancel"], done["can_front"], done["handoff_until"]),
                         ("finished", "submitted", "", False, False, None))
        self.assertEqual(done["summary"], "Greenhouse showed its confirmation page. Mark as applied?")
        self.assertEqual(json.loads(self.run_row(run_id)["evidence_json"])["handoff_end"], "posted")
        self.assert_settled()

    def test_with_the_email_watch_available_the_submission_awaits_its_confirmation_email(self):
        with mock.patch.object(apply_watch, "watch_available", return_value=""):
            run_id = self.handoff()
            self.finished(run_id)
        claim = self.claim_of(run_id)
        self.assertEqual((claim["state"], claim["verification"]), ("submitted", "awaiting_email"))
        self.assertTrue(claim["watch_until"])

    def test_the_run_keeps_the_child_evidence_and_the_requests_value_free(self):
        run_id = self.handoff()
        row = self.finished(run_id)
        evidence = json.loads(row["evidence_json"])
        self.assertEqual((evidence["submit_continued"], evidence["browser_closed"], evidence["handoff_end"]), (True, True, "posted"))
        self.assertEqual(evidence["runner"]["closed_confirmed"], True)
        self.assertEqual(evidence["security_code_reader"], {})
        for secret in ("Sam", "Rivera", "sam.rivera@example.test"):
            self.assertNotIn(secret, json.dumps({key: row[key] for key in row}))

    def test_the_student_never_hands_over_in_a_rehearsal_and_a_handoff_leaves_other_roles_alone(self):
        before = self.counts("applications")["applications"]
        self.finished(self.handoff())
        self.assertEqual(self.counts("applications")["applications"] - before, 1, "exactly the role's own application")


class RefusalTests(HandoffCase):
    def refused(self, **kwargs):
        before = self.tables()
        with self.assertRaises(RunRefused) as caught:
            self.handoff(**kwargs)
        self.assertEqual(self.tables(), before, "a refused start writes no claim, no run, no application, no event and no notice")
        self.assertIsNone(self.runner.busy())
        self.assertEqual(apply_claims.RUNNING, set())
        return caught.exception

    def test_a_second_start_while_one_runs_is_refused_with_nothing_written(self):
        first = self.handoff(handoff_factory(wait=30))
        self.turn(first)
        before = self.tables()
        with self.assertRaises(RunnerBusy):
            self.handoff()
        self.assertEqual(self.tables(), before)
        # The Stop route's order: the database flag first, then the message to the agent (a settlement that reads the claim before the
        # flag lands decides a failure with a notice instead of the student's Stop).
        self.assertTrue(apply_runs.request_cancel(self.conn, self.claim_of(first)["token"], user_id=USER))
        self.assertTrue(self.runner.cancel(first))
        self.finished(first)

    def test_a_posting_that_cannot_run_is_refused_with_the_checks_sentence(self):
        problem = self.refused(schema_client=FakeSchemaClient(closed=True))
        self.assertEqual((problem.status_code, problem.message, problem.code), (409, "The app couldn't find this posting on Greenhouse. It may be closed", ""))
        problem = self.refused(opportunity_id="job-b")
        self.assertEqual(problem.message, "Apply for me works with Greenhouse and Lever postings only, for now")

    def test_a_posting_that_differs_waits_for_the_students_word(self):
        with self.conn:
            self.conn.execute("UPDATE opportunities SET company='Acme Robotics', title='Mechanical Engineering Intern' WHERE id=?", (ACME,))
        problem = self.refused(posting_confirmed=False)   # RunnerCase.start confirms the posting unless told not to
        self.assertEqual((problem.status_code, problem.code, problem.ask), (409, "posting", False))
        self.assertTrue(problem.message.startswith("Check the posting first. Greenhouse's form is for"), problem.message)
        run_id = self.handoff(posting_confirmed=True)
        self.assertEqual(self.finished(run_id)["outcome"], "submitted")

    def tombstone(self):
        """A first attempt the student said did not go through (released), handed over three days ago."""
        run_id = self.handoff()
        self.finished(run_id)
        token = self.claim_of(run_id)["token"]
        with self.conn:
            self.conn.execute("UPDATE application_submit_claims SET state='unconfirmed', resolved_by='' WHERE token=?", (token,))
        apply_watch.resolve_by_student(self.conn, token, user_id=USER, went_through=False)
        old = (utc_now() and (apply_runs.at_utc(None) - timedelta(days=3)).isoformat(timespec="microseconds"))
        with self.conn:
            self.conn.execute("UPDATE application_submit_claims SET handed_over_at=? WHERE token=?", (old, token))
        self.assertEqual(self.claim_of(run_id)["state"], "released")

    def test_a_released_attempt_and_the_company_limit_each_need_a_tick(self):
        self.tombstone()
        problem = self.refused()
        self.assertEqual((problem.status_code, problem.code, problem.ask), (409, "released_job", True))
        problem = self.refused(acknowledged=("released_job",))
        self.assertEqual((problem.code, problem.ask), ("company_limit", True))
        run_id = self.handoff(acknowledged=("released_job", "company_limit"))
        self.assertEqual(json.loads(self.claim_of(run_id)["detail_json"])["acknowledged"], ["released_job", "company_limit"])
        self.finished(run_id)

    def second_role(self):
        """Another saved copy of the company's role (another job on the same board), with no application of its own yet."""
        stamp = utc_now()
        with self.conn:
            self.conn.execute(
                "INSERT INTO opportunities(id, company, title, url, first_seen_at, last_seen_at, created_at, updated_at) VALUES('job-c', 'Example Robotics, Inc.', "
                "'Robotics Intern', ?, ?, ?, ?, ?)", (OTHER_URL, stamp, stamp, stamp, stamp),
            )

    def test_the_spacing_between_two_applications_cannot_be_ticked_past(self):
        self.finished(self.handoff())
        self.second_role()
        problem = self.refused(opportunity_id="job-c", acknowledged=("company_limit", "released_job", "applying_old"))
        self.assertEqual((problem.status_code, problem.code, problem.ask), (409, "spacing", False))
        self.assertIn("The next agent submission is allowed at", problem.message)

    def test_the_company_limit_is_a_tick_after_the_spacing_has_passed(self):
        first = self.handoff()
        self.finished(first)
        old = (apply_runs.at_utc(None) - timedelta(days=5)).isoformat(timespec="microseconds")
        with self.conn:
            self.conn.execute("UPDATE application_submit_claims SET handed_over_at=? WHERE run_id=?", (old, first))
        self.second_role()
        problem = self.refused(opportunity_id="job-c")
        self.assertEqual((problem.code, problem.ask), ("company_limit", True))
        second = self.handoff(opportunity_id="job-c", acknowledged=("company_limit",))
        self.assertIn("company_limit", json.loads(self.claim_of(second)["detail_json"])["acknowledged"])
        self.finished(second)

    def test_an_application_that_is_already_applied_or_live_is_refused_without_a_tick(self):
        run_id = self.handoff()
        self.finished(run_id)
        problem = self.refused()
        self.assertEqual((problem.status_code, problem.ask), (409, False))


class StartFailureTests(HandoffCase):
    def test_a_start_that_fails_after_the_run_exists_settles_the_claim_and_the_run_together(self):
        before = self.counts("apply_runs")["apply_runs"]
        with mock.patch("opportunity_app.apply.runner.threading.Thread", side_effect=RuntimeError("no thread")):
            with self.assertRaises(RuntimeError):
                self.handoff()
        self.assertEqual(self.counts("apply_runs")["apply_runs"] - before, 1)
        run = dict(self.conn.execute("SELECT * FROM apply_runs").fetchone())
        claim = dict(self.conn.execute("SELECT * FROM application_submit_claims").fetchone())
        self.assertEqual((run["status"], run["outcome"], json.loads(run["reasons_json"])), ("finished", "failed", [apply_runner.START_FAILED]))
        self.assertEqual((claim["state"], claim["after_click"], claim["note"]), ("failed", 0, apply_runner.START_FAILED))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM apply_runs WHERE status='running'").fetchone()[0], 0)
        self.assert_settled()
        self.assertEqual(self.finished(self.handoff())["outcome"], "submitted", "the stopped attempt is released by the next one")

    def test_a_start_that_fails_before_the_run_row_settles_the_claim_alone(self):
        with mock.patch.object(apply_runs, "create_run", side_effect=RuntimeError("no row")):
            with self.assertRaises(RuntimeError):
                self.handoff()
        claim = dict(self.conn.execute("SELECT * FROM application_submit_claims").fetchone())
        self.assertEqual((claim["state"], claim["after_click"], claim["note"]), ("failed", 0, apply_runner.START_FAILED))
        self.assertEqual(self.counts("apply_runs")["apply_runs"], 0)
        self.assert_settled()


class StopAndEndTests(HandoffCase):
    """Every way a run can end settles its claim: the table of spec 6.15 as the parent decides it from the claim it reads."""

    def stop(self, run_id):
        """What the Stop route does: the database flag first, then the message to the agent."""
        token = self.claim_of(run_id)["token"]
        self.assertTrue(apply_runs.request_cancel(self.conn, token, user_id=USER))
        self.assertTrue(self.runner.cancel(run_id))

    def urgent_kinds(self):
        return [item["kind"] for item in urgent.urgent_queue(self.conn, user_id=USER)["items"] if item["kind"].startswith("apply_")]

    def test_stop_during_the_turn_sends_nothing_and_asks_the_student_nothing(self):
        run_id = self.handoff(handoff_factory(wait=30))
        self.turn(run_id)
        before = self.counts("automation_notices")
        self.stop(run_id)
        self.finished(run_id)
        claim = self.claim_of(run_id)
        self.assertEqual((claim["state"], claim["after_click"], claim["note"]), ("needs_you", 0, HANDOFF_NOT_SUBMITTED))
        self.assertEqual(json.loads(claim["detail_json"]), {**json.loads(claim["detail_json"]), "waiting": "", "stopped_by": "student"})
        self.assertEqual(self.counts("automation_notices"), before, "the student's own Stop writes no notice")
        self.assertEqual(self.urgent_kinds(), [], "nothing to ask: they know")
        self.assertEqual(apply_runs.get_run(self.conn, run_id, user_id=USER)["outcome"], "needs_you")
        self.assertEqual(self.view(run_id)["summary"], HANDOFF_NOT_SUBMITTED)
        self.assertNotIn("apply_agent_unconfirmed", [name for name, _ in self.events()])
        self.assert_settled()

    def test_stop_during_the_fill_ends_the_same_way(self):
        run_id = self.handoff(handoff_factory(step_delay=0.4))
        self.stop(run_id)
        self.finished(run_id)
        claim = self.claim_of(run_id)
        self.assertEqual((claim["state"], claim["after_click"], claim["note"]), ("needs_you", 0, HANDOFF_NOT_SUBMITTED))
        self.assertEqual(json.loads(claim["detail_json"])["stopped_by"], "student")
        self.assertEqual(self.urgent_kinds(), [])
        self.assert_settled()

    def test_a_submit_pressed_after_stop_is_refused_and_is_still_a_stop_not_a_failure_to_record(self):
        run_id = self.handoff(handoff_factory(wait=0.8))
        self.turn(run_id)
        # Stop reached the database but not the window yet: the student's press asks for the hand-over, which is refused.
        self.assertTrue(apply_runs.request_cancel(self.conn, self.claim_of(run_id)["token"], user_id=USER))
        row = self.finished(run_id)
        self.assertEqual(json.loads(row["evidence_json"])["handoff_end"], "refused", "the child saw the refusal")
        claim = self.claim_of(run_id)
        self.assertEqual((claim["state"], claim["after_click"], claim["note"], claim["handed_over_at"]), ("needs_you", 0, HANDOFF_NOT_SUBMITTED, None))
        self.assertEqual(json.loads(row["reasons_json"]), [HANDOFF_NOT_SUBMITTED], "not 'couldn't record this submission'")
        self.assertEqual(self.notices(), [])
        self.assert_settled()

    def test_a_hand_over_the_child_did_not_hear_leaves_the_attempt_unconfirmed_never_not_sent(self):
        # The parent committed, then the agent reports it was refused (a commit that came after the child gave up).
        run_id = self.handoff(handoff_factory("refused", wait=0.2))
        row = self.finished(run_id)
        claim = self.claim_of(run_id)
        self.assertEqual((claim["state"], claim["after_click"], claim["note"]), ("unconfirmed", 1, UNCONFIRMED_NOTE))
        self.assertEqual(json.loads(row["reasons_json"]), [UNCONFIRMED_NOTE], "never the child's sentence that says nothing was sent")
        self.assertIn("apply_agent_unconfirmed", [name for name, _ in self.events()])
        self.assertEqual(len(self.notices()), 1)
        self.assertIn(f"{self.view(run_id)['summary']}", UNCONFIRMED_NOTE)
        self.assertIn("apply_needs_you", self.urgent_kinds(), "it may have been sent: it waits for the student")
        self.assert_settled()

    def test_a_form_that_rejects_the_submission_is_failed_after_the_click(self):
        run_id = self.handoff(handoff_factory("failed_4xx", wait=0.2))
        self.finished(run_id)
        claim = self.claim_of(run_id)
        self.assertEqual((claim["state"], claim["after_click"]), ("failed", 1))
        self.assertTrue(claim["note"].startswith('Greenhouse marked "'))
        view = self.view(run_id)
        self.assertEqual(view["summary"], claim["note"], "a handed-over attempt is never said to be unsent")
        self.assertEqual(sum(1 for name, _ in self.events() if name == "apply_agent_unconfirmed"), 1)
        self.assertEqual(apply_watch.claim_card(self.conn, USER, claim["token"])["status"], "may_have_been_sent")
        self.assert_settled()

    def test_no_confirmation_page_is_unconfirmed_with_its_event_and_notice(self):
        run_id = self.handoff(handoff_factory("unconfirmed", wait=0.2))
        self.finished(run_id)
        claim = self.claim_of(run_id)
        self.assertEqual((claim["state"], claim["after_click"], claim["note"]), ("unconfirmed", 1, UNCONFIRMED_NOTE))
        detail = next(detail for name, detail in self.events() if name == "apply_agent_unconfirmed")
        self.assertEqual((detail["run_id"], detail["mode"], detail["state"]), (run_id, "handoff", "unconfirmed"))
        self.assertEqual(detail["source"], "apply_agent:watch", "the timeline credits the app, not the student, for what it wrote on its own")
        self.assertEqual(self.notices(), ["Example Robotics, Inc.: your application may or may not have gone through"])
        self.assertEqual(self.view(run_id)["summary"], UNCONFIRMED_NOTE)

    def test_an_agent_that_crashes_before_the_turn_leaves_a_stopped_attempt_nothing_was_sent(self):
        run_id = self.handoff(CrashingAgentFactory())
        row = self.finished(run_id)
        claim = self.claim_of(run_id)
        self.assertEqual((claim["state"], claim["after_click"], claim["note"]), ("failed", 0, apply_runner.CHILD_DIED))
        self.assertEqual((row["outcome"], json.loads(row["reasons_json"])), ("failed", [apply_runner.CHILD_DIED]))
        self.assertEqual(len(self.notices()), 1)
        self.assert_settled()

    def test_a_run_that_could_not_even_begin_says_the_app_could_not_start_it(self):
        real = apply_policy.sources_for

        def fail_in_the_run(*args, **kwargs):
            if threading.current_thread().name.startswith("apply-runner-"):
                raise RuntimeError("a message that must not cross")
            return real(*args, **kwargs)   # the start's own read of the listing is fine

        with mock.patch.object(apply_policy, "sources_for", side_effect=fail_in_the_run):
            run_id = self.handoff()
            row = self.finished(run_id)
        claim = self.claim_of(run_id)
        self.assertEqual((claim["state"], claim["after_click"], claim["note"]), ("failed", 0, apply_runner.NOT_STARTED))
        self.assertEqual(row["outcome"], "failed")
        self.assertNotIn("a message that must not cross", self.everywhere())
        self.assert_settled()

    def test_the_deadline_before_the_hand_over_fails_the_attempt_with_nothing_sent(self):
        runner = ApplyRunner(deadlines={"handoff": 2}, cancel_grace_s=GRACE, security_code_reader=self.reader)
        self.addCleanup(runner.shutdown, 30)
        run_id = self.handoff(handoff_factory(wait=30), runner=runner)
        self.finished(run_id, runner)
        claim = self.claim_of(run_id)
        self.assertEqual((claim["state"], claim["after_click"]), ("failed", 0))
        self.assertTrue(claim["note"].startswith("The run took longer than"), claim["note"])
        self.assertEqual(self.claim_of(run_id)["state"], "failed")
        self.assertIsNone(runner.busy())

    def test_the_deadline_after_the_hand_over_is_unconfirmed_never_failed(self):
        runner = ApplyRunner(deadlines={"handoff": 4}, cancel_grace_s=GRACE, security_code_reader=self.reader)
        self.addCleanup(runner.shutdown, 30)
        run_id = self.handoff(handoff_factory("hang_after_hand_over", wait=0.3), runner=runner)
        self.finished(run_id, runner)
        claim = self.claim_of(run_id)
        self.assertEqual((claim["state"], claim["after_click"], claim["note"]), ("unconfirmed", 1, UNCONFIRMED_NOTE))
        self.assertIn("apply_agent_unconfirmed", [name for name, _ in self.events()])

    def test_the_server_stopping_before_the_hand_over_is_a_failure_that_says_so_and_after_it_unconfirmed(self):
        first = self.handoff(handoff_factory(wait=30))
        self.turn(first)
        self.runner.shutdown(30)
        claim = self.claim_of(first)
        self.assertEqual((claim["state"], claim["after_click"], claim["note"]), ("failed", 0, apply_runner.SERVER_STOPPED))
        self.assertNotIn("stopped_by", json.loads(claim["detail_json"]), "the server stopped it, not the student")
        self.assertEqual(len(self.notices()), 1)
        self.assertIsNone(self.runner.busy())

    def test_the_server_stopping_after_the_hand_over_is_unconfirmed(self):
        run_id = self.handoff(handoff_factory("hang_after_hand_over", wait=0.3))
        self.assertTrue(wait_until(lambda: self.claim_of(run_id)["state"] == "clicking"))
        self.runner.shutdown(30)
        claim = self.claim_of(run_id)
        self.assertEqual((claim["state"], claim["after_click"], claim["note"]), ("unconfirmed", 1, UNCONFIRMED_NOTE))

    def test_a_claim_that_moves_between_the_read_and_the_write_is_decided_again(self):
        real = apply_runs.record_result
        calls = []

        def flip_then_record(*args, **kwargs):
            if not calls:
                # Between the runner reading 'claimed' and writing, the claim is handed over.
                with self.conn:
                    self.conn.execute(
                        "UPDATE application_submit_claims SET state='clicking', after_click=1, handed_over_at=? WHERE run_id=?", (utc_now(), kwargs["run_id"]),
                    )
            calls.append(kwargs["expected_states"])
            return real(*args, **kwargs)

        with mock.patch.object(apply_runs, "record_result", side_effect=flip_then_record):
            run_id = self.handoff(CrashingAgentFactory())
            self.finished(run_id)
        self.assertEqual(calls, [("claimed",), ("clicking",)], "decided again from what was there")
        claim = self.claim_of(run_id)
        self.assertEqual((claim["state"], claim["after_click"], claim["note"]), ("unconfirmed", 1, UNCONFIRMED_NOTE))
        self.assertEqual(self.run_row(run_id)["outcome"], "unconfirmed")
        self.assertEqual(self.run_row(run_id)["status"], "finished")

    def test_a_claim_that_keeps_moving_finishes_only_the_run(self):
        real = apply_runs.record_result

        def lose(*args, **kwargs):
            return {"settled": False, "stage_recorded": False, "state_now": "released"}

        with mock.patch.object(apply_runs, "record_result", side_effect=lose):
            run_id = self.handoff(CrashingAgentFactory())
            self.finished(run_id)
        self.assertEqual(self.run_row(run_id)["status"], "finished")
        self.assertEqual(self.claim_of(run_id)["state"], "claimed", "the claim is left to whoever moved it (recovery settles it)")
        with self.conn:
            self.conn.execute("UPDATE application_submit_claims SET state='failed', note='x' WHERE run_id=?", (run_id,))
        self.assertTrue(callable(real))


    def window_unconfirmed(self, *args, **kwargs):
        """handoff_settlement, deciding row 7 whatever the run did (a Chromium process that survived)."""
        return apply_runner.Settlement(
            "unconfirmed", "unconfirmed", WINDOW_UNCONFIRMED, [WINDOW_UNCONFIRMED], True, expected_states=("claimed",), row=7,
        )

    def assert_recovery_does_not_say_nothing_was_sent(self, run_id, note=WINDOW_UNCONFIRMED):
        self.assertEqual(self.claim_of(run_id)["state"], "claimed", "the settle was not written")
        self.assertEqual(apply_claims.RUNNING, set())
        apply_runs.recover_stale(self.conn)
        claim = self.claim_of(run_id)
        self.assertEqual((claim["state"], claim["after_click"], claim["note"]), ("unconfirmed", 1, note),
                         "recovery said nothing was sent about a window that was never confirmed closed")
        self.assertNotIn("Nothing was sent", claim["note"])
        self.assertIn("apply_agent_unconfirmed", [name for name, _ in self.events()])
        self.assertEqual(self.run_row(run_id)["outcome"], "unconfirmed")
        self.assertNotIn("Nothing was sent", self.run_row(run_id)["reasons_json"])
        self.assertEqual(apply_claims.UNCONFIRMED_UNWRITTEN, {}, "the kept decision is used once")

    def test_a_settle_that_could_not_be_written_is_not_turned_into_nothing_was_sent_by_recovery(self):
        def busy(*args, **kwargs):
            raise sqlite3.OperationalError("database is locked")

        with mock.patch.object(apply_runner, "handoff_settlement", side_effect=self.window_unconfirmed),                 mock.patch.object(apply_runs, "record_result", side_effect=busy):
            run_id = self.handoff(CrashingAgentFactory())
            self.finished(run_id)
        self.assert_recovery_does_not_say_nothing_was_sent(run_id)

    def test_a_recovery_whose_own_write_fails_keeps_the_decision_for_the_next_pass(self):
        with mock.patch.object(apply_runner, "handoff_settlement", side_effect=self.window_unconfirmed),                 mock.patch.object(apply_runs, "record_result", side_effect=sqlite3.OperationalError("database is locked")):
            run_id = self.handoff(CrashingAgentFactory())
            self.finished(run_id)
        token = self.claim_of(run_id)["token"]
        self.assertIn(token, apply_claims.UNCONFIRMED_UNWRITTEN)
        with mock.patch.object(apply_runs, "lock_user", side_effect=sqlite3.OperationalError("database is locked")):
            with self.assertRaises(sqlite3.OperationalError):
                apply_runs.recover_stale(self.conn)
        self.conn.rollback()
        self.assertEqual(self.claim_of(run_id)["state"], "claimed")
        self.assertEqual(apply_claims.UNCONFIRMED_UNWRITTEN.get(token), WINDOW_UNCONFIRMED, "the failed write used the decision up")
        self.assert_recovery_does_not_say_nothing_was_sent(run_id)

    def test_a_claim_that_could_not_be_read_is_not_turned_into_nothing_was_sent_by_recovery(self):
        def busy(conn, token, user_id):
            raise sqlite3.OperationalError("database is locked")

        # No result came back, so nothing proves the application was not handed over: the run is finished as "may have been sent",
        # and the claim (still 'claimed' here) is kept for recovery to settle that way.
        with mock.patch.object(apply_runner.ApplyRunner, "_read_claim", side_effect=busy):
            run_id = self.handoff(CrashingAgentFactory())
            self.finished(run_id)
        row = self.run_row(run_id)
        self.assertEqual(row["outcome"], "unconfirmed")
        self.assertNotRegex(row["reasons_json"], r"(?i)no application was sent|nothing was sent")
        self.assert_recovery_does_not_say_nothing_was_sent(run_id, note=UNCONFIRMED_NOTE)

    def test_a_claim_that_could_not_be_read_after_the_hand_over_never_leaves_a_run_saying_no_application_was_sent(self):
        def busy(conn, token, user_id):
            raise sqlite3.OperationalError("database is locked")

        # The student pressed Submit (the claim is 'clicking'), then the server stopped with no result back, and the claim cannot be read.
        with mock.patch.object(apply_runner.ApplyRunner, "_read_claim", side_effect=busy):
            run_id = self.handoff(handoff_factory("hang_after_hand_over", wait=0.3))
            self.assertTrue(wait_until(lambda: self.claim_of(run_id)["state"] == "clicking"))
            self.runner.shutdown(30)
        row = self.run_row(run_id)
        self.assertEqual((row["status"], row["outcome"]), ("finished", "unconfirmed"))
        self.assertNotRegex(row["reasons_json"], r"(?i)no application was sent|nothing was sent")
        self.assertNotRegex(self.view(run_id)["summary"], r"(?i)no application was sent|nothing was sent|not sent")
        self.assertEqual(self.claim_of(run_id)["state"], "clicking", "the claim is left to recovery, which reads it")
        apply_runs.recover_stale(self.conn)
        claim = self.claim_of(run_id)
        self.assertEqual((claim["state"], claim["after_click"]), ("unconfirmed", 1))
        self.assertNotRegex(self.view(run_id)["summary"], r"(?i)no application was sent|nothing was sent|not sent")

    def test_a_claim_that_could_not_be_read_is_decided_unsent_only_from_a_result_that_proves_it(self):
        def busy(conn, token, user_id):
            raise sqlite3.OperationalError("database is locked")

        proof = RunResult(
            "needs_you", [HANDOFF_NOT_SUBMITTED], handed_over=False, after_click=False, evidence={"handoff_end": "closed", "browser_closed": True},
        )
        continued = RunResult("needs_you", [HANDOFF_NOT_SUBMITTED], handed_over=False, evidence={"handoff_end": "closed", "submit_continued": True})
        # (the child's result, whether the parent itself handed over, whether the run may be called unsent)
        for result, passed_on, unsent in ((proof, False, True), (proof, True, False), (continued, False, False), (None, False, False)):
            with self.subTest(result=result is not None, passed_on=passed_on, continued=result is continued):
                work = mock.Mock(user_id=USER, token="tok-unread", run_id="run-" + "c" * 32, deadline_s=600.0, handed_over_seen=passed_on, job=mock.Mock(ats="greenhouse"))
                written = {}

                def finish_only(conn, work, outcome, documents, written=written):
                    written.update(outcome=outcome, reasons=list(documents["reasons"]))

                with mock.patch.object(apply_runner.ApplyRunner, "_read_claim", side_effect=busy),                         mock.patch.object(apply_runner.ApplyRunner, "_finish_run_only", side_effect=finish_only),                         mock.patch.object(apply_runner.ApplyRunner, "_keep_unconfirmed"):
                    self.runner._finish_handoff(self.conn, work, apply_runner.Supervised(result, closed_confirmed=True), shutting_down=False)
                if unsent:
                    self.assertEqual(written["outcome"], "needs_you")
                else:
                    self.assertEqual((written["outcome"], written["reasons"]), ("unconfirmed", [UNCONFIRMED_NOTE]))

    def test_a_decision_that_may_have_been_sent_is_kept_before_the_first_write_so_recovery_cannot_beat_it(self):
        real = apply_runs.record_result
        calls = []

        def busy_then_real(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                # What record_result does when its write fails: the token leaves RUNNING. A recovery pass lands before the second try.
                apply_claims.forget(kwargs["token"])
                apply_runs.recover_stale(self.conn)
                raise sqlite3.OperationalError("database is locked")
            return real(*args, **kwargs)

        with mock.patch.object(apply_runner, "handoff_settlement", side_effect=self.window_unconfirmed),                 mock.patch.object(apply_runs, "record_result", side_effect=busy_then_real):
            run_id = self.handoff(CrashingAgentFactory())
            self.finished(run_id)
        claim = self.claim_of(run_id)
        self.assertEqual((claim["state"], claim["after_click"], claim["note"]), ("unconfirmed", 1, WINDOW_UNCONFIRMED),
                         "a recovery pass between the two tries said nothing was sent about a window never confirmed closed")
        self.assertNotIn("Nothing was sent", claim["note"])
        self.assertEqual(apply_claims.UNCONFIRMED_UNWRITTEN, {}, "nothing is left kept once the claim says it")

    def test_a_settle_that_failed_but_said_nothing_was_sent_is_still_recovered_as_before(self):
        def busy(*args, **kwargs):
            raise sqlite3.OperationalError("database is locked")

        with mock.patch.object(apply_runs, "record_result", side_effect=busy):
            run_id = self.handoff(CrashingAgentFactory())
            self.finished(run_id)
        apply_runs.recover_stale(self.conn)
        claim = self.claim_of(run_id)
        self.assertEqual((claim["state"], claim["after_click"]), ("failed", 0))
        self.assertIn("Nothing was sent", claim["note"])


class PhaseTests(HandoffCase):
    """Where a running run is, as the page is told: the claim leads, the last step catches up."""

    def test_the_phase_follows_the_claim_and_then_the_last_step(self):
        phase = apply_runner._handoff_phase
        card = {"status": "your_turn"}
        self.assertEqual(phase([], None, "claimed"), "filling")
        self.assertEqual(phase([{"step": "picture"}], None, "claimed"), "filling")
        self.assertEqual(phase([{"step": "picture"}], card, "claimed"), "your_turn", "ready is recorded just before the step is reported")
        self.assertEqual(phase([{"step": "your_turn"}], card, "claimed"), "your_turn")
        self.assertEqual(phase([{"step": "your_turn"}], None, "clicking"), "submitting", "the hand-over committed just before the step catches up")
        self.assertEqual(phase([{"step": "picture"}], None, "clicking"), "submitting")
        for step in ("submitting", "security_code", "code_typed", "code_yours", "challenge"):
            self.assertEqual(phase([{"step": step}], None, "clicking"), step)

    def test_every_phase_has_its_own_sentence_and_none_says_nothing_was_sent(self):
        for step in ("submitting", "security_code", "code_typed", "code_yours", "challenge"):
            row = {"status": "running", "kind": "handoff", "outcome": "", "ats": "greenhouse"}
            text = apply_runner._summary(row, [], {}, "Acme", "Intern", [{"step": "your_turn", "text": "stale"}], False, phase=step)
            self.assertEqual(text, progress_text(step, "Greenhouse"))
            self.assertNotRegex(text.lower(), r"not sent|nothing was sent|no application was sent")
        row = {"status": "running", "kind": "handoff", "outcome": "", "ats": "greenhouse"}
        self.assertEqual(apply_runner._summary(row, [], {}, "", "", [], False, phase="your_turn", nothing_left=False), YOUR_TURN)
        self.assertEqual(apply_runner._summary(row, [], {}, "", "", [], False, phase="your_turn", nothing_left=True), YOUR_TURN_NONE_LEFT)
        self.assertEqual(apply_runner._summary(row, [], {}, "", "", [{"step": "fill", "text": "Filling 3 fields"}], False, phase="filling"), "Filling 3 fields")

    def test_a_form_that_tried_to_send_elsewhere_is_still_the_students_turn_and_says_so_in_its_own_words(self):
        phase = apply_runner._handoff_phase
        card = {"status": "your_turn"}
        told = [{"step": "your_turn", "text": "x"}, {"step": "form_elsewhere", "text": "The form tried to send to apply.example.test, which the app doesn't recognize"}]
        self.assertEqual(phase(told, card, "claimed"), "form_elsewhere")
        # The student's next press commits the hand-over: the phase is submitting at once, not the stale notice.
        self.assertEqual(phase(told, None, "clicking"), "submitting")
        row = {"status": "running", "kind": "handoff", "outcome": "", "ats": "greenhouse"}
        text = apply_runner._summary(row, [], {}, "Acme", "Intern", told, False, phase="form_elsewhere")
        self.assertEqual(text, told[-1]["text"], "the sentence names the host the agent saw, so it is the step's own text")
        self.assertIn("doesn't recognize", PROGRESS_STEPS["form_elsewhere"])
        self.assertNotIn("Nothing was sent", PROGRESS_STEPS["form_elsewhere"], "the step is about the stopped request, not about the application")

    def test_a_turn_with_nothing_left_for_the_student_says_so(self):
        run_id = self.handoff(handoff_factory(wait=30))
        self.turn(run_id)
        evidence = json.loads(self.run_row(run_id)["evidence_json"])
        evidence["left_for_you"] = []
        with self.conn:
            self.conn.execute("UPDATE apply_runs SET evidence_json=? WHERE id=?", (json.dumps(evidence), run_id))
        self.assertEqual(self.view(run_id)["summary"], YOUR_TURN_NONE_LEFT)
        self.assertTrue(apply_runs.request_cancel(self.conn, self.claim_of(run_id)["token"], user_id=USER))
        self.runner.cancel(run_id)
        self.finished(run_id)

    def test_a_handoff_that_stopped_says_not_sent_once_and_one_that_may_have_been_sent_never(self):
        row = {"status": "finished", "kind": "handoff", "outcome": "needs_you", "ats": "greenhouse"}
        self.assertEqual(apply_runner._summary(row, [HANDOFF_NOT_SUBMITTED], {}, "", "", [], False), HANDOFF_NOT_SUBMITTED, "not doubled")
        self.assertEqual(apply_runner._summary(row, ["The form tried to upload a file"], {}, "", "", [], False),
                         "The form tried to upload a file. No application was sent.")
        self.assertEqual(apply_runner._summary({**row, "outcome": "failed"}, ['Greenhouse marked "Why?" as wrong'], {}, "", "", [], False, after_click=True),
                         'Greenhouse marked "Why?" as wrong')


class WindowTimeTests(HandoffCase):
    """The closing time the student is shown is the agent's own (a slow fill shortens the turn), never more than handoff_s."""

    def shown(self, in_s):
        extra = {} if in_s is None else {"in_s": in_s}
        run_id = self.handoff(handoff_factory(wait=2.0, **extra))
        self.turn(run_id)
        view = self.view(run_id)
        detail = self.detail(run_id)
        self.finished(run_id)
        seconds = (parse_app_instant(view["handoff_until"]) - datetime.now(timezone.utc)).total_seconds()
        self.assertEqual(view["handoff_until"], detail["handoff_until"], "the card and the run say the same time")
        return seconds

    def test_the_time_is_the_one_the_agent_says_it_will_keep(self):
        seconds = self.shown(120)
        self.assertTrue(110 < seconds <= 121, seconds)

    def full_window(self, value):
        seconds = self.shown(value)
        self.assertTrue(1190 < seconds <= 1201, seconds)

    def test_a_time_the_agent_did_not_say_is_the_full_window(self):
        self.full_window(None)

    def test_a_negative_time_is_the_full_window(self):
        self.full_window(-5)

    def test_a_time_longer_than_the_window_can_be_is_the_full_window(self):
        self.full_window(99999)

    def test_a_flag_is_not_a_time(self):
        self.full_window(True)

    def test_text_is_not_a_time(self):
        self.full_window("soon")

    def test_the_real_agent_says_how_long_the_turn_will_last(self):
        # The message the agent sends is built from the instant its turn ends, so a late fill is shown as a shorter window.
        source = inspect.getsource(ApplyAgent._student_turn)
        self.assertIn('"handoff_in_s": max(0.0, until - time.monotonic())', source)
        self.assertLess(source.index("until = min(until"), source.index('"handoff_in_s"'), "the time is sent before the cap is applied to it")

class SettlementTableTests(unittest.TestCase):
    """handoff_settlement (5.3), row by row. Pure: every input is a fact read after the child and its browser ended."""

    def settle(self, result=None, *, stop="", shutting_down=False, claim_state="claimed", cancel_requested=False, handed_over=False,
               closed_confirmed=True, minutes=48, not_started=False):
        return handoff_settlement(
            result, stop=stop, shutting_down=shutting_down, claim_state=claim_state, cancel_requested=cancel_requested, handed_over=handed_over,
            closed_confirmed=closed_confirmed, minutes=minutes, not_started=not_started, ats_name="Greenhouse",
        )

    def result(self, outcome, *, reasons=(), handed_over=False, after_click=False, confirmation_seen=False, requests=(), **evidence):
        return RunResult(outcome, list(reasons), handed_over=handed_over, after_click=after_click, confirmation_seen=confirmation_seen,
                         requests=list(requests), evidence=dict(evidence))

    def check(self, got, row, state, after_click, note=None, **fields):
        self.assertEqual((got.row, got.state, got.after_click), (row, state, after_click), got)
        if note is not None:
            self.assertEqual(got.note, note)
        for name, value in fields.items():
            self.assertEqual(getattr(got, name), value, name)

    def test_row_1_a_seen_confirmation_upgrades_an_unconfirmed_recovery(self):
        got = self.settle(self.result("submitted", handed_over=True, after_click=True, confirmation_seen=True), claim_state="unconfirmed")
        self.check(got, 1, "submitted", True, confirmation_seen=True, expected_states=("unconfirmed",))

    def test_row_2_a_settled_claim_is_left_alone(self):
        for state in ("needs_you", "failed", "released", "submitted", ""):
            got = self.settle(self.result("needs_you", reasons=["x"]), claim_state=state)
            self.check(got, 2, "", False, "x", outcome="needs_you")
        self.check(self.settle(None, claim_state="released", stop="deadline"), 2, "", False, outcome="failed")
        self.check(self.settle(self.result("rehearsed"), claim_state="failed"), 2, "", False, outcome="failed", )
        self.check(self.settle(self.result("submitted", confirmation_seen=True), claim_state="submitted"), 2, "", False, outcome="submitted")

    def test_row_2_never_puts_the_childs_not_sent_sentence_on_a_claim_that_may_have_been_sent(self):
        # A recovery moved the claim to unconfirmed (or it was handed over) before the run was settled: only our sentence stands.
        for kwargs in (dict(claim_state="unconfirmed"), dict(claim_state="failed", handed_over=True), dict(claim_state="unconfirmed", handed_over=True)):
            for sentence in (HANDOFF_UNRECORDED, HANDOFF_NOT_SUBMITTED, apply_runner.CHILD_DIED, "Nothing was sent."):
                with self.subTest(sentence=sentence, **kwargs):
                    got = self.settle(self.result("needs_you", reasons=[sentence]), **kwargs)
                    self.check(got, 2, "", True, UNCONFIRMED_NOTE, reasons=[UNCONFIRMED_NOTE], outcome="unconfirmed")
        got = self.settle(None, stop="child_died", claim_state="unconfirmed")
        self.check(got, 2, "", True, UNCONFIRMED_NOTE, outcome="unconfirmed")
        # What does not say "not sent" is kept, and a claim that was not handed over keeps the child's sentence as before.
        kept = self.settle(self.result("failed", reasons=["Greenhouse refused the form (HTTP 422)"]), claim_state="unconfirmed")
        self.check(kept, 2, "", False, "Greenhouse refused the form (HTTP 422)", outcome="failed")
        self.check(self.settle(self.result("needs_you", reasons=[HANDOFF_NOT_SUBMITTED]), claim_state="failed"), 2, "", False, HANDOFF_NOT_SUBMITTED, outcome="needs_you")

    def test_row_4_keeps_a_sentence_that_says_what_greenhouse_did_but_never_one_that_says_nothing_was_sent(self):
        for outcome in ("failed", "needs_you"):
            for sentence in (HANDOFF_NOT_SUBMITTED, apply_runner.CHILD_DIED, "Nothing was sent. Apply from the posting instead", "The app could not record this. No application was sent."):
                with self.subTest(outcome=outcome, sentence=sentence):
                    got = self.settle(self.result(outcome, handed_over=True, after_click=True, reasons=[sentence]), claim_state="clicking")
                    self.check(got, 6, "unconfirmed", True, UNCONFIRMED_NOTE, reasons=[UNCONFIRMED_NOTE])
            got = self.settle(self.result(outcome, handed_over=True, after_click=True, reasons=["Greenhouse refused the form (HTTP 422)"]), claim_state="clicking")
            self.check(got, 4, outcome, True, "Greenhouse refused the form (HTTP 422)")

    def test_rows_3_and_4_a_handed_over_result_is_kept_as_it_says(self):
        for claim_state in ("clicking",):
            sub = self.settle(self.result("submitted", handed_over=True, after_click=True, confirmation_seen=True, reasons=["a"]), claim_state=claim_state)
            self.check(sub, 3, "submitted", True, reasons=["a"], confirmation_seen=True, expected_states=("clicking",))
            for outcome in ("failed", "needs_you"):
                got = self.settle(self.result(outcome, handed_over=True, after_click=True, reasons=["b"]), claim_state=claim_state)
                self.check(got, 4, outcome, True, "b", expected_states=("clicking",))

    def test_row_5_only_the_routes_own_record_that_the_post_was_aborted_is_called_unsent(self):
        got = self.settle(self.result("failed", reasons=["c"], handed_over=True, after_click=False, submit_continued=False), claim_state="clicking")
        self.check(got, 5, "failed", False, "c")
        for evidence in ({}, {"submit_continued": True}, {"submit_continued": None}):
            got = self.settle(self.result("failed", reasons=["c"], handed_over=True, after_click=False, **evidence), claim_state="clicking")
            self.check(got, 6, "unconfirmed", True, UNCONFIRMED_NOTE)

    def test_row_6_anything_else_after_the_hand_over_is_unconfirmed_with_only_our_sentence(self):
        cases = {
            "no result, a stop": dict(result=None, stop="deadline"), "no result, a crash": dict(result=None, stop="child_died"),
            "a result without handed_over": dict(result=self.result("needs_you", reasons=[HANDOFF_UNRECORDED])),
            "unconfirmed": dict(result=self.result("unconfirmed", handed_over=True, after_click=True, reasons=["d"])),
            "submitted without the page": dict(result=self.result("submitted", handed_over=True, after_click=True, reasons=["d"])),
            "failed after_click false": dict(result=self.result("failed", handed_over=True, after_click=False, reasons=[HANDOFF_UNRECORDED])),
            "shutting down": dict(result=None, stop="cancelled", shutting_down=True), "cancel requested": dict(result=None, cancel_requested=True),
        }
        for name, kwargs in cases.items():
            with self.subTest(name):
                got = self.settle(claim_state="clicking", **kwargs)
                self.check(got, 6, "unconfirmed", True, UNCONFIRMED_NOTE, reasons=[UNCONFIRMED_NOTE], notify=True, stopped_by="")
        got = self.settle(self.result("needs_you", reasons=[HANDOFF_UNRECORDED]), claim_state="claimed", handed_over=True)
        self.check(got, 6, "unconfirmed", True, UNCONFIRMED_NOTE)

    def test_row_7_a_window_that_may_still_be_open_means_nothing_can_be_called_unsent(self):
        for result in (None, self.result("needs_you", reasons=[HANDOFF_NOT_SUBMITTED], handoff_end="stopped", browser_closed=True)):
            got = self.settle(result, closed_confirmed=False, cancel_requested=True)
            self.check(got, 7, "unconfirmed", True, WINDOW_UNCONFIRMED, reasons=[WINDOW_UNCONFIRMED], notify=True, stopped_by="")

    def test_row_8_a_claimed_attempt_whose_result_says_it_was_handed_over_is_unconfirmed_and_an_integrity_error(self):
        for result in (
            self.result("submitted", handed_over=True, after_click=True, confirmation_seen=True),
            self.result("needs_you", handed_over=False, submit_continued=True),
            self.result("failed", requests=[{"method": "POST", "host": "boards.greenhouse.io", "path": "/x", "status": 200, "passed": True}]),
        ):
            got = self.settle(result)
            self.check(got, 8, "unconfirmed", True, UNCONFIRMED_NOTE, integrity_error=True, notify=True)
        quiet = self.result("needs_you", requests=[{"method": "GET", "host": "x", "path": "/", "status": 200, "passed": True},
                                                   {"method": "POST", "host": "c.spl.greenhouse.io", "path": "/", "status": None, "passed": False}])
        self.assertNotEqual(self.settle(quiet).row, 8, "a GET that passed and a POST that was refused are not a send")

    def test_row_9_the_students_stop_wins_whatever_came_back(self):
        for result in (None, self.result("failed", reasons=["x"]), self.result("needs_you", reasons=[HANDOFF_UNRECORDED], handoff_end="refused")):
            got = self.settle(result, cancel_requested=True, stop="cancelled")
            self.check(got, 9, "needs_you", False, HANDOFF_NOT_SUBMITTED, notify=False, stopped_by="student", reasons=[HANDOFF_NOT_SUBMITTED])

    def test_row_10_the_server_stopping_is_not_the_students_stop(self):
        got = self.settle(None, stop="cancelled", shutting_down=True)
        self.check(got, 10, "failed", False, apply_runner.SERVER_STOPPED, notify=True, stopped_by="")

    def test_row_11_a_cleanly_closed_window_is_the_students_doing(self):
        got = self.settle(self.result("failed", reasons=[WINDOW_CLOSED], handoff_end="closed", browser_closed=True))
        self.check(got, 11, "needs_you", False, WINDOW_CLOSED, notify=False, stopped_by="student")
        got = self.settle(self.result("needs_you", reasons=[HANDOFF_NOT_SUBMITTED], handoff_end="closed", browser_closed=True))
        self.check(got, 11, "needs_you", False, HANDOFF_NOT_SUBMITTED, notify=False, stopped_by="student")

    def test_a_window_that_crashed_is_not_the_students_doing_and_is_noticed(self):
        # A renderer that crashed leaves the page open and the browser alive: the browser closes cleanly afterwards, but nobody closed it.
        got = self.settle(self.result("needs_you", reasons=[HANDOFF_CRASHED], handoff_end="crashed", browser_closed=True))
        self.check(got, 13, "needs_you", False, HANDOFF_CRASHED, notify=True, stopped_by="")

    def test_row_12_a_stop_the_student_did_not_ask_for_is_a_failure(self):
        for end in ("stopped", "closed"):
            got = self.settle(self.result("needs_you", reasons=["x"], handoff_end=end, browser_closed=False))
            self.check(got, 12, "failed", False, apply_runner.CHILD_DIED, notify=True, stopped_by="")

    def test_row_13_any_other_result_keeps_a_needs_you_or_failed_and_nothing_else(self):
        self.check(self.settle(self.result("needs_you", reasons=[apply_runner.CHILD_DIED + "!"])), 13, "needs_you", False, apply_runner.CHILD_DIED + "!")
        self.check(self.settle(self.result("failed", reasons=["boom"])), 13, "failed", False, "boom")
        self.check(self.settle(self.result("rehearsed", reasons=["x"])), 13, "failed", False, apply_runner.CHILD_DIED)
        self.check(self.settle(self.result("needs_you")), 13, "failed", False, apply_runner.CHILD_DIED, outcome="failed")

    def test_rows_14_and_15_no_result(self):
        self.check(self.settle(None, stop="deadline", minutes=48), 14, "failed", False, outcome="failed")
        self.assertTrue(self.settle(None, stop="deadline", minutes=48).note.startswith("The run took longer than 48 minutes"))
        for stop in ("child_died", "error", "cancelled", ""):
            self.check(self.settle(None, stop=stop), 15, "failed", False, apply_runner.CHILD_DIED)

    def test_row_15_says_the_browser_never_opened_when_nothing_ran(self):
        got = self.settle(None, stop="error", not_started=True)
        self.check(got, 15, "failed", False, apply_runner.NOT_STARTED)

    def test_the_decision_names_the_state_it_was_made_from(self):
        self.assertEqual(self.settle(None, stop="deadline").expected_states, ("claimed",))
        self.assertEqual(self.settle(None, stop="deadline", claim_state="clicking").expected_states, ("clicking",))

    def test_no_row_before_the_hand_over_says_unconfirmed_except_the_window_and_the_integrity_rows(self):
        for result in (None, self.result("failed"), self.result("needs_you", handoff_end="stopped")):
            for stop in ("", "deadline", "child_died", "cancelled"):
                got = self.settle(result, stop=stop)
                self.assertEqual(got.after_click, False, got)
                self.assertNotEqual(got.state, "unconfirmed")


class SecurityCodeRunnerTests(HandoffCase):
    """The parent's side of D10 B: the reader runs off the pump thread for the run's own claim, and "typed" is the child's word."""

    def code_run(self, reader, *, runner=None, **extra):
        runner = runner or ApplyRunner(cancel_grace_s=GRACE, security_code_reader=reader)
        self.addCleanup(runner.shutdown, 30)
        self.runner = runner
        return self.handoff(handoff_factory("security_code", wait=0.2, **extra), runner=runner)

    def spy_replies(self):
        replies = []
        real = apply_runner._answer

        def spy(inbox, message):
            if message.get("op") == OP_SECURITY_CODE_REPLY:
                replies.append((threading.current_thread().name, message.get("id"), message.get("status")))
            return real(inbox, message)

        return replies, mock.patch.object(apply_runner, "_answer", side_effect=spy)

    def test_the_reader_is_asked_for_the_runs_own_claim_off_the_pump_thread_and_confirmed_by_the_child(self):
        reader = FakeReader()
        replies, patched = self.spy_replies()
        with patched:
            run_id = self.code_run(reader)
            row = self.finished(run_id)
        token = self.claim_of(run_id)["token"]
        self.assertEqual(reader.asked, [(USER, token)], "the parent answers for its own run's claim, never one the child names")
        self.assertTrue(reader.threads and all(name.startswith("apply-security-code") for name in reader.threads), reader.threads)
        self.assertEqual(reader.confirmed, [(USER, token, True, "")])
        self.assertIn(token, reader.forgotten)
        self.assertEqual([(status, isinstance(ident, int)) for name, ident, status in replies], [("found", True)])
        self.assertTrue(replies[0][0].startswith("apply-runner-"), "only the supervisor thread sends to the child")
        steps = [item["step"] for item in json.loads(row["progress_json"])]
        self.assertEqual(steps[-3:], ["submitting", "security_code", "code_typed"])
        evidence = json.loads(row["evidence_json"])
        self.assertEqual((evidence["security_code"]["prompted"], evidence["security_code"]["typed"]), (True, True), "the child's own record stays")
        self.assertIn("security_code_reader", evidence)
        self.assertEqual(self.claim_of(run_id)["state"], "submitted")
        self.assertNotIn(CODE, self.everywhere(), "the code is in one pipe reply and nowhere else")

    def real_reader(self):
        reader = apply_security_code.SecurityCodeReader()
        for patcher in (
            mock.patch.object(apply_security_code.SecurityCodeReader, "_unable", return_value=""),
            mock.patch.object(apply_security_code, "find_code", return_value=apply_security_code.CodeAnswer("found", code=CODE)),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        return reader

    def test_typed_is_recorded_only_when_the_child_says_it_typed_the_code(self):
        run_id = self.code_run(self.real_reader())
        self.finished(run_id)
        record = self.detail(run_id)[apply_security_code.RECORD_KEY]
        self.assertEqual(record["reader"], "typed")
        self.assertTrue(record["handed_at"] and record["typed_at"])
        self.assertIn("Apply for me entered the security code Greenhouse emailed you for Example Robotics, Inc.", self.notices())
        self.assertEqual(apply_watch.ats_statistics(self.conn, USER)["security_code_typed"], 1)
        self.assertNotIn(CODE, self.everywhere())

    def test_a_code_the_child_could_not_type_is_a_fallback_and_never_counted_as_typed(self):
        run_id = self.code_run(self.real_reader(), code_typed=False, code_reason="inputs_not_empty")
        row = self.finished(run_id)
        record = self.detail(run_id)[apply_security_code.RECORD_KEY]
        self.assertEqual((record["reader"], record["reason"]), ("fallback", "inputs_not_empty"))
        self.assertFalse([title for title in self.notices() if "entered the security code" in title], "no 'entered' notice")
        self.assertTrue([title for title in self.notices() if "security code" in title])
        self.assertEqual(apply_watch.ats_statistics(self.conn, USER)["security_code_typed"], 0)
        self.assertEqual(json.loads(row["evidence_json"])["security_code"]["typed"], False)
        self.assertNotIn(CODE, self.everywhere())

    def test_a_reader_that_fails_tells_the_child_to_fall_back_and_the_run_still_ends(self):
        class Broken(FakeReader):
            def answer(self, conn, *, user_id, token, now=None):
                raise RuntimeError("a message that must not cross")

        run_id = self.code_run(Broken())
        self.finished(run_id)
        self.assertEqual(self.claim_of(run_id)["state"], "submitted")
        self.assertNotIn("a message that must not cross", self.everywhere())

    def assert_prompt_counted_without_a_reader_record(self, reader):
        run_id = self.code_run(reader)
        self.finished(run_id)
        detail = self.detail(run_id)
        self.assertFalse((detail.get(apply_security_code.RECORD_KEY) or {}).get("prompted_at"), "the reader recorded no prompt")
        self.assertIs(detail.get("security_code"), True, "6.14's boolean is written when the child saw the prompt")
        self.assertEqual(apply_watch.ats_statistics(self.conn, USER)["security_code_prompts"], 1)

    def test_a_prompt_whose_first_reader_answer_failed_is_still_counted_by_the_statistics(self):
        class Broken(FakeReader):
            def answer(self, conn, *, user_id, token, now=None):
                raise RuntimeError("the database was busy")

        self.assert_prompt_counted_without_a_reader_record(Broken())

    def test_a_prompt_the_reader_recorded_nothing_for_is_still_counted_by_the_statistics(self):
        self.assert_prompt_counted_without_a_reader_record(FakeReader())

    def test_a_slow_gmail_does_not_stop_the_heartbeats(self):
        beats = set()
        reader = FakeReader(delay=3.5)
        with mock.patch.object(apply_runner, "HEARTBEAT_EVERY_S", 0.2):
            run_id = self.code_run(reader)
            self.assertTrue(wait_until(lambda: len(reader.asked) == 1, 30), "Gmail was never asked")
            end = time.monotonic() + 2.5   # all of it inside the 3.5 second read
            while time.monotonic() < end:
                claim, run = self.claim_of(run_id), self.run_row(run_id)
                beats.add((claim["heartbeat_at"], run["heartbeat_at"]))
                time.sleep(0.05)
            self.finished(run_id)
        self.assertGreaterEqual(len({claim for claim, _ in beats}), 3, "the claim's heartbeat advanced while Gmail was being read")
        self.assertGreaterEqual(len({run for _, run in beats}), 3, "and the run's")

    def test_a_reader_that_never_returns_cannot_hold_the_deadline(self):
        runner = ApplyRunner(deadlines={"handoff": 4}, cancel_grace_s=GRACE, security_code_reader=FakeReader(delay=5))
        # The stuck read holds its own connection until it ends; the database cannot be removed before that.
        self.addCleanup(lambda: wait_until(lambda: not any(t.name.startswith("apply-security-code") and t.is_alive() for t in threading.enumerate()), 30))
        started = time.monotonic()
        run_id = self.code_run(None, runner=runner)
        self.finished(run_id, runner)
        self.assertLess(time.monotonic() - started, 9, "the watchdog fired on time while Gmail was being read")
        claim = self.claim_of(run_id)
        self.assertEqual((claim["state"], claim["after_click"]), ("unconfirmed", 1))

    def test_asks_that_arrive_while_one_is_being_answered_get_that_answers_result(self):
        reader = FakeReader(delay=0.4)
        answers = apply_runner._CodeAnswers(reader, self.path, USER, "tok-1")
        self.addCleanup(answers.close)
        answers.ask(1)
        answers.ask(2)
        self.assertEqual(answers.ready(), [], "nothing to send while Gmail is being read")
        self.assertTrue(wait_until(lambda: len(reader.asked) == 1))
        replies = []
        end = time.monotonic() + 5
        while not replies and time.monotonic() < end:
            replies = answers.ready()
            time.sleep(0.05)
        self.assertEqual([(item["op"], item["id"], item["status"]) for item in replies],
                         [(OP_SECURITY_CODE_REPLY, 1, "found"), (OP_SECURITY_CODE_REPLY, 2, "found")])
        self.assertEqual(len(reader.asked), 1, "one look in Gmail served both asks")
        self.assertEqual(answers.ready(), [], "each reply is sent once")


class FrontTests(HandoffCase):
    def test_the_request_thread_only_counts_and_the_supervisor_thread_sends_one_op_front(self):
        sent = []
        real = apply_runner._answer

        def spy(inbox, message):
            if message.get("op") == OP_FRONT:
                sent.append(threading.current_thread().name)
            return real(inbox, message)

        with mock.patch.object(apply_runner, "_answer", side_effect=spy):
            # The student's turn lasts until the request has reached the agent (then half a second more, for a second
            # OP_FRONT to show up if one were sent). A fixed 1.5 s turn raced the request on a loaded machine.
            run_id = self.handoff(handoff_factory(wait=15.0, after_front=0.5))
            self.turn(run_id)
            self.assertFalse(self.runner.front("run-" + "0" * 32), "only the run this app is running")
            caller = threading.current_thread().name
            self.assertTrue(self.runner.front(run_id))
            row = self.finished(run_id)
        self.assertEqual(len(sent), 1)
        self.assertTrue(sent[0].startswith("apply-runner-") and sent[0] != caller, sent)
        self.assertEqual(json.loads(row["evidence_json"])["fronts"], 1, "the agent was told once")

    def test_a_finished_run_has_no_window_to_bring_forward(self):
        run_id = self.handoff()
        self.finished(run_id)
        self.assertFalse(self.runner.front(run_id))
        self.assertFalse(self.view(run_id)["can_front"])


class PipeTests(unittest.TestCase):
    """The child's end of the pipe: fail closed on a bad frame, answer only the ask that is outstanding, stamp when it stops waiting."""

    def setUp(self):
        context = multiprocessing.get_context("spawn")
        self.inbox_recv, self.inbox_send = context.Pipe(duplex=False)
        self.outbox_recv, self.outbox_send = context.Pipe(duplex=False)
        self.channel = ChildChannel(self.inbox_recv, self.outbox_send, ApplyTimeouts(reply_s=0.4))
        self.addCleanup(self.close_pipes)

    def close_pipes(self):
        # The parent's end first: the reader reads end-of-file and closes the inbox itself. Closing the inbox from this thread
        # while the reader may still be inside recv() is the race test_the_inbox_is_closed_only_by_its_reader is about.
        self.inbox_send.close()
        self.assertTrue(wait_until(lambda: self.inbox_recv.closed, 5), "the reader never closed the inbox after end-of-file")
        for end in (self.outbox_recv, self.outbox_send):
            end.close()

    def test_the_inbox_is_closed_only_by_its_reader(self):
        # The agent's thread closes the channel as the run ends (child_main's finally), often just after the reader filed the
        # hand-over reply that woke it, so the reader is on its way back into recv(). Had the agent's thread closed the inbox
        # then, a pipe made next (the next test's, on an xdist worker) can take the freed descriptor, and the stale reader
        # reads that pipe's frames: CI lost test_a_corrupt_frame_stops_the_run_visibly_and_marks_the_parent_gone's frame so.
        inbox_recv, inbox_send = multiprocessing.get_context("spawn").Pipe(duplex=False)
        _, outbox_send = multiprocessing.get_context("spawn").Pipe(duplex=False)
        entered, release = threading.Event(), threading.Event()

        class HeldInbox:
            """The real inbox, with the reader held inside its second recv() (after it filed the first message)."""

            calls = 0

            def recv(self):
                HeldInbox.calls += 1
                if HeldInbox.calls == 2:
                    entered.set()
                    release.wait(5)
                return inbox_recv.recv()

            def close(self):
                inbox_recv.close()

        channel = ChildChannel(HeldInbox(), outbox_send, ApplyTimeouts(reply_s=0.4))
        inbox_send.send({"op": OP_FRONT})
        self.assertTrue(entered.wait(5), "the reader never came back into recv()")
        channel.close()   # what child_main does as the run ends
        self.assertFalse(inbox_recv.closed, "the agent's thread closed the inbox while the reader was inside recv()")
        self.assertTrue(outbox_send.closed, "the agent's own end, the outbox, is closed at once")
        release.set()
        self.assertFalse(channel.parent_gone())
        inbox_send.close()
        self.assertTrue(wait_until(lambda: channel.parent_gone() and inbox_recv.closed, 5), "the reader closes the inbox at end-of-file")

    def test_a_corrupt_frame_stops_the_run_visibly_and_marks_the_parent_gone(self):
        self.assertFalse(self.channel.cancelled() or self.channel.parent_gone())
        self.inbox_send.send_bytes(b"this is not a pickle")
        self.assertTrue(wait_until(lambda: self.channel.cancelled() and self.channel.parent_gone(), 5))
        self.assertEqual(self.channel.code_reply(1), {"status": "fallback", "reason": "not_current"})
        self.assertFalse(self.channel.hand_over(), "a hand-over is never granted after that")

    def test_the_parent_closing_its_end_stops_a_run_that_has_not_handed_over(self):
        self.inbox_send.close()
        self.assertTrue(wait_until(lambda: self.channel.cancelled() and self.channel.parent_gone(), 5))

    def test_only_the_outstanding_ask_is_answered_and_a_reply_is_taken_once(self):
        first = self.channel.ask_code()
        self.assertEqual(self.outbox_recv.recv(), {"op": "security_code", "id": first})
        self.inbox_send.send({"op": OP_SECURITY_CODE_REPLY, "id": first + 5, "status": "found", "code": CODE})
        self.inbox_send.send({"op": OP_SECURITY_CODE_REPLY, "id": first, "status": "waiting", "reason": ""})
        self.assertTrue(wait_until(lambda: self.channel._code_replies, 5))
        self.assertEqual(self.channel.code_reply(first + 5), None, "a reply for an id that is not pending was dropped on arrival")
        self.assertEqual(self.channel.code_reply(first)["status"], "waiting")
        self.assertIsNone(self.channel.code_reply(first), "taken once")
        self.inbox_send.send({"op": OP_SECURITY_CODE_REPLY, "id": first, "status": "found", "code": CODE})
        time.sleep(0.3)
        self.assertIsNone(self.channel.code_reply(first), "a late reply for an ask that was answered is dropped")
        second = self.channel.ask_code()
        self.assertNotEqual(first, second)
        self.inbox_send.send({"op": OP_SECURITY_CODE_REPLY, "id": second, "status": "found", "code": CODE})
        self.assertTrue(wait_until(lambda: self.channel._code_replies, 5))
        self.assertEqual(self.channel.code_reply(second)["code"], CODE)
        self.assertNotIn(CODE, repr(vars(self.channel)), "nothing is kept after the agent took it")

    def test_an_ask_the_agent_stopped_waiting_for_is_dropped_and_a_late_reply_is_never_kept(self):
        first = self.channel.ask_code()
        self.assertEqual(self.outbox_recv.recv(), {"op": "security_code", "id": first})
        self.channel.abandon_code()
        self.assertEqual(self.outbox_recv.recv(), {"op": "security_code_result", "id": first, "typed": False, "reason": "abandoned"},
                         "the parent is told once, so its reader drops what it found")
        self.assertIsNone(self.channel._code_pending)
        self.inbox_send.send({"op": OP_SECURITY_CODE_REPLY, "id": first, "status": "found", "code": CODE})
        time.sleep(0.4)
        self.assertEqual(self.channel._code_replies, {}, "a reply for an abandoned ask was filed")
        self.assertNotIn(CODE, repr(vars(self.channel)), "the code of an abandoned ask is held in the child")
        self.assertIsNone(self.channel.code_reply(first))
        # A reply that had already arrived when the agent gave up is dropped with the ask.
        second = self.channel.ask_code()
        self.assertEqual(self.outbox_recv.recv(), {"op": "security_code", "id": second})
        self.inbox_send.send({"op": OP_SECURITY_CODE_REPLY, "id": second, "status": "found", "code": CODE})
        self.assertTrue(wait_until(lambda: self.channel._code_replies, 5))
        self.channel.abandon_code()
        self.assertEqual(self.channel._code_replies, {})
        self.assertNotIn(CODE, repr(vars(self.channel)))
        self.assertEqual(self.outbox_recv.recv()["reason"], "abandoned")
        # With nothing outstanding there is nothing to abandon and nothing is sent.
        self.channel.abandon_code()
        self.assertFalse(self.outbox_recv.poll(0.3))
        # An ask that was answered and taken is not outstanding either.
        third = self.channel.ask_code()
        self.assertEqual(self.outbox_recv.recv(), {"op": "security_code", "id": third})
        self.inbox_send.send({"op": OP_SECURITY_CODE_REPLY, "id": third, "status": "waiting", "reason": ""})
        self.assertTrue(wait_until(lambda: self.channel._code_replies, 5))
        self.assertEqual(self.channel.code_reply(third)["status"], "waiting")
        self.channel.abandon_code()
        self.assertFalse(self.outbox_recv.poll(0.3))

    def test_a_code_result_and_ready_are_one_way_and_a_dead_pipe_is_never_an_exception(self):
        self.channel.code_result(3, True, "")
        self.channel.ready({"plan": [], "plan_hash": "h", "left": []})
        self.assertEqual(self.outbox_recv.recv(), {"op": "security_code_result", "id": 3, "typed": True, "reason": ""})
        ready = self.outbox_recv.recv()
        self.assertEqual((ready["op"], ready["plan_hash"]), ("handoff_ready", "h"))
        self.outbox_recv.close()
        self.channel.code_result(4, False, "bad_code")
        self.channel.ready({"plan": []})
        self.assertIsInstance(self.channel.ask_code(), int)

    def test_the_front_requests_are_counted_and_each_is_seen_once(self):
        for _ in range(2):
            self.inbox_send.send({"op": OP_FRONT})
        self.assertTrue(wait_until(lambda: self.channel._front == 2, 5))
        self.assertEqual([self.channel.front_requested() for _ in range(3)], [True, True, False])

    def test_the_hand_over_request_says_when_the_child_stops_waiting(self):
        before = time.monotonic()
        self.assertFalse(self.channel.hand_over(), "no answer is a no")
        message = self.outbox_recv.recv()
        self.assertEqual(message["op"], "hand_over")
        self.assertGreaterEqual(message["expires"], before + 0.4 - 0.05)
        self.assertLess(message["expires"], time.monotonic() + 0.5)


class HandOverDeadlineTests(ApplyCase):
    """The parent never commits a hand-over after the child gave up waiting for it (spec 5.2 rule 3, invariant I1)."""

    def setUp(self):
        super().setUp()
        self.claim = self.start("hand-over-1")
        self.token = self.claim["token"]

    def test_a_deadline_in_the_past_refuses_and_leaves_the_claim_claimed(self):
        self.assertFalse(apply_runs.hand_over(self.conn, self.token, user_id=USER, deadline=time.monotonic() - 1))
        row = self.claim_row(self.token)
        self.assertEqual((row["state"], row["handed_over_at"], row["after_click"]), ("claimed", None, 0))
        self.assertTrue(apply_runs.hand_over(self.conn, self.token, user_id=USER, deadline=time.monotonic() + 30))
        row = self.claim_row(self.token)
        self.assertEqual((row["state"], row["after_click"]), ("clicking", 1))
        self.assertEqual(json.loads(row["detail_json"])["waiting"], "", "a clicking claim never says the student is still working")

    def test_a_deadline_that_passes_while_the_profile_is_read_refuses_and_leaves_the_claim_claimed(self):
        # The deadline comparison is the last step before the UPDATE: nothing slow may sit between them.
        real = apply_runs.application_address

        def slow(*args, **kwargs):
            time.sleep(0.2)
            return real(*args, **kwargs)

        with mock.patch.object(apply_runs, "application_address", side_effect=slow):
            self.assertFalse(apply_runs.hand_over(self.conn, self.token, user_id=USER, deadline=time.monotonic() + 0.1))
        row = self.claim_row(self.token)
        self.assertEqual((row["state"], row["handed_over_at"], row["after_click"]), ("claimed", None, 0))

    def test_a_request_that_waited_in_the_queue_past_its_expiry_is_refused(self):
        case = self

        class Inbox:
            sent = []
            committed = []

            def send(self, message):
                self.sent.append(message)
                # What another connection sees at the moment the reply goes out: a commit, not a write that is still open.
                self.committed.append((ApplyCase.committed_claim_row(case, case.token)["state"], case.conn.in_transaction))

        inbox = Inbox()
        handlers = SupervisorHandlers(hand_over=lambda deadline=None: apply_runs.hand_over(self.conn, self.token, user_id=USER, deadline=deadline))
        apply_runner._dispatch({"op": "hand_over", "id": 7, "expires": time.monotonic() - 0.5}, handlers, inbox)
        self.assertEqual(inbox.sent, [{"op": OP_HAND_OVER_REPLY, "id": 7, "ok": False}])
        self.assertEqual(self.claim_row(self.token)["state"], "claimed")
        apply_runner._dispatch({"op": "hand_over", "id": 8, "expires": time.monotonic() + 30}, handlers, inbox)
        self.assertEqual(inbox.sent[-1], {"op": OP_HAND_OVER_REPLY, "id": 8, "ok": True})
        self.assertEqual(inbox.committed, [("claimed", False), ("clicking", False)], "the reply went out before the commit, or with it still open")

    def test_the_reply_is_sent_after_the_commit(self):
        order = []

        class Inbox:
            def send(self, message):
                order.append(("reply", message["ok"], ApplyCase.committed_claim_row(case, case.token)["state"], case.conn.in_transaction))

        case = self
        handlers = SupervisorHandlers(hand_over=lambda deadline=None: apply_runs.hand_over(self.conn, self.token, user_id=USER, deadline=deadline))
        apply_runner._dispatch({"op": "hand_over", "id": 1, "expires": time.monotonic() + 30}, handlers, Inbox())
        self.assertEqual(order, [("reply", True, "clicking", False)], "the child is told yes only once another connection sees clicking")

    def test_the_commit_checks_see_a_hand_over_that_never_committed(self):
        """A negative control for the two tests above: the same checks fail when the write is left open."""
        class NeverCommits:
            def __init__(self, conn):
                self._conn = conn

            def __enter__(self):
                return self._conn

            def __exit__(self, *exc):
                return False       # neither commit nor rollback

            def __getattr__(self, name):
                return getattr(self._conn, name)

        self.assertTrue(apply_runs.hand_over(NeverCommits(self.conn), self.token, user_id=USER, deadline=time.monotonic() + 30))
        self.assertEqual(self.claim_row(self.token)["state"], "clicking", "the writing connection sees its own open write")
        self.assertTrue(self.conn.in_transaction)
        self.assertEqual(self.committed_claim_row(self.token)["state"], "claimed", "another connection sees no commit")

    def test_a_handler_that_does_not_take_a_deadline_is_still_called_and_one_that_raises_is_a_no(self):
        class Inbox:
            sent = []

            def send(self, message):
                self.sent.append(message["ok"])

        inbox = Inbox()
        apply_runner._dispatch({"op": "hand_over", "id": 1}, SupervisorHandlers(hand_over=lambda: True), inbox)

        def boom(deadline=None):
            raise RuntimeError("x")

        apply_runner._dispatch({"op": "hand_over", "id": 2}, SupervisorHandlers(hand_over=boom), inbox)
        self.assertEqual(inbox.sent, [True, False])


class AbortNeverCommitsAHandOverTests(ApplyCase):
    """A run that is being aborted (server shutdown, account deletion) never commits the student's Submit, whenever the press arrives."""

    def setUp(self):
        super().setUp()
        self.claim = self.start("hand-over-abort-1")
        self.token = self.claim["token"]

    def test_the_pump_refuses_a_hand_over_asked_once_abort_is_set_without_asking_the_handler(self):
        class Inbox:
            sent = []

            def send(self, message):
                self.sent.append(message)

        abort = threading.Event()
        pump = apply_runner._Pump(worker=mock.Mock(), in_process=False, reply_s=10.0, outcome=apply_runner.Supervised(None), abort=abort)
        asked = []
        handlers = SupervisorHandlers(hand_over=lambda deadline=None: asked.append(1) or apply_runs.hand_over(self.conn, self.token, user_id=USER, deadline=deadline))
        inbox = Inbox()
        abort.set()
        apply_runner._dispatch({"op": "hand_over", "id": 3, "expires": time.monotonic() + 30}, handlers, inbox, pump)
        self.assertEqual(inbox.sent, [{"op": OP_HAND_OVER_REPLY, "id": 3, "ok": False}])
        self.assertEqual(asked, [], "the handler was asked although the run was being aborted")
        self.assertEqual(ApplyCase.committed_claim_row(self, self.token)["state"], "claimed")
        abort.clear()
        apply_runner._dispatch({"op": "hand_over", "id": 4, "expires": time.monotonic() + 30}, handlers, inbox, pump)
        self.assertEqual(inbox.sent[-1], {"op": OP_HAND_OVER_REPLY, "id": 4, "ok": True}, "without an abort the same press commits (the control)")


class AbortReachesTheRunnersHandOverTests(HandoffCase):
    def test_the_runners_own_hand_over_checks_abort_just_before_it_commits(self):
        seen = {}

        def supervise_that_aborts(factory, job, *, handlers, abort, **_kwargs):
            abort.set()          # shutdown arrives between the pump's look and the handler
            seen["granted"] = handlers.hand_over()
            seen["state"] = self.conn.execute("SELECT state FROM application_submit_claims").fetchone()[0]
            return apply_runner.Supervised(None, stop=apply_runner.STOP_ERROR, error="Stopped")

        with mock.patch.object(apply_runner, "supervise", supervise_that_aborts):
            run_id = self.handoff()
            self.finished(run_id)
        self.assertFalse(seen["granted"])
        self.assertEqual(seen["state"], "claimed", "the hand-over was committed while the run was being aborted")


class KillOrderingTests(HandoffCase):
    """Real processes: the child is a spawned process and a sleeping grandchild stands in for Chromium.

    The browser is killed, by pid, and confirmed gone before the claim is settled as "nothing was sent" (I3): a spy on
    record_result looks at both processes at the moment the settlement is written. It looks with ``still_running``, which asks
    about the very processes the child started, never with ``process_alive``: that one says True when it cannot tell, so on Linux
    it can call a browser the runner had already confirmed dead (a zombie) running while init reaps it, a moment after the runner's
    own look, and on Windows a freed pid can belong to another program by then.
    """

    def setUp(self):
        super().setUp()
        self.pid_file = self.root / "pids.txt"
        self.alive_at_settle = None
        self.grandchildren = {}
        self.addCleanup(self.kill_leftovers)

    def kill_leftovers(self):
        # Only a process that is still the one first seen at its pid: most tests have asserted these dead, and a freed pid may be a stranger's.
        for pid, started in self.grandchildren.items():
            kill_if_same_process(pid, started)

    def recorded(self):
        """What the child wrote once it had started its grandchild (each pid with its start), or None before it has."""
        try:
            return json.loads(self.pid_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def pids(self):
        self.assertTrue(wait_until(lambda: self.recorded() is not None, 60), "the child never started its grandchild")
        seen = self.recorded()
        self.assertTrue(seen["child_start"] and seen["grandchild_start"], "the child could not read when its processes started")
        self.grandchildren[seen["grandchild"]] = seen["grandchild_start"]
        return seen["child"], seen["grandchild"]

    def spy(self):
        real = apply_runs.record_result

        def look(*args, **kwargs):
            seen = self.recorded() if self.alive_at_settle is None else None
            if seen is not None:
                self.alive_at_settle = {
                    "child": still_running(seen["child"], seen["child_start"]),
                    "grandchild": still_running(seen["grandchild"], seen["grandchild_start"]),
                }
            return real(*args, **kwargs)

        return mock.patch.object(apply_runs, "record_result", side_effect=look)

    def process_run(self, outcome="submitted", *, wait=0.5, deadline=None, runner=None, **factory):
        runner = runner or ApplyRunner(deadlines={"handoff": deadline} if deadline else None, cancel_grace_s=GRACE, security_code_reader=self.reader)
        self.addCleanup(runner.shutdown, 60)
        self.runner = runner
        factory = ProcessCannedFactory(outcome=outcome, wait=wait, pid_file=str(self.pid_file), **factory)
        return self.handoff(factory, runner=runner), runner

    def finished_slowly(self, run_id, runner):
        self.assertTrue(runner.wait(run_id, 120), "the run did not finish")
        return self.run_row(run_id)

    def test_the_watchdog_before_the_hand_over_kills_the_whole_tree_and_then_says_nothing_was_sent(self):
        with self.spy():
            run_id, runner = self.process_run(wait=120, deadline=20)
            child, grandchild = self.pids()
            self.finished_slowly(run_id, runner)
        claim = self.claim_of(run_id)
        self.assertEqual((claim["state"], claim["after_click"]), ("failed", 0))
        self.assertTrue(claim["note"].startswith("The run took longer than"), claim["note"])
        self.assertEqual(self.alive_at_settle, {"child": False, "grandchild": False}, "gone before the claim was settled")
        self.assertFalse(apply_runner.process_alive(grandchild))
        self.assert_settled()

    def test_the_watchdog_after_the_hand_over_is_unconfirmed_never_failed(self):
        with self.spy():
            run_id, runner = self.process_run("hang_after_hand_over", wait=0.2, deadline=25)
            self.pids()
            row = self.finished_slowly(run_id, runner)
        claim = self.claim_of(run_id)
        self.assertEqual((claim["state"], claim["after_click"], claim["note"]), ("unconfirmed", 1, UNCONFIRMED_NOTE))
        self.assertEqual(self.alive_at_settle, {"child": False, "grandchild": False})
        self.assertEqual(json.loads(row["reasons_json"]), [UNCONFIRMED_NOTE])
        self.assertEqual(len(self.notices()), 1)

    def test_a_driver_that_dies_during_the_turn_leaves_a_browser_the_parent_kills_by_pid(self):
        before_end = {}
        real_end = apply_runner._end_child

        def look(worker, outcome):
            before_end.update(outcome.pids)    # what the snapshots taken while the child ran had found, before the final look
            return real_end(worker, outcome)

        with self.spy(), mock.patch.object(apply_runner, "_end_child", look):
            run_id, runner = self.process_run(wait=60, crash_at="turn", leak=True)
            child, grandchild = self.pids()
            self.finished_slowly(run_id, runner)
        self.assertIn(grandchild, before_end, "the snapshot taken while the child ran did not find the browser: only the final look did")
        claim = self.claim_of(run_id)
        self.assertEqual((claim["state"], claim["after_click"], claim["note"]), ("failed", 0, apply_runner.CHILD_DIED))
        self.assertEqual(self.alive_at_settle, {"child": False, "grandchild": False}, "the orphaned browser was found by pid and killed first")
        self.assertFalse(apply_runner.process_alive(grandchild))
        self.assertEqual(json.loads(self.run_row(run_id)["evidence_json"])["runner"]["closed_confirmed"], True)

    def test_a_driver_that_dies_right_after_the_hand_over_is_unconfirmed(self):
        with self.spy():
            run_id, runner = self.process_run(wait=0.3, crash_at="after_hand_over", leak=True)
            child, grandchild = self.pids()
            self.finished_slowly(run_id, runner)
        claim = self.claim_of(run_id)
        self.assertEqual((claim["state"], claim["after_click"], claim["note"]), ("unconfirmed", 1, UNCONFIRMED_NOTE))
        self.assertEqual(self.alive_at_settle, {"child": False, "grandchild": False})

    def test_the_server_stopping_before_and_after_the_hand_over(self):
        with self.spy():
            run_id, runner = self.process_run(wait=120)
            self.pids()
            self.assertTrue(wait_until(lambda: self.detail(run_id).get("waiting") == "student", 60))
            runner.shutdown(90)
        claim = self.claim_of(run_id)
        self.assertEqual((claim["state"], claim["after_click"], claim["note"]), ("failed", 0, apply_runner.SERVER_STOPPED))
        self.assertNotIn("stopped_by", json.loads(claim["detail_json"]))
        self.assertEqual(self.alive_at_settle, {"child": False, "grandchild": False})
        self.assertEqual(len(self.notices()), 1)
        self.alive_at_settle = None
        self.pid_file.unlink()
        with self.spy():
            run_id, runner = self.process_run("hang_after_hand_over", wait=0.3)
            self.pids()
            self.assertTrue(wait_until(lambda: self.claim_of(run_id)["state"] == "clicking", 60))
            runner.shutdown(90)
        claim = self.claim_of(run_id)
        self.assertEqual((claim["state"], claim["after_click"], claim["note"]), ("unconfirmed", 1, UNCONFIRMED_NOTE))
        self.assertEqual(self.alive_at_settle, {"child": False, "grandchild": False})

    def test_a_browser_that_cannot_be_killed_turns_nothing_was_sent_into_may_have_been_sent(self):
        run_id, runner = self.process_run(wait=120, leak=True)
        child, grandchild = self.pids()
        real = apply_runner.process_alive
        with mock.patch.object(apply_runner, "process_alive", side_effect=lambda pid: True if pid == grandchild else real(pid)):
            self.assertTrue(wait_until(lambda: self.detail(run_id).get("waiting") == "student", 60))
            self.assertTrue(apply_runs.request_cancel(self.conn, self.claim_of(run_id)["token"], user_id=USER))
            self.assertTrue(runner.cancel(run_id))
            self.finished_slowly(run_id, runner)
        claim = self.claim_of(run_id)
        self.assertEqual((claim["state"], claim["after_click"], claim["note"]), ("unconfirmed", 1, WINDOW_UNCONFIRMED), "even the student's own Stop")
        self.assertEqual(len(self.notices()), 1)
        self.assertNotIn("stopped_by", json.loads(claim["detail_json"]))
        self.assertEqual(json.loads(self.run_row(run_id)["evidence_json"])["runner"]["closed_confirmed"], False)

    def test_a_process_list_that_cannot_be_read_never_lets_nothing_was_sent_be_said(self):
        # Before anything is killed or counted the parent must know what runs below the child. When it cannot read that list after
        # the child did anything, it cannot say the browser is gone, so a Stop is not "nothing was sent".
        with mock.patch.object(apply_runner, "_process_table", return_value=None):
            run_id, runner = self.process_run(wait=120)
            self.assertTrue(wait_until(lambda: self.detail(run_id).get("waiting") == "student", 60))
            self.assertTrue(apply_runs.request_cancel(self.conn, self.claim_of(run_id)["token"], user_id=USER))
            self.assertTrue(runner.cancel(run_id))
            self.finished_slowly(run_id, runner)
        claim = self.claim_of(run_id)
        self.assertEqual((claim["state"], claim["after_click"], claim["note"]), ("unconfirmed", 1, WINDOW_UNCONFIRMED))
        evidence = json.loads(self.run_row(run_id)["evidence_json"])["runner"]
        self.assertEqual((evidence["closed_confirmed"], evidence["row"]), (False, 7))

    def test_a_process_list_that_cannot_be_read_before_the_child_did_anything_is_not_a_doubt(self):
        outcome = apply_runner.Supervised(None, snapshot_failed=True, saw_activity=False)
        worker = mock.Mock(pid=0)
        worker.is_alive.return_value = False
        with mock.patch.object(apply_runner, "_process_table", return_value=None):
            apply_runner._end_child(worker, outcome)
        self.assertTrue(outcome.closed_confirmed, "nothing ran, so nothing can be open")
        outcome = apply_runner.Supervised(None, saw_activity=True)
        worker = mock.Mock(pid=4194311)      # a pid no platform hands out (past Linux pid_max, not a multiple of 4 for Windows)
        worker.is_alive.return_value = False
        with mock.patch.object(apply_runner, "_process_table", return_value=None):
            apply_runner._end_child(worker, outcome)
        self.assertEqual((outcome.snapshot_failed, outcome.closed_confirmed), (True, False), "a list that could not be read after activity is a doubt")

    def test_a_clean_run_in_a_real_process_confirms_every_process_gone(self):
        run_id, runner = self.process_run("submitted", wait=0.3)
        child, grandchild = self.pids()
        row = self.finished_slowly(run_id, runner)
        self.assertEqual(self.claim_of(run_id)["state"], "submitted")
        self.assertEqual(json.loads(row["evidence_json"])["runner"]["closed_confirmed"], True)
        self.assertTrue(wait_until(lambda: not apply_runner.process_alive(child) and not apply_runner.process_alive(grandchild), 10))

    def test_descendants_lists_a_grandchild_on_windows_too(self):
        script = "import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)']); time.sleep(600)"
        child = subprocess.Popen([sys.executable, "-c", script])
        self.addCleanup(child.kill)
        # Runs first: the grandchild goes with it, even when the wait below fails. Never after the child was reaped: its pid is free then.
        self.addCleanup(lambda: apply_runner.kill_tree(child.pid) if child.poll() is None else None)
        self.assertTrue(wait_until(lambda: bool(apply_runner.descendants(child.pid)), 30), "the grandchild was never listed")
        found = apply_runner.descendants(child.pid)
        self.assertTrue(all(isinstance(pid, int) and pid != child.pid for pid in found))
        killed = apply_runner.kill_tree(child.pid)
        child.wait(timeout=30)
        self.assertTrue(all(wait_until(lambda pid=pid: not apply_runner.process_alive(pid), 15) for pid in killed))

    def test_the_process_table_is_readable(self):
        table = apply_runner._process_table()
        self.assertIsNotNone(table)
        self.assertIn(os.getpid(), table)


@unittest.skipIf(os.name == "nt", "process groups and reparenting are POSIX; Windows keeps an orphan's parent pid")
class OrphanGroupTests(unittest.TestCase):
    """On POSIX an orphan is reparented at once, so the parent-pid walk cannot find what a child started a moment before it died.

    The child calls setsid, so what it started stays in its process group, and the group is what finds it.
    """

    def test_a_process_the_child_started_is_found_and_killed_by_its_group_after_the_child_died(self):
        script = "import os, subprocess, sys; p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)']); print(p.pid, flush=True); os._exit(3)"
        child = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True, start_new_session=True)
        grandchild = int(child.stdout.readline())
        child.wait(timeout=30)
        self.addCleanup(apply_runner._kill_pid, grandchild)
        self.assertNotIn(grandchild, apply_runner._descendants_of(apply_runner._process_table() or {}, child.pid),
                         "an orphan was reparented: the parent-pid walk is what cannot find it")
        self.assertEqual(apply_runner._group_members(child.pid), [grandchild])
        worker = mock.Mock(pid=child.pid)
        worker.is_alive.return_value = False
        outcome = apply_runner.Supervised(None, saw_activity=True)
        apply_runner._end_child(worker, outcome)
        self.assertIn(grandchild, outcome.pids, "the final look did not find what the dead child had started")
        self.assertTrue(wait_until(lambda: not apply_runner.process_alive(grandchild), 15), "the orphan was left running")
        self.assertTrue(outcome.closed_confirmed)

    def test_the_group_of_a_process_that_shares_the_servers_own_is_never_taken_for_the_childs(self):
        self.assertEqual(apply_runner._group_members(os.getpgrp()), [], "the server's own group is not a child's")
        self.assertEqual(apply_runner._group_members(1), [])


class StaleParentTests(unittest.TestCase):
    """Windows keeps a dead parent's pid in its children's table rows, and hands the freed pid to the next process that starts.

    An old, unrelated process whose recorded parent pid is now the child's (or one of its descendants') must never be listed as below
    the child, remembered, or killed. The tables here are fake, so this holds on every platform.
    """

    CHILD, DRIVER, BROWSER, OLD, BELOW_OLD = 100, 200, 300, 400, 500
    TABLE = {CHILD: 1, DRIVER: CHILD, BROWSER: DRIVER, OLD: DRIVER, BELOW_OLD: OLD}
    # Creation times, sortable: the old process (and what it started) is older than the child that was handed its parent's pid.
    STARTS = {CHILD: "0010", DRIVER: "0020", BROWSER: "0030", OLD: "0001", BELOW_OLD: "0002"}

    def test_a_process_older_than_its_parent_is_not_below_it(self):
        found = apply_runner._descendants_of(self.TABLE, self.CHILD, started=self.STARTS.get)
        self.assertEqual(found, [self.DRIVER, self.BROWSER])
        self.assertEqual(apply_runner._descendants_of(self.TABLE, self.CHILD), [self.DRIVER, self.BROWSER, self.OLD, self.BELOW_OLD],
                         "without creation times the table is taken as it stands (POSIX, where an orphan is reparented)")

    def test_a_process_older_than_the_child_is_not_below_it_even_when_its_parent_has_exited(self):
        # The driver died (its start time cannot be read now) and left its browser; a stale row names the driver as the old process's parent.
        starts = {pid: when for pid, when in self.STARTS.items() if pid != self.DRIVER}
        found = apply_runner._descendants_of(self.TABLE, self.CHILD, started=starts.get, known={self.DRIVER: self.STARTS[self.DRIVER]})
        self.assertEqual(found, [self.DRIVER, self.BROWSER], "the browser of a dead driver is still found by the start time recorded earlier")
        # With nothing recorded for the dead driver, the child's own start is the floor.
        found = apply_runner._descendants_of(self.TABLE, self.CHILD, started=starts.get)
        self.assertNotIn(self.OLD, found)
        self.assertNotIn(self.BELOW_OLD, found)

    def test_a_process_whose_start_cannot_be_read_is_not_listed(self):
        starts = {**self.STARTS, self.BROWSER: None}
        self.assertEqual(apply_runner._descendants_of(self.TABLE, self.CHILD, started=starts.get), [self.DRIVER])

    def test_a_snapshot_never_remembers_or_kills_a_stale_child(self):
        worker = mock.Mock(pid=self.CHILD)
        worker.is_alive.return_value = False
        outcome = apply_runner.Supervised(None, saw_activity=True)
        outcome.started[self.CHILD] = self.STARTS[self.CHILD]
        pump = apply_runner._Pump(worker=worker, in_process=True, reply_s=1.0, outcome=outcome)
        killed = []
        with mock.patch.object(apply_runner, "_process_table", return_value=dict(self.TABLE)),                 mock.patch.object(apply_runner, "_start_check", return_value=self.STARTS.get),                 mock.patch.object(apply_runner, "_group_members", return_value=[]),                 mock.patch.object(apply_runner, "process_start", side_effect=lambda pid: self.STARTS.get(pid)),                 mock.patch.object(apply_runner, "process_alive", return_value=True),                 mock.patch.object(apply_runner, "_kill_pid", side_effect=killed.append),                 mock.patch.object(apply_runner, "VERIFY_S", 0.2), mock.patch.object(apply_runner.time, "sleep"):
            apply_runner._snapshot(pump, force=True)
            self.assertEqual(sorted(outcome.pids), [self.DRIVER, self.BROWSER])
            apply_runner._end_child(worker, outcome)
        self.assertNotIn(self.OLD, killed)
        self.assertNotIn(self.BELOW_OLD, killed)
        self.assertNotIn(self.OLD, outcome.pids)
        self.assertNotIn(self.BELOW_OLD, outcome.pids)


class ProcessIdentityTests(unittest.TestCase):
    """A pid is not an identity: a freed pid is handed to an unrelated program, which must never be killed or counted (I3's pid check)."""

    def sleeper(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.addCleanup(child.wait, 30)
        self.addCleanup(child.kill)
        self.assertTrue(wait_until(lambda: apply_runner.process_start(child.pid) is not None, 30), "the start time of a running process was unreadable")
        return child

    def test_a_process_start_is_read_and_differs_between_processes(self):
        mine = apply_runner.process_start(os.getpid())
        self.assertTrue(mine)
        self.assertEqual(apply_runner.process_start(os.getpid()), mine)
        other = self.sleeper()
        self.assertNotEqual(apply_runner.process_start(other.pid), mine)

    def test_the_look_at_settle_follows_the_process_not_its_pid(self):
        # KillOrderingTests' instrument. Running: True. Another start at the pid (a program handed a freed pid): False. Exited: False,
        # also before it is reaped and while process_alive, which says True when it cannot tell, would still call it running.
        child = self.sleeper()
        started = apply_runner.process_start(child.pid)
        self.assertTrue(still_running(child.pid, started))
        self.assertFalse(still_running(child.pid, "the start of the process that had this pid before"))
        self.assertTrue(still_running(child.pid, None), "with no recorded start it cannot tell, so it must not say the process is gone")
        child.kill()
        self.assertTrue(wait_until(lambda: not still_running(child.pid, started), 15), "a killed process still read as running")
        if os.name == "nt" or hasattr(os, "pidfd_open"):
            # Not reaped yet: a zombie on Linux, a process object this Popen still holds a handle to on Windows.
            with mock.patch.object(apply_runner, "process_alive", return_value=True):
                self.assertFalse(still_running(child.pid, started), "an exited process read as running because process_alive could not tell")
        child.wait(timeout=30)
        self.assertFalse(still_running(child.pid, started))

    def test_the_snapshots_keep_when_each_process_started(self):
        child = self.sleeper()
        outcome = apply_runner.Supervised(None)
        apply_runner._remember(outcome, {child.pid: os.getpid()}, [child.pid])
        self.assertEqual(outcome.pids, {child.pid: os.getpid()})
        self.assertEqual(outcome.started, {child.pid: apply_runner.process_start(child.pid)})

    def test_a_pid_now_held_by_another_process_is_neither_killed_nor_counted(self):
        stranger = self.sleeper()
        foreign = []
        with mock.patch.object(apply_runner, "VERIFY_S", 1.0):
            survivors = apply_runner._kill_survivors(
                {stranger.pid: os.getpid()}, own=None, started={stranger.pid: "the start of the process that had this pid before"}, foreign=foreign,
            )
        self.assertEqual(survivors, [], "an unrelated process counted against the close")
        self.assertEqual(foreign, [stranger.pid])
        self.assertIsNone(stranger.poll(), "an unrelated process was killed")
        self.assertTrue(apply_runner.process_alive(stranger.pid))

    def test_the_same_process_is_killed_and_confirmed_gone(self):
        # The control for the test above: with the start time that matches, the same call ends the process.
        ours = self.sleeper()
        foreign = []
        with mock.patch.object(apply_runner, "VERIFY_S", 5.0):
            survivors = apply_runner._kill_survivors(
                {ours.pid: os.getpid()}, own=None, started={ours.pid: apply_runner.process_start(ours.pid)}, foreign=foreign,
            )
        self.assertEqual((survivors, foreign), ([], []))
        self.assertTrue(wait_until(lambda: ours.poll() is not None, 15), "a process of the run was not killed")

    def test_without_a_recorded_start_the_parent_rule_still_protects_a_pid_whose_parent_is_alive(self):
        child = self.sleeper()
        foreign = []
        # Recorded as a child of a process that is still running, though it is not one: a pid that was handed to someone else.
        real = apply_runner._process_table
        with mock.patch.object(apply_runner, "VERIFY_S", 1.0), mock.patch.object(apply_runner, "_process_table", lambda: {**(real() or {}), child.pid: os.getppid()}):
            survivors = apply_runner._kill_survivors({child.pid: os.getpid()}, own=None, started={}, foreign=foreign)
        self.assertEqual((survivors, foreign), ([], [child.pid]))
        self.assertIsNone(child.poll())

    def test_a_remembered_pid_now_held_by_a_process_windows_will_not_show_is_not_ours_and_does_not_unconfirm_the_close(self):
        pid = 4194312      # a pid no platform hands out; every look at it is patched
        foreign, killed = [], []
        with mock.patch.object(apply_runner, "VERIFY_S", 0.5), mock.patch.object(apply_runner, "process_alive", lambda item: True),                 mock.patch.object(apply_runner, "process_start", lambda item: None), mock.patch.object(apply_runner, "process_access_denied", lambda item: True),                 mock.patch.object(apply_runner, "_kill_pid", killed.append):
            survivors = apply_runner._kill_survivors({pid: os.getpid()}, own=None, started={pid: "when the browser process that had this pid started"}, foreign=foreign)
        self.assertEqual((survivors, foreign, killed), ([], [pid], []), "a system process that was handed a freed pid counted against the close")

    def test_a_remembered_pid_whose_start_is_unreadable_for_any_other_reason_still_counts_as_running(self):
        pid = 4194312
        with mock.patch.object(apply_runner, "VERIFY_S", 0.5), mock.patch.object(apply_runner, "process_alive", lambda item: True),                 mock.patch.object(apply_runner, "process_start", lambda item: None), mock.patch.object(apply_runner, "process_access_denied", lambda item: False),                 mock.patch.object(apply_runner, "_kill_pid", lambda item: None):
            survivors = apply_runner._kill_survivors({pid: os.getpid()}, own=None, started={pid: "recorded"})
        self.assertEqual(survivors, [pid], "a check that cannot be made must not say a browser is gone")

    def test_a_close_is_confirmed_when_the_only_survivor_is_a_pid_windows_will_not_show(self):
        pid = 4194312
        outcome = apply_runner.Supervised(None, saw_activity=True, pids={pid: os.getpid()}, started={pid: "recorded"})
        worker = mock.Mock(pid=4194316)
        worker.is_alive.return_value = False
        with mock.patch.object(apply_runner, "VERIFY_S", 0.5), mock.patch.object(apply_runner, "_process_table", return_value={os.getpid(): 1}),                 mock.patch.object(apply_runner, "process_alive", lambda item: item == pid), mock.patch.object(apply_runner, "process_start", lambda item: None),                 mock.patch.object(apply_runner, "process_access_denied", lambda item: True), mock.patch.object(apply_runner, "_kill_pid", lambda item: None):
            apply_runner._end_child(worker, outcome)
        self.assertTrue(outcome.closed_confirmed)
        self.assertNotIn(pid, outcome.killed_pids)

    def test_access_denied_is_false_for_a_process_of_ours(self):
        self.assertFalse(apply_runner.process_access_denied(os.getpid()))

    def test_a_liveness_check_that_cannot_be_made_counts_the_process_as_running(self):
        with mock.patch.object(apply_runner.os, "name", "nt"), mock.patch.object(apply_runner, "_windows_process", return_value=(True, None, True)):
            self.assertTrue(apply_runner.process_alive(4), "a check that failed said the process was gone")
        with mock.patch.object(apply_runner.os, "name", "nt"), mock.patch.object(apply_runner, "_windows_process", return_value=(False, None, False)):
            self.assertFalse(apply_runner.process_alive(4))
        with mock.patch.object(apply_runner.os, "name", "nt"), mock.patch.object(apply_runner, "_windows_process", return_value=(True, "start", False)):
            self.assertFalse(apply_runner.process_alive(4), "a process that exited but is still held open by someone is not running")

    @unittest.skipUnless(os.name == "nt", "the Windows process calls")
    def test_windows_says_a_process_is_running_gone_or_unknown_without_starting_a_program(self):
        exists, started, running = apply_runner._windows_process(os.getpid())
        self.assertEqual((exists, running, bool(started)), (True, True, True))
        child = self.sleeper()
        child.kill()
        child.wait(30)
        # A finished process whose handle is still held by this test is "exists, not running"; once freed it is "no such process".
        self.assertTrue(wait_until(lambda: not apply_runner.process_alive(child.pid), 15))
        with mock.patch("subprocess.run", side_effect=AssertionError("a program was started to look at a process")):
            apply_runner.process_alive(os.getpid())

    @unittest.skipUnless(os.name == "nt", "the Windows process calls")
    def test_windows_gives_a_failed_look_as_running(self):
        import ctypes

        with mock.patch.object(ctypes, "WinDLL", side_effect=OSError("no kernel32")):
            self.assertEqual(apply_runner._windows_process(os.getpid()), (True, None, True))

class DataCase(ApplyCase):
    """A handoff's rows written by hand: a claim, and the run that goes with it."""

    def attempt(self, opportunity_id="op-data", *, now=None, **claim):
        """The claim of an attempt (the application and its started event with it), and its run, linked both ways."""
        run_id = f"run-{hashlib.sha256(opportunity_id.encode()).hexdigest()[:32]}"
        claim.setdefault("acknowledged", ("company_limit",))
        taken = self.start(opportunity_id, "handoff", run_id=run_id, now=now, **claim)
        apply_runs.create_run(
            self.conn, user_id=USER, opportunity_id=opportunity_id, kind="handoff", started_by="student", ats="greenhouse", adapter_version="greenhouse-1", board_token="bluefin",
            page_url="https://boards.example.test/bluefin/1", company="bluefin", deadline_seconds=2910, run_id=run_id,
            application_id=taken["application_id"], claim_token=taken["token"], now=now,
        )
        return taken["token"], run_id

    def run_row(self, run_id):
        return dict(self.conn.execute("SELECT * FROM apply_runs WHERE id=?", (run_id,)).fetchone())

    def hand_over(self, token):
        """Hand a claim over, at a time long before the next attempt's spacing check (several attempts share one student)."""
        self.days = getattr(self, "days", 0) + 1
        return apply_runs.hand_over(self.conn, token, user_id=USER, now=self.at(days=self.days - 80))

    def event_names(self, token):
        application = self.claim_row(token)["application_id"]
        return self.events(application)


class RunRowTests(DataCase):
    def test_a_run_made_with_a_given_id_uses_it_and_a_bad_id_is_refused(self):
        token, run_id = self.attempt()
        self.assertEqual(self.claim_row(token)["run_id"], run_id)
        self.assertEqual(self.run_row(run_id)["claim_token"], token)
        for bad in ("run-xyz", "x" * 36, "run-" + "G" * 32, "run-" + "a" * 31):
            with self.assertRaises(ValueError):
                apply_runs.create_run(
                    self.conn, user_id=USER, opportunity_id="op-data", kind="handoff", started_by="student", ats="greenhouse", adapter_version="greenhouse-1", board_token="bluefin",
                    page_url="https://boards.example.test/b", company="bluefin", deadline_seconds=10, run_id=bad,
                )
        minted = apply_runs.create_run(
            self.conn, user_id=USER, opportunity_id="op-data", kind="rehearsal", started_by="student", ats="greenhouse", adapter_version="greenhouse-1", board_token="bluefin",
            page_url="https://boards.example.test/b", company="bluefin", deadline_seconds=10,
        )
        self.assertRegex(minted, r"^run-[0-9a-f]{32}$")

    def test_the_company_limit_tick_carries_the_date_of_the_application_it_is_about(self):
        token, _run = self.attempt("op-first")
        apply_runs.hand_over(self.conn, token, user_id=USER, now=self.at(days=-3))
        block = apply_runs.limit_check(self.conn, USER, employer_key(BLUEFIN), "greenhouse", "bluefin", "handoff", self.at())
        self.assertEqual((block.kind, block.code), ("ask", "company_limit"))
        self.assertRegex(block.date, r"^[A-Z][a-z]+ \d{1,2}$")
        self.assertIn("3 days ago", block.message)

    def test_a_block_that_is_not_the_company_limit_has_no_date(self):
        token, _run = self.attempt("op-first")
        apply_runs.hand_over(self.conn, token, user_id=USER, now=self.at(minutes=-1))
        block = apply_runs.limit_check(self.conn, USER, employer_key(BLUEFIN), "greenhouse", "bluefin", "handoff", self.at())
        self.assertEqual((block.code, block.date), ("spacing", ""))

    def test_an_application_the_app_made_is_not_one_the_student_applied_to_by_hand(self):
        token, _run = self.attempt("op-old", now=self.at(days=-2))
        apply_runs.settle(self.conn, token, user_id=USER, state="needs_you", after_click=False, note="stopped", now=self.at(days=-2, minutes=5))
        job = dict(opportunity_id="op-old", ats="greenhouse", job_ref="bluefin/op-old", company=employer_key(BLUEFIN))
        self.assertIsNone(apply_runs.duplicate_block(self.conn, USER, now=self.at(), **job), "its own row is not asked about")
        # An application the student made by hand two days ago still asks.
        self.opportunity("op-hand")
        stamp = self.at(days=-2).isoformat(timespec="microseconds")
        with self.conn:
            self.conn.execute(
                "INSERT INTO applications(id, opportunity_id, user_id, stage, created_at, updated_at) VALUES('app-op-hand', 'op-hand', ?, 'applying', ?, ?)",
                (USER, stamp, stamp),
            )
        hand = dict(opportunity_id="op-hand", ats="greenhouse", job_ref="bluefin/op-hand", company=employer_key(BLUEFIN))
        block = apply_runs.duplicate_block(self.conn, USER, now=self.at(), **hand)
        self.assertEqual((block.kind, block.code), ("ask", "applying_old"))
        self.assertIsNone(apply_runs.duplicate_block(self.conn, USER, acknowledged=("applying_old",), now=self.at(), **hand))

    def test_a_row_made_by_the_app_and_a_newer_started_event_still_count_as_the_apps(self):
        token, _run = self.attempt("op-old", now=self.at(days=-2))
        apply_runs.settle(self.conn, token, user_id=USER, state="failed", after_click=False, note="stopped", now=self.at(days=-2, minutes=1))
        # A second attempt makes a second started event, with a newer stamp; the row's own is still there.
        second = self.start("op-old", "handoff", now=self.at(days=-1))
        apply_runs.settle(self.conn, second["token"], user_id=USER, state="failed", after_click=False, note="stopped", now=self.at(days=-1, minutes=1))
        self.assertIsNone(apply_runs.duplicate_block(
            self.conn, USER, opportunity_id="op-old", ats="greenhouse", job_ref="bluefin/op-old", company=employer_key(BLUEFIN), now=self.at()))


class RecordResultTests(DataCase):
    def handed_over(self, name="op-data"):
        token, run_id = self.attempt(name)
        self.assertTrue(self.hand_over(token))
        return token, run_id

    def settle(self, token, run_id, **kwargs):
        defaults = dict(user_id=USER, token=token, run_id=run_id, state="unconfirmed", outcome="unconfirmed", note="may have been sent")
        return apply_runs.record_result(self.conn, **{**defaults, **kwargs})

    def test_every_may_have_been_sent_settle_leaves_the_timeline_event_in_the_same_transaction(self):
        for state, after_click, expect in (("unconfirmed", True, True), ("needs_you", True, True), ("failed", True, True), ("needs_you", False, False), ("failed", False, False)):
            with self.subTest(state=state, after_click=after_click):
                token, run_id = self.attempt(f"op-{state}-{after_click}")
                if after_click:
                    self.hand_over(token)
                outcome = state if state != "unconfirmed" else "unconfirmed"
                self.settle(token, run_id, state=state, outcome=outcome, after_click=after_click)
                names = self.event_names(token)
                self.assertEqual("apply_agent_unconfirmed" in names, expect, names)
        token, run_id = self.handed_over("op-submitted")
        self.settle(token, run_id, state="submitted", outcome="submitted", confirmation_seen=True)
        self.assertNotIn("apply_agent_unconfirmed", self.event_names(token))

    def test_the_event_is_decided_from_the_row_after_the_settle_so_a_none_after_click_still_writes_it(self):
        token, run_id = self.handed_over()
        self.settle(token, run_id, state="needs_you", outcome="needs_you", after_click=None)
        self.assertEqual(self.claim_row(token)["after_click"], 1)
        detail = [json.loads(row["detail_json"]) for row in self.conn.execute(
            "SELECT detail_json FROM application_events WHERE event_type='apply_agent_unconfirmed'").fetchall()]
        self.assertEqual(len(detail), 1)
        self.assertEqual((detail[0]["run_id"], detail[0]["mode"], detail[0]["state"], detail[0]["note"]), (run_id, "handoff", "needs_you", "may have been sent"))

    def test_the_claims_detail_is_merged_and_waiting_is_cleared(self):
        token, run_id = self.attempt()
        with self.conn:
            self.conn.execute("UPDATE application_submit_claims SET detail_json=? WHERE token=?", (json.dumps({"waiting": "student", "handoff_until": "x", "keep": 1}), token))
        self.settle(token, run_id, state="needs_you", outcome="needs_you", after_click=False, detail={"waiting": "", "stopped_by": "student"}, notify=False)
        self.assertEqual(json.loads(self.claim_row(token)["detail_json"]), {"waiting": "", "stopped_by": "student", "handoff_until": "x", "keep": 1})

    def test_notify_false_writes_no_notice_and_true_writes_one(self):
        token, run_id = self.attempt("op-quiet")
        self.settle(token, run_id, state="needs_you", outcome="needs_you", after_click=False, notify=False)
        self.assertEqual(self.notices(), [])
        token, run_id = self.attempt("op-loud")
        self.settle(token, run_id, state="needs_you", outcome="needs_you", after_click=False)
        self.assertEqual(len(self.notices()), 1)

    def test_the_plan_and_its_hash_are_stored_on_the_run(self):
        token, run_id = self.attempt()
        plan = [{"key": "first_name", "question": "First Name", "disposition": "fill"}]
        self.settle(token, run_id, state="needs_you", outcome="needs_you", after_click=False, plan=plan, plan_hash="abc", notify=False)
        row = self.run_row(run_id)
        self.assertEqual((json.loads(row["plan_json"]), row["plan_hash"], row["status"], row["outcome"]), (plan, "abc", "finished", "needs_you"))

    def test_a_claim_that_moved_is_left_alone_and_so_is_its_run(self):
        token, run_id = self.attempt()
        self.hand_over(token)   # the claim is clicking now
        got = self.settle(token, run_id, state="failed", outcome="failed", after_click=False, expected_states=("claimed",))
        self.assertEqual(got, {"settled": False, "stage_recorded": False, "state_now": "clicking"})
        row = self.claim_row(token)
        self.assertEqual((row["state"], row["after_click"]), ("clicking", 1), "never the unsent failure")
        self.assertEqual(self.run_row(run_id)["status"], "running")
        self.assertNotIn("apply_agent_unconfirmed", self.event_names(token))
        again = self.settle(token, run_id, state="unconfirmed", outcome="unconfirmed", after_click=True, expected_states=("clicking",))
        self.assertEqual(again, {"settled": True, "stage_recorded": False})
        self.assertEqual(self.run_row(run_id)["status"], "finished")
        self.assertEqual(self.claim_row(token)["state"], "unconfirmed")

    def test_without_expected_states_a_released_claim_still_finishes_its_run_as_before(self):
        token, run_id = self.attempt()
        self.hand_over(token)
        with self.conn:
            self.conn.execute("UPDATE application_submit_claims SET state='released' WHERE token=?", (token,))
        got = self.settle(token, run_id, state="needs_you", outcome="needs_you", after_click=True)
        self.assertEqual((got["settled"], got["state_now"]), (False, "released"))
        self.assertEqual(self.run_row(run_id)["status"], "finished")

    def test_expected_states_must_name_claim_states(self):
        token, run_id = self.attempt()
        for bad in ((), ("nonsense",)):
            with self.assertRaises(ValueError):
                self.settle(token, run_id, state="failed", outcome="failed", expected_states=bad)

    def test_a_late_confirmation_page_upgrades_an_unconfirmed_claim_only_when_the_caller_names_unconfirmed(self):
        token, run_id = self.handed_over()
        self.settle(token, run_id, state="unconfirmed", outcome="unconfirmed", after_click=True)
        self.assertEqual(self.claim_row(token)["state"], "unconfirmed")
        other = apply_runs.create_run(
            self.conn, user_id=USER, opportunity_id="op-data", kind="handoff", started_by="student", ats="greenhouse", adapter_version="greenhouse-1", board_token="bluefin",
            page_url="https://boards.example.test/b", company="bluefin", deadline_seconds=10,
        )
        got = self.settle(token, other, state="submitted", outcome="submitted", confirmation_seen=True, expected_states=("unconfirmed",))
        self.assertTrue(got["settled"])
        self.assertEqual((self.claim_row(token)["state"], self.claim_row(token)["resolved_by"]), ("submitted", "page"))


class ReadyTests(DataCase):
    def ready(self, token, run_id, **kwargs):
        defaults = dict(user_id=USER, run_id=run_id, token=token, plan=[{"key": "a"}], plan_hash="ph", screenshots=[{"step": "filled", "path": "x.png"}],
                        evidence={"left_for_you": [{"key": "q", "question": "Q?", "reason": "r"}]}, handoff_until="2030-01-01T00:00:00+00:00")
        return apply_runs.record_handoff_ready(self.conn, **{**defaults, **kwargs})

    def test_it_stores_the_plan_on_the_run_and_the_students_turn_on_the_claim(self):
        token, run_id = self.attempt()
        self.assertTrue(self.ready(token, run_id))
        run, claim = self.run_row(run_id), self.claim_row(token)
        self.assertEqual((json.loads(run["plan_json"]), run["plan_hash"], json.loads(run["screenshots_json"])[0]["step"]), ([{"key": "a"}], "ph", "filled"))
        self.assertEqual(json.loads(run["evidence_json"])["left_for_you"][0]["question"], "Q?")
        self.assertEqual(claim["plan_hash"], "ph")
        detail = json.loads(claim["detail_json"])
        self.assertEqual((detail["waiting"], detail["handoff_until"], detail["acknowledged"]), ("student", "2030-01-01T00:00:00+00:00", ["company_limit"]))
        self.assertEqual(run["status"], "running")

    def test_only_while_the_run_is_running_and_the_claim_is_claimed(self):
        token, run_id = self.attempt("op-handed")
        self.hand_over(token)
        self.assertFalse(self.ready(token, run_id), "after the hand-over the claim is not waiting for the student")
        self.assertEqual(self.run_row(run_id)["plan_hash"], "", "nothing was written to the run either")
        token, run_id = self.attempt("op-done")
        apply_runs.finish_run(self.conn, run_id, outcome="failed")
        self.assertFalse(self.ready(token, run_id))
        self.assertNotIn("waiting", json.loads(self.claim_row(token)["detail_json"]), "nor to the claim")
        self.assertFalse(self.ready("no-such-token", run_id))
        self.assertFalse(self.ready(token, run_id, user_id="someone-else"))


class RecoveryTests(DataCase):
    def test_a_stale_clicking_claim_is_unconfirmed_with_its_event_in_one_transaction_and_waiting_cleared(self):
        token, run_id = self.attempt()
        with self.conn:
            self.conn.execute("UPDATE application_submit_claims SET detail_json=? WHERE token=?", (json.dumps({"waiting": "student", "keep": 2}), token))
        self.hand_over(token)
        apply_claims.forget(token)
        counts = apply_runs.recover_stale(self.conn, self.at(minutes=10))
        self.assertEqual(counts["unconfirmed"], 1)
        row = self.claim_row(token)
        self.assertEqual((row["state"], row["after_click"]), ("unconfirmed", 1))
        self.assertEqual(json.loads(row["detail_json"]), {"waiting": "", "keep": 2})
        event = next(json.loads(item["detail_json"]) for item in self.conn.execute(
            "SELECT detail_json FROM application_events WHERE event_type='apply_agent_unconfirmed'").fetchall())
        self.assertEqual((event["run_id"], event["mode"], event["state"], event["by"]), (run_id, "handoff", "unconfirmed", "recovery"))
        self.assertEqual(event["source"], "apply_agent:watch", "a recovery is the app's own act, never the student's")
        self.assertEqual(self.run_row(run_id)["outcome"], "unconfirmed", "the run it belonged to is finished as may have been sent")
        self.assertEqual(sum(1 for name in self.event_names(token) if name == "apply_agent_unconfirmed"), 1, "one event, not one per branch")

    def test_a_stale_claimed_claim_is_failed_nothing_sent_and_has_no_unconfirmed_event(self):
        token, run_id = self.attempt()
        with self.conn:
            self.conn.execute("UPDATE application_submit_claims SET detail_json=? WHERE token=?", (json.dumps({"waiting": "student"}), token))
        apply_claims.forget(token)
        apply_runs.recover_stale(self.conn, self.at(minutes=10))
        row = self.claim_row(token)
        self.assertEqual((row["state"], row["after_click"], row["note"]), ("failed", 0, "The app stopped before handing your application to Greenhouse. Nothing was sent."))
        self.assertEqual(json.loads(row["detail_json"])["waiting"], "")
        self.assertNotIn("apply_agent_unconfirmed", self.event_names(token))
        self.assertEqual(self.run_row(run_id)["outcome"], "failed")


class CardAndQueueTests(DataCase):
    def test_a_held_claimed_claim_is_filling_then_your_turn_and_one_nobody_holds_is_stopped(self):
        token, _run = self.attempt()
        card = apply_watch.claim_card(self.conn, USER, token)
        self.assertEqual((card["status"], card["state"], card["can_resolve"]), ("filling", "claimed", False))
        with self.conn:
            self.conn.execute("UPDATE application_submit_claims SET detail_json=? WHERE token=?", (json.dumps({"waiting": "student"}), token))
        self.assertEqual(apply_watch.claim_card(self.conn, USER, token)["status"], "your_turn")
        found = apply_watch.card_states(self.conn, USER)
        shown = found[self.claim_row(token)["application_id"]]
        self.assertEqual((shown["status"], shown["run_id"]), ("your_turn", _run), "the card shows it too, with the run to open")
        apply_claims.forget(token)
        self.assertEqual(apply_watch.claim_card(self.conn, USER, token)["status"], "stopped")
        self.assertIsNone(apply_watch.claim_card(self.conn, USER, "no-such-token"))
        self.assertIsNone(apply_watch.claim_card(self.conn, "someone-else", token))

    def test_a_claim_another_server_holds_is_held_while_its_heartbeat_is_fresh(self):
        token = self.raw_claim(state="claimed", mode="handoff", instance="another-server", detail={"waiting": "student"})
        self.assertEqual(apply_watch.claim_card(self.conn, USER, token)["status"], "your_turn")
        with self.conn:
            self.conn.execute("UPDATE application_submit_claims SET heartbeat_at='2020-01-01T00:00:00+00:00' WHERE token=?", (token,))
        self.assertEqual(apply_watch.claim_card(self.conn, USER, token)["status"], "stopped")

    def test_a_claim_nobody_holds_whose_settlement_was_decided_as_maybe_sent_is_not_a_stopped_attempt(self):
        token, _run = self.attempt()
        apply_claims.forget(token)
        self.assertEqual(apply_watch.claim_card(self.conn, USER, token)["status"], "stopped")
        apply_claims.mark_unconfirmed(token, WINDOW_UNCONFIRMED)
        self.addCleanup(apply_claims.take_unconfirmed, token)
        card = apply_watch.claim_card(self.conn, USER, token)
        self.assertEqual((card["status"], card["note"], card["can_resolve"]), ("may_have_been_sent", WINDOW_UNCONFIRMED, False))
        found = apply_watch.card_states(self.conn, USER)[self.claim_row(token)["application_id"]]
        self.assertEqual(found["status"], "may_have_been_sent")
        self.assertEqual(apply_claims.UNCONFIRMED_UNWRITTEN[token], WINDOW_UNCONFIRMED, "reading the card does not use the decision up: recovery does")
        apply_runs.recover_stale(self.conn)
        settled = self.claim_row(token)
        self.assertEqual((settled["state"], settled["after_click"], settled["note"]), ("unconfirmed", 1, WINDOW_UNCONFIRMED))

    def test_the_card_says_whether_the_claim_was_ever_handed_over(self):
        replaced = self.raw_claim(state="released", mode="handoff", after_click=0, handed_over_at=None)
        said_no = self.raw_claim(state="released", mode="handoff", handed_over_at=utc_now(), after_click=1)
        with self.conn:
            self.conn.execute("UPDATE application_submit_claims SET resolved_by='student' WHERE token IN (?, ?)", (replaced, said_no))
        self.assertFalse(apply_watch.claim_card(self.conn, USER, replaced)["handed_over"], "released by a later start, not by the student's word")
        self.assertTrue(apply_watch.claim_card(self.conn, USER, said_no)["handed_over"])

    def test_the_card_carries_the_run_id_and_the_mark_as_applied_question(self):
        token, run_id = self.attempt()
        self.assertEqual(apply_watch.claim_card(self.conn, USER, token)["run_id"], run_id)

    def test_a_claim_the_student_stopped_gives_no_urgent_row_and_a_timeout_still_does(self):
        stopped = self.raw_claim(state="needs_you", mode="handoff", after_click=0, detail={"stopped_by": "student"})
        timed_out = self.raw_claim(state="needs_you", mode="handoff", after_click=0, detail={"waiting": ""})
        maybe_sent = self.raw_claim(state="needs_you", mode="handoff", after_click=1, handed_over_at=utc_now(), detail={"stopped_by": "student"})
        items = {item["key"] for item in urgent.urgent_queue(self.conn, user_id=USER, now=self.at())["items"] if item["kind"].startswith("apply_")}
        self.assertNotIn(f"apply_needs_you:{stopped}", items)
        self.assertIn(f"apply_needs_you:{timed_out}", items)
        self.assertIn(f"apply_needs_you:{maybe_sent}", items, "an attempt that may have been sent always asks, whatever its detail says")

    def test_an_open_window_is_listed_as_in_flight_and_a_pause_says_it_does_not_stop_it(self):
        token, _run = self.attempt()
        self.assertEqual(automation.in_flight(self.conn, USER), [], "still filling: nothing to say")
        with self.conn:
            self.conn.execute("UPDATE application_submit_claims SET detail_json=? WHERE token=?", (json.dumps({"waiting": "student"}), token))
        [item] = automation.in_flight(self.conn, USER)
        self.assertEqual((item["source"], item["action"], item["kind"], item["target_id"]), ("apply_claim", "window", "handoff", self.claim_row(token)["application_id"]))
        self.assertEqual(item["label"], "A Finish in browser window is open. Pausing doesn't stop your own Submit; press Stop to end it.")
        paused = automation.set_paused(self.conn, USER, True)
        self.assertEqual([entry["action"] for entry in paused["in_flight"]], ["window"])
        apply_claims.forget(token)
        self.assertEqual(automation.in_flight(self.conn, USER), [], "a window nobody holds any more is not open")

    def test_an_application_handed_over_is_in_flight_as_before(self):
        token, _run = self.attempt()
        self.hand_over(token)
        self.assertEqual([item["action"] for item in automation.in_flight(self.conn, USER)], ["application"])


class StageTests(DataCase):
    """The forward-only stage write of 6.15 (12.3), for a Finish in browser submission."""

    def submit(self, name, policy):
        token, run_id = self.attempt(name, stage_policy=policy)
        self.hand_over(token)
        result = apply_runs.record_result(
            self.conn, user_id=USER, token=token, run_id=run_id, state="submitted", outcome="submitted", confirmation_seen=True,
            expected_states=("clicking",), notify=False,
        )
        return token, result

    def test_an_ask_claim_never_moves_the_stage_by_itself_and_the_students_mark_does(self):
        token, result = self.submit("op-ask", "ask")
        self.assertEqual((result["settled"], result["stage_recorded"]), (True, False))
        self.assertEqual(self.stage("op-ask")[0], "applying")
        self.assertFalse(apply_runs.record_stage(self.conn, token, user_id=USER), "record_stage without a source does nothing for 'ask'")
        self.assertEqual(self.claim_row(token)["stage_recorded"], 0)
        card = apply_watch.mark_applied(self.conn, token, user_id=USER)
        self.assertEqual(self.stage("op-ask")[0], "applied")
        self.assertTrue(card["stage_recorded"])

    def test_a_stage_the_student_set_meanwhile_wins_over_the_record_and_over_mark_as_applied(self):
        token, result = self.submit("op-rec", "record")
        self.assertEqual((self.stage("op-rec")[0], result["stage_recorded"]), ("applied", True))
        token, _result = self.submit("op-late", "ask")
        with self.conn:
            self.conn.execute("UPDATE applications SET stage='rejected' WHERE opportunity_id='op-late'")
        apply_watch.mark_applied(self.conn, token, user_id=USER)
        self.assertEqual(self.stage("op-late")[0], "rejected", "forward only: never back to applied")
        self.assertEqual(self.claim_row(token)["stage_recorded"], 1)

    def test_a_record_claim_does_not_overwrite_an_interview(self):
        token, run_id = self.attempt("op-int", stage_policy="record")
        self.hand_over(token)
        with self.conn:
            self.conn.execute("UPDATE applications SET stage='interview' WHERE opportunity_id='op-int'")
        result = apply_runs.record_result(
            self.conn, user_id=USER, token=token, run_id=run_id, state="submitted", outcome="submitted", confirmation_seen=True, notify=False,
        )
        self.assertEqual((self.stage("op-int")[0], result["stage_recorded"]), ("interview", True))


class PreviewTests(HandoffCase):
    """What the student sees beside each field (10.4): today's source for each stored plan entry, never stored on the run."""

    LONG = "I build small robot arms and I would like to learn how a whole team ships hardware and software together. " * 3

    def setUp(self):
        super().setUp()
        self.key = apply_policy.mac_key(self.apply_root)
        preparation.save_answer(self.conn, "Why do you want to work at Example Robotics?", "I build robot arms", "Example Robotics, Inc.", [], user_id=USER)

    def rehearse(self):
        return self.finished(self.start(kind="rehearsal"))

    def values(self, row):
        return apply_policy.preview_values(self.conn, USER, row, key=self.key, storage_root=self.root / "resumes")

    def test_a_rehearsal_shows_todays_value_for_what_it_would_fill(self):
        values = self.values(self.rehearse())
        self.assertEqual(values["first_name"], {"text": "Sam", "changed": False, "available": True, "shown": True})
        self.assertEqual(values["email"]["text"], "sam.rivera@example.test")
        self.assertEqual(values["question_4000000101"], {"text": "I build robot arms", "changed": False, "available": True, "shown": True})
        self.assertEqual(values["resume"]["text"], "Resume.pdf", "a file is shown by its original name")
        self.assertNotIn("question_4000000103", values, "no answer: nothing to show")
        self.assertNotIn("question_4000000102", values, "an optional field left blank has no value")

    def test_a_long_answer_is_cut_and_a_ticked_box_says_ticked(self):
        with self.conn:
            self.conn.execute("UPDATE answer_library SET answer=?", (self.LONG,))
        text = self.values(self.rehearse())["question_4000000101"]["text"]
        self.assertLessEqual(len(text), 121)
        self.assertTrue(text.endswith("…") and self.LONG.startswith(text[:-1]) and len(text) > 100)
        self.assertEqual(apply_policy._preview_text(True), "Ticked")
        self.assertEqual(apply_policy._preview_text(["A", "B"]), "A; B")

    def test_editing_the_saved_answer_or_the_profile_changes_the_value_and_says_so(self):
        row = self.rehearse()
        with self.conn:
            self.conn.execute("UPDATE answer_library SET answer='A different answer'")
        values = self.values(row)
        self.assertEqual(values["question_4000000101"], {"text": "A different answer", "changed": True, "available": True, "shown": True})
        self.assertFalse(values["first_name"]["changed"])
        from opportunity_app.student.profile import update_profile
        update_profile(self.conn, {"name_parts": {"first": "Alex", "last": "Rivera", "preferred": ""}}, ["name_parts"], user_id=USER)
        values = self.values(row)
        self.assertEqual((values["first_name"]["text"], values["first_name"]["changed"]), ("Alex", True))
        self.assertFalse(values["last_name"]["changed"])

    def test_a_new_answer_row_for_the_same_question_counts_as_a_change(self):
        row = self.rehearse()
        newer = preparation.save_answer(self.conn, "Why do you want to work at Example Robotics?", "I build robot arms", "Example Robotics, Inc.", [], user_id=USER)
        self.assertTrue(newer["id"])
        values = self.values(row)
        self.assertTrue(values["question_4000000101"]["changed"], "a different row now answers it, even with the same words")
        with self.conn:
            self.conn.execute("UPDATE answer_library SET answer='Something else' WHERE id=?", (newer["id"],))
        values = self.values(row)
        self.assertEqual(values["question_4000000101"], {"text": "", "changed": True, "available": False, "shown": False}, "two answers now: nothing answers it")

    def test_a_deleted_answer_is_no_longer_available(self):
        row = self.rehearse()
        with self.conn:
            self.conn.execute("DELETE FROM answer_library")
        self.assertEqual(self.values(row)["question_4000000101"], {"text": "", "changed": True, "available": False, "shown": False})

    def test_a_newer_resume_version_is_a_change(self):
        row = self.rehearse()
        data = b"%PDF-1.4 another fictional resume"
        stamp = (apply_runs.at_utc(None) + timedelta(minutes=5)).isoformat(timespec="microseconds")
        (self.root / "resumes" / "resume-file-2.pdf").write_bytes(data)
        with self.conn:
            self.conn.execute(
                "INSERT INTO resume_files(id, user_id, original_name, media_type, byte_size, sha256, storage_path, created_at) VALUES('resume-file-2', ?, 'Newer.pdf', "
                "'application/pdf', ?, ?, 'resume-file-2.pdf', ?)", (USER, len(data), hashlib.sha256(data).hexdigest(), stamp))
            self.conn.execute(
                "INSERT INTO resume_versions(id, resume_file_id, user_id, extracted_text, status, created_at, confirmed_at) VALUES('resume-2', 'resume-file-2', ?, 't', 'confirmed', ?, ?)",
                (USER, stamp, stamp))
        values = self.values(row)
        self.assertEqual((values["resume"]["text"], values["resume"]["changed"]), ("Newer.pdf", True))

    def sponsorship(self):
        from opportunity_app.apply import sensitive as apply_sensitive
        apply_sensitive.set_allowed_categories(self.conn, USER, ["sponsorship"])
        apply_sensitive.add_entry(
            self.conn, USER, category="sponsorship", question="Will you now or in the future require sponsorship for employment visa status?",
            answer="No", answer_kind="option", company="", links=(), consent=True,
        )

    def test_a_sensitive_value_is_in_the_values_response_and_never_in_the_run_view(self):
        self.sponsorship()
        row = self.finished(self.handoff())
        values = self.values(row)
        self.assertEqual(values["question_4000000106"], {"text": "No", "changed": False, "available": True, "shown": True})
        view = apply_runner.run_view(self.conn, row)
        entry = next(item for item in view["fields"] if item["key"] == "question_4000000106")
        self.assertEqual((entry["disposition_text"], entry["sensitive"], entry["source_text"]), ("Filled from your stored answer", True, "Your stored answer"))
        self.assertTrue(all(not ({"text", "value", "answer"} & set(item)) for item in view["fields"]), "no field of the view carries a value")

    def test_a_handoff_shows_only_what_it_provably_filled(self):
        row = self.finished(self.handoff())
        plan = {item["key"]: item for item in json.loads(row["plan_json"])}
        values = self.values(row)
        self.assertEqual(values["first_name"], {"text": "Sam", "changed": False, "available": True, "shown": True})
        for key, entry in plan.items():
            if entry["disposition"] in ("left_for_you", "blank"):
                self.assertNotIn(key, values, f"{key} was not filled, so it has no value")
        from opportunity_app.student.profile import update_profile
        update_profile(self.conn, {"name_parts": {"first": "Alex", "last": "Rivera", "preferred": ""}}, ["name_parts"], user_id=USER)
        changed = self.values(row)["first_name"]
        self.assertEqual(changed, {"text": "", "changed": True, "available": True, "shown": False}, "what was sent is not stored, so it is not claimed")
        self.assertEqual(self.values(row)["last_name"]["text"], "Rivera")

    def test_a_cover_letter_entry_is_worded_like_any_other_file(self):
        entry = {"key": "cover_letter", "question": "Cover Letter", "control": "file", "required": True, "options": [], "sensitive": None,
                 "disposition": "left_for_you", "source": {"kind": "none", "ref": "", "company": "", "reusable": False, "links": []},
                 "value_mac": "", "file_sha256": "", "problem": "", "note": ""}
        self.assertEqual(apply_runner._disposition_text(entry, kind="handoff"), "Left for you")
        filled = {**entry, "disposition": "fill", "source": {"kind": "cover_letter", "ref": "d@1", "links": []}, "file_sha256": "x"}
        self.assertEqual(apply_runner._disposition_text(filled, kind="handoff"), "Filled in the window", "the agent moves a letter it did not attach to left_for_you before it shows the plan")
        self.assertEqual(apply_runner._disposition_text(filled, kind="rehearsal"), "Not attached in this rehearsal")
        self.assertEqual(apply_runner._disposition_text(filled, frozenset({"cover_letter"}), kind="rehearsal"), "Filled in the rehearsal")
        deferred = {**filled, "disposition": "deferred"}
        self.assertEqual(apply_runner._disposition_text(deferred, kind="rehearsal"), "Not attached: this board uploads files as soon as they are attached")

    def test_the_source_of_a_cover_letter_names_its_version(self):
        filled = {"key": "cover_letter", "source": {"kind": "cover_letter", "ref": "document-abc@3", "links": []}}
        self.assertEqual(apply_runner._source_text(filled), "Approved cover letter, version 3")
        self.assertEqual(apply_runner._source_text({"source": {"kind": "cover_letter", "ref": "", "links": []}}), "Your approved cover letter")

    def test_the_view_says_filled_in_the_window_where_the_old_wording_was_wrong_under_the_students_pressing_submit(self):
        row = self.rehearse()
        view = apply_runner.run_view(self.conn, row)
        self.assertNotIn("only when you submit", view["measured"] + json.dumps(view["fields"]))
        self.assertIn("if you choose Finish in browser, before you press Submit application", view["measured"])

    def test_links_and_notes_ride_with_every_field_of_every_kind(self):
        row = self.rehearse()
        plan = json.loads(row["plan_json"])
        plan[0]["source"]["links"] = ["https://example-robotics.test/privacy"]
        plan[1]["note"] = "Left blank: nothing saved answers it"
        with self.conn:
            self.conn.execute("UPDATE apply_runs SET plan_json=? WHERE id=?", (json.dumps(plan), row["id"]))
        view = apply_runner.run_view(self.conn, self.run_row(row["id"]))
        by_key = {item["key"]: item for item in view["fields"]}
        self.assertEqual(by_key[plan[0]["key"]]["links"], ["https://example-robotics.test/privacy"])
        self.assertEqual(by_key[plan[1]["key"]]["note"], "Left blank: nothing saved answers it")
        self.assertEqual(view["kind"], "rehearsal")

    def test_page_defaults_are_listed_by_question_for_a_rehearsal_too(self):
        row = self.rehearse()
        key = json.loads(row["plan_json"])[0]["key"]
        question = json.loads(row["plan_json"])[0]["question"]
        evidence = json.loads(row["evidence_json"])
        evidence["page_defaults"] = [key]
        with self.conn:
            self.conn.execute("UPDATE apply_runs SET evidence_json=? WHERE id=?", (json.dumps(evidence), row["id"]))
        view = apply_runner.run_view(self.conn, self.run_row(row["id"]))
        self.assertEqual(view["page_defaults"], [{"key": key, "question": question}])

    def test_run_views_lists_handoffs_and_the_list_route_helper_filters_by_kind(self):
        rehearsal = self.rehearse()["id"]
        handoff = self.finished(self.handoff())["id"]
        self.assertEqual([item["id"] for item in apply_runner.run_views(self.conn, USER, ACME)], [handoff, rehearsal])
        self.assertEqual([item["id"] for item in apply_runner.run_views(self.conn, USER, ACME, kind="handoff")], [handoff])
        self.assertEqual([item["id"] for item in apply_runner.run_views(self.conn, USER, ACME, kind="rehearsal")], [rehearsal])

    def test_a_role_opened_during_a_finish_in_browser_run_is_never_said_to_be_submitted(self):
        run_id = self.handoff(handoff_factory(wait=30))
        self.turn(run_id)
        result = apply_preflight.check(self.conn, USER, ACME, client=self.schema, resume_root=self.root / "resumes")
        self.assertEqual((result["status"], result["message"]), ("failed", apply_runs.HANDOFF_OPEN))
        self.assertNotIn("submitted", result["message"], "nothing has been handed over: the claim is only 'claimed'")
        self.assertEqual(result["eligibility"]["handoff"]["reason"], apply_runs.HANDOFF_OPEN)
        # Once the student's Submit was handed over, the either-or sentence is the honest one again.
        with self.conn:
            self.conn.execute("UPDATE application_submit_claims SET state='clicking', after_click=1, handed_over_at=? WHERE run_id=?", (utc_now(), run_id))
        again = apply_preflight.check(self.conn, USER, ACME, client=self.schema, resume_root=self.root / "resumes")
        self.assertEqual(again["message"], apply_runs.LIVE_APPLICATION)
        with self.conn:
            self.conn.execute("UPDATE application_submit_claims SET state='claimed', after_click=0, handed_over_at=NULL WHERE run_id=?", (run_id,))
        self.assertTrue(apply_runs.request_cancel(self.conn, self.claim_of(run_id)["token"], user_id=USER))
        self.assertTrue(self.runner.cancel(run_id))
        self.finished(run_id)

    def test_the_ticks_carry_the_asks_words_and_the_company_limit_its_date(self):
        run_id = self.handoff()
        self.finished(run_id)
        token = self.claim_of(run_id)["token"]
        with self.conn:
            self.conn.execute("UPDATE application_submit_claims SET state='unconfirmed', resolved_by='' WHERE token=?", (token,))
        apply_watch.resolve_by_student(self.conn, token, user_id=USER, went_through=False)
        old = (apply_runs.at_utc(None) - timedelta(days=3)).isoformat(timespec="microseconds")
        with self.conn:
            self.conn.execute("UPDATE application_submit_claims SET handed_over_at=? WHERE token=?", (old, token))
        result = apply_preflight.check(self.conn, USER, ACME, client=self.schema, resume_root=self.root / "resumes")
        handoff = result["eligibility"]["handoff"]
        asks = {item["code"]: item["message"] for item in result["asks"]}
        self.assertEqual([item["code"] for item in handoff["ticks"]], ["released_job", "company_limit"])
        self.assertEqual(handoff["ticks"][0]["label"], asks["released_job"])
        label = handoff["ticks"][1]["label"]
        self.assertTrue(label.startswith("I know Apply for me handed an application to Example Robotics, Inc. to Greenhouse on "), label)
        self.assertRegex(label, r"on [A-Z][a-z]+ \d{1,2} \(it may not have gone through\)\. Apply anyway\.$")
        self.assertNotIn("I applied", label, "the claim is released: the student said it did not go through, so the box never says they applied")
        self.assertNotIn("ticks", result["eligibility"]["submit"])
        self.assertNotIn("ticks", result["eligibility"]["rehearse"])


class PauseTests(HandoffCase):
    def test_a_pause_during_the_turn_lists_the_window_and_the_reader_still_answers(self):
        reader = FakeReader()
        runner = ApplyRunner(cancel_grace_s=GRACE, security_code_reader=reader)
        self.addCleanup(runner.shutdown, 30)
        self.runner = runner
        run_id = self.handoff(handoff_factory("security_code", wait=4.0), runner=runner)
        self.turn(run_id)
        paused = automation.set_paused(self.conn, USER, True)
        self.assertEqual([item["action"] for item in paused["in_flight"]], ["window"])
        row = self.finished(run_id, runner)
        self.assertEqual(self.claim_of(run_id)["state"], "submitted", "a pause does not stop the student's own Submit")
        self.assertEqual(len(reader.asked), 1, "the reader ran under the pause: it is part of a run the student started")
        self.assertEqual(json.loads(row["evidence_json"])["security_code"]["typed"], True)


class HandoffApiCase(api_tests.RunApiCase):
    """Finish in browser over HTTP, with the canned handoff: no browser, no network."""

    def setUp(self):
        super().setUp()
        with self.conn:
            self.conn.execute("UPDATE opportunities SET company='Example Robotics, Inc.', title='Robotics Intern' WHERE id=?", (ACME,))
        self.factory.handoff = {"wait": 0.4, "outcome": "submitted"}
        self.addCleanup(self.wait_for_helpers)

    def wait_for_helpers(self):
        wait_until(lambda: not any(t.name.startswith("apply-security-code") and t.is_alive() for t in threading.enumerate()), 20)

    def handoff(self, body=None, opportunity_id=ACME):
        return self.send("POST", f"{self.BASE}/opportunities/{opportunity_id}/handoffs", {} if body is None else body)

    def claim(self):
        row = self.conn.execute("SELECT * FROM application_submit_claims ORDER BY created_at DESC").fetchone()
        return None if row is None else dict(row)

    def turn(self, run_id):
        self.assertTrue(wait_until(lambda: self.get(f"{self.BASE}/runs/{run_id}").json()["phase"] == "your_turn"), "the turn never began")


class HandoffStartTests(HandoffApiCase):
    def test_finish_in_browser_is_accepted_runs_and_leaves_a_finished_view_with_its_claim(self):
        response = self.handoff()
        self.assertEqual(response.status_code, 202, response.text)
        started = response.json()
        self.assertEqual(set(started), api_tests.RUN_KEYS)
        self.assertEqual((started["kind"], started["opportunity_id"], started["can_cancel"], started["status"]), ("handoff", ACME, True, "running"))
        self.assertEqual(started["claim"]["state"], "claimed")
        view = self.finished(started["id"])
        self.assertEqual(set(view), api_tests.RUN_KEYS)
        self.assertEqual((view["status"], view["outcome"], view["can_cancel"], view["can_front"], view["phase"]), ("finished", "submitted", False, False, ""))
        self.assertEqual(view["summary"], "Greenhouse showed its confirmation page. Mark as applied?")
        self.assertEqual((view["claim"]["state"], view["claim"]["ask_mark_applied"], view["claim"]["status"]), ("submitted", True, "not_watched"))
        self.assertEqual(self.claim()["stage_policy"], "ask")
        stage = "SELECT stage FROM applications WHERE opportunity_id=?"
        self.assertEqual(self.conn.execute(stage, (ACME,)).fetchone()[0], "applying", "the tracker waits for the student's Mark as applied")
        marked = self.send("POST", f"{self.BASE}/claims/{view['claim']['token']}/mark-applied")
        self.assertEqual(marked.status_code, 200, marked.text)
        self.assertEqual(self.conn.execute(stage, (ACME,)).fetchone()[0], "applied")

    def test_it_needs_the_students_own_browser_not_a_token_a_script_holds(self):
        url = f"{self.BASE}/opportunities/{ACME}/handoffs"
        before = self.counts("apply_runs", "application_submit_claims", "applications", "application_events")
        stage = self.conn.execute("SELECT stage FROM applications WHERE opportunity_id=?", (ACME,)).fetchall()
        bearer = self.client.post(url, headers=AUTH, json={})
        self.assertEqual(bearer.status_code, 403, bearer.text)
        self.assertIn("browser", bearer.json()["detail"])
        both = self.browser.post(url, headers={**AUTH, **self.csrf}, json={})
        self.assertEqual(both.status_code, 403, "a cookie next to the header does not make a script the student")
        self.assertEqual(self.client.post(url, json={}).status_code, 401)
        bare = self.browser.post(url, json={})
        self.assertEqual((bare.status_code, bare.json()["detail"]), (403, "CSRF validation failed"), "even with no Origin header")
        wrong = self.browser.post(url, json={}, headers={"X-CSRF-Token": "not-the-token"})
        self.assertEqual(wrong.status_code, 403)
        # Counted before the refused requests, so a refused start that wrote a run, a claim, an application or an event is seen.
        self.assertEqual(self.counts("apply_runs", "application_submit_claims", "applications", "application_events"), before)
        self.assertEqual((before["apply_runs"], before["application_submit_claims"]), (0, 0))
        self.assertEqual(self.conn.execute("SELECT stage FROM applications WHERE opportunity_id=?", (ACME,)).fetchall(), stage, "a stage moved")

    def test_every_new_route_refuses_the_owner_token_and_the_screenshots_too(self):
        run_id = self.handoff().json()["id"]
        self.finished(run_id)
        for method, path, body in (
            ("POST", f"/runs/{run_id}/front", None), ("GET", f"/runs/{run_id}/values", None), ("GET", f"/runs/{run_id}/screenshots/0", None),
            ("POST", f"/runs/{run_id}/cancel", None),
        ):
            bearer = self.client.request(method, f"{self.BASE}{path}", headers=AUTH, json=body)
            self.assertEqual(bearer.status_code, 403, f"{method} {path}: {bearer.text}")
            self.assertIn("browser", bearer.json()["detail"])
            self.assertEqual(self.client.request(method, f"{self.BASE}{path}", json=body).status_code, 401)
        for method, path in (("POST", f"/runs/{run_id}/front"), ("POST", f"/runs/{run_id}/cancel")):
            bare = self.browser.request(method, f"{self.BASE}{path}")
            self.assertEqual((bare.status_code, bare.json()["detail"]), (403, "CSRF validation failed"), path)

    def test_the_guards_answer_in_the_same_order_as_a_rehearsals(self):
        before = self.counts("apply_runs", "application_submit_claims", "application_events")
        self.client.put("/api/v1/automation/settings", headers=AUTH, json={"modes": {"apply_agent": "off"}})
        response = self.handoff()
        self.assertEqual((response.status_code, response.json()["detail"]), (409, "Apply for me is off. Turn it on under Automation"))
        self.client.put("/api/v1/automation/settings", headers=AUTH, json={"modes": {"apply_agent": "on"}})
        self.factory.missing = "Could not start the browser. Install Playwright and Chromium: python -m playwright install chromium"
        response = self.handoff()
        self.assertEqual((response.status_code, response.json()["detail"]), (409, self.factory.missing))
        self.assertEqual(self.counts("apply_runs", "application_submit_claims", "application_events"), before)

    def test_a_second_start_while_one_runs_is_a_409_with_nothing_written(self):
        self.factory.handoff = {"wait": 30, "outcome": "submitted"}
        first = self.handoff().json()["id"]
        before = self.counts("apply_runs", "application_submit_claims", "application_events")
        for response in (self.handoff(), self.rehearse(), self.lookup()):
            self.assertEqual((response.status_code, response.json()["detail"]), (409, "Another application is being filled. Wait for it to finish."))
        self.assertEqual(self.counts("apply_runs", "application_submit_claims", "application_events"), before)
        self.send("POST", f"{self.BASE}/runs/{first}/cancel")
        self.finished(first)

    def test_a_posting_that_differs_answers_with_its_code_and_the_tick_lets_it_start(self):
        with self.conn:
            self.conn.execute("UPDATE opportunities SET company='Acme Robotics', title='Mechanical Engineering Intern' WHERE id=?", (ACME,))
        response = self.handoff()
        self.assertEqual(response.status_code, 409, response.text)
        detail = response.json()["detail"]
        self.assertEqual((detail["code"], detail["ask"]), ("posting", False))
        self.assertTrue(detail["message"].startswith("Check the posting first."), detail)
        self.assertEqual(self.counts("application_submit_claims")["application_submit_claims"], 0)
        accepted = self.handoff({"posting_confirmed": True})
        self.assertEqual(accepted.status_code, 202, accepted.text)
        self.finished(accepted.json()["id"])

    def test_a_refused_tick_names_its_code_and_the_ticked_start_records_it(self):
        first = self.handoff().json()["id"]
        self.finished(first)
        token = self.claim()["token"]
        with self.conn:
            self.conn.execute("UPDATE application_submit_claims SET state='unconfirmed' WHERE token=?", (token,))
        self.send("POST", f"{self.BASE}/claims/{token}/resolve", {"went_through": False})
        old = "2020-01-01T00:00:00.000000+00:00"
        with self.conn:
            self.conn.execute("UPDATE application_submit_claims SET handed_over_at=?", (old,))
        refused = self.handoff()
        self.assertEqual(refused.status_code, 409, refused.text)
        detail = refused.json()["detail"]
        self.assertEqual((detail["code"], detail["ask"]), ("released_job", True))
        self.assertEqual(self.counts("application_submit_claims")["application_submit_claims"], 1)
        accepted = self.handoff({"acknowledged": ["released_job"]})
        self.assertEqual(accepted.status_code, 202, accepted.text)
        self.finished(accepted.json()["id"])
        self.assertEqual(json.loads(self.claim()["detail_json"])["acknowledged"], ["released_job"])

    def test_the_body_is_validated(self):
        for body in ({"acknowledged": ["everything"]}, {"acknowledged": ["company_limit"] * 6}, {"posting_confirmed": "maybe"}, {"acknowledged": "company_limit"}):
            self.assertEqual(self.handoff(body).status_code, 422, body)
        self.assertEqual(self.counts("apply_runs")["apply_runs"], 0)

    def test_a_role_that_is_not_greenhouse_a_closed_posting_and_an_unknown_role(self):
        response = self.handoff(opportunity_id="job-b")
        self.assertEqual((response.status_code, response.json()["detail"]), (409, "Apply for me works with Greenhouse and Lever postings only, for now"))
        self.schema.closed = True
        self.assertEqual(self.handoff().json()["detail"], "The app couldn't find this posting on Greenhouse. It may be closed")
        self.assertEqual(self.handoff(opportunity_id="no-such-role").status_code, 404)
        self.assertEqual(self.counts("apply_runs", "application_submit_claims", "applications", "application_events")["apply_runs"], 0)

    def test_no_socket_is_opened_by_a_canned_handoff(self):
        boom = AssertionError("a run reached for the network")
        with mock.patch("socket.socket.connect", side_effect=boom), mock.patch("socket.create_connection", side_effect=boom), \
                mock.patch("urllib.request.urlopen", side_effect=boom):
            self.assertEqual(self.finished(self.handoff().json()["id"])["outcome"], "submitted")


class HandoffNoFactoryTests(api_tests.ApplyApiCase):
    with_factories = False

    def test_without_factories_and_without_a_folder_for_pictures_it_answers_503_at_once(self):
        self.greenhouse_role()
        browser = self.enterContext(TestClient(self.app))
        signed = browser.post("/api/v1/session", json={"token": api_tests.TOKEN})
        self.assertEqual(signed.status_code, 200, signed.text)
        csrf = {"X-CSRF-Token": browser.cookies.get("pipeline_csrf")}
        response = browser.post(f"/api/v1/apply-agent/opportunities/{ACME}/handoffs", headers=csrf, json={})
        self.assertEqual((response.status_code, response.json()["detail"]), (503, apply_runs.NOT_HERE))
        self.assertEqual(self.schema.calls, [], "no request was made")
        self.addCleanup(apply_runs.configure_agent_factory, None)
        app = create_app(db_path=self.path, access_token=api_tests.TOKEN, static_dir=api_tests.STATIC_DIR, resume_storage=self.root / "resumes",
                         apply_schema_client_factory=lambda: self.schema, apply_agent_factory=FakeApplyAgentFactory())
        with TestClient(app) as client:
            client.post("/api/v1/session", json={"token": api_tests.TOKEN})
            csrf = {"X-CSRF-Token": client.cookies.get("pipeline_csrf")}
            for method, path in (("POST", f"/opportunities/{ACME}/handoffs"),):
                response = client.request(method, f"/api/v1/apply-agent{path}", headers=csrf, json={})
                self.assertEqual((response.status_code, response.json()["detail"]), (503, apply_runs.NOT_HERE))
        self.assertEqual(self.schema.calls, [])


class HandoffReadTests(HandoffApiCase):
    def test_the_list_filters_by_kind_and_finds_one_run_by_its_id(self):
        rehearsal = self.rehearse().json()["id"]
        self.finished(rehearsal)
        handoff = self.handoff().json()["id"]
        self.finished(handoff)
        listed = self.get(f"{self.BASE}/opportunities/{ACME}/runs").json()
        self.assertEqual(([item["id"] for item in listed["runs"]], listed["busy"]), ([handoff, rehearsal], False))
        self.assertEqual([item["id"] for item in self.get(f"{self.BASE}/opportunities/{ACME}/runs?kind=handoff").json()["runs"]], [handoff])
        one = self.get(f"{self.BASE}/opportunities/{ACME}/runs?run_id={rehearsal}").json()
        self.assertEqual([item["id"] for item in one["runs"]], [rehearsal])
        self.assertEqual(self.get(f"{self.BASE}/opportunities/{ACME}/runs?run_id=run-{'0' * 32}").json()["runs"], [])
        other = self.other_students_run()
        self.assertEqual(self.get(f"{self.BASE}/opportunities/{ACME}/runs?run_id={other}").json()["runs"], [], "another student's run is never listed")
        self.assertEqual(self.get(f"{self.BASE}/opportunities/job-b/runs?run_id={handoff}").status_code, 200)
        self.assertEqual(self.get(f"{self.BASE}/opportunities/job-b/runs?run_id={handoff}").json()["runs"], [], "a run of another role is not this role's")

    def test_the_view_while_running_says_the_turn_the_left_list_and_how_long_the_window_stays(self):
        self.factory.handoff = {"wait": 30, "outcome": "submitted"}
        run_id = self.handoff().json()["id"]
        self.turn(run_id)
        view = self.get(f"{self.BASE}/runs/{run_id}").json()
        self.assertEqual((view["phase"], view["summary"], view["can_cancel"], view["can_front"]), ("your_turn", YOUR_TURN, True, True))
        self.assertTrue(view["left_for_you"] and view["handoff_until"])
        self.assertEqual(view["claim"]["status"], "your_turn")
        self.assertEqual(self.get(f"{self.BASE}/opportunities/{ACME}/runs").json()["busy"], True)
        self.send("POST", f"{self.BASE}/runs/{run_id}/cancel")
        self.finished(run_id)

    def test_the_applications_card_says_filling_and_then_your_turn(self):
        self.factory.handoff = {"wait": 30, "outcome": "submitted"}
        run_id = self.handoff().json()["id"]
        self.turn(run_id)
        cards = apply_watch.card_states(self.conn, USER)
        self.assertEqual([card["status"] for card in cards.values()], ["your_turn"])
        self.send("POST", f"{self.BASE}/runs/{run_id}/cancel")
        self.finished(run_id)
        self.assertEqual([card["status"] for card in apply_watch.card_states(self.conn, USER).values()], ["stopped"])

    def test_the_values_are_the_students_own_browsers_and_are_never_cached(self):
        run_id = self.rehearse().json()["id"]
        self.finished(run_id)
        url = f"{self.BASE}/runs/{run_id}/values"
        response = self.browser.get(url)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")
        values = response.json()["values"]
        self.assertEqual(values["first_name"], {"text": "Sam", "changed": False, "available": True, "shown": True})
        self.assertEqual(self.client.get(url).status_code, 401)
        self.assertEqual(self.get(url).status_code, 403)
        self.assertNotIn("Sam", json.dumps(self.get(f"{self.BASE}/runs/{run_id}").json()), "the run view holds no value")
        other = self.other_students_run()
        self.assertEqual(self.browser.get(f"{self.BASE}/runs/{other}/values").status_code, 404)
        self.assertEqual(self.browser.get(f"{self.BASE}/runs/run-{'0' * 32}/values").status_code, 404)

    def test_a_handoff_that_stopped_before_it_filled_anything_returns_no_values_and_says_nothing_was_filled(self):
        run_id = self.handoff().json()["id"]
        self.finished(run_id)
        # What the driver now records for a run that stopped before the fill: every field it never typed is "blank", with its reason.
        plan = json.loads(self.conn.execute("SELECT plan_json FROM apply_runs WHERE id=?", (run_id,)).fetchone()[0])
        self.assertTrue(any(entry["disposition"] == "fill" for entry in plan), "the canned run filled something to begin with")
        for entry in plan:
            if entry["disposition"] == "fill":
                entry["disposition"], entry["note"] = "blank", "The run stopped before the app filled and checked this field, so nothing was put in it"
        with self.conn:
            self.conn.execute("UPDATE apply_runs SET plan_json=?, outcome='needs_you' WHERE id=?", (json.dumps(plan), run_id))
        self.assertEqual(self.browser.get(f"{self.BASE}/runs/{run_id}/values").json()["values"], {}, "a field that was never filled has a value on the page")
        view = self.get(f"{self.BASE}/runs/{run_id}").json()
        texts = {item["disposition_text"] for item in view["fields"]}
        self.assertFalse(texts & {"Filled in the window", "Filled from your stored answer", "Ticked from your stored statement"}, texts)
        self.assertTrue(all(item["note"] for item in view["fields"] if item["disposition"] == "blank"))

    def test_the_values_of_a_handoff_are_shown_only_where_unchanged(self):
        run_id = self.handoff().json()["id"]
        self.finished(run_id)
        values = self.browser.get(f"{self.BASE}/runs/{run_id}/values").json()["values"]
        self.assertEqual(values["first_name"]["text"], "Sam")
        self.assertTrue(values["first_name"]["shown"])
        from opportunity_app.student.profile import update_profile
        update_profile(self.conn, {"name_parts": {"first": "Alex", "last": "Rivera", "preferred": ""}}, ["name_parts"], user_id=USER)
        changed = self.browser.get(f"{self.BASE}/runs/{run_id}/values").json()["values"]["first_name"]
        self.assertEqual(changed, {"text": "", "changed": True, "available": True, "shown": False})

    def test_the_values_route_needs_a_folder_for_the_key(self):
        self.addCleanup(apply_runs.configure_agent_factory, None)
        app = create_app(db_path=self.path, access_token=api_tests.TOKEN, static_dir=api_tests.STATIC_DIR, resume_storage=self.root / "resumes",
                         apply_schema_client_factory=lambda: self.schema, apply_agent_factory=FakeApplyAgentFactory())
        run_id = apply_runs.create_run(
            self.conn, user_id=USER, opportunity_id=ACME, kind="rehearsal", started_by="student", ats="greenhouse", adapter_version="greenhouse-1", board_token="b",
            page_url=JOB_URL, company="acme", deadline_seconds=300,
        )
        with TestClient(app) as client:
            client.post("/api/v1/session", json={"token": api_tests.TOKEN})
            response = client.get(f"{self.BASE}/runs/{run_id}/values")
            self.assertEqual((response.status_code, response.json()["detail"]), (503, apply_runs.NOT_HERE))

    def test_the_new_routes_and_the_changed_ones_are_in_the_openapi_schema(self):
        schema = self.client.get("/openapi.json").json()
        paths = schema["paths"]
        self.assertIn("202", paths["/api/v1/apply-agent/opportunities/{opportunity_id}/handoffs"]["post"]["responses"])
        self.assertIn("post", paths["/api/v1/apply-agent/runs/{run_id}/front"])
        self.assertIn("get", paths["/api/v1/apply-agent/runs/{run_id}/values"])
        kind = next(item for item in paths["/api/v1/apply-agent/opportunities/{opportunity_id}/runs"]["get"]["parameters"] if item["name"] == "kind")
        self.assertIn("handoff", json.dumps(kind["schema"]))
        self.assertIn("run_id", [item["name"] for item in paths["/api/v1/apply-agent/opportunities/{opportunity_id}/runs"]["get"]["parameters"]])


class HandoffStopTests(HandoffApiCase):
    def test_stop_during_the_turn_ends_the_run_with_a_stopped_claim_and_no_notice(self):
        self.factory.handoff = {"wait": 30, "outcome": "submitted"}
        run_id = self.handoff().json()["id"]
        self.turn(run_id)
        stopped = self.send("POST", f"{self.BASE}/runs/{run_id}/cancel")
        self.assertEqual(stopped.status_code, 200, stopped.text)
        self.assertEqual(set(stopped.json()), api_tests.RUN_KEYS)
        view = self.finished(run_id)
        self.assertEqual((view["status"], view["outcome"], view["summary"]), ("finished", "needs_you", HANDOFF_NOT_SUBMITTED))
        claim = self.claim()
        self.assertEqual((claim["state"], claim["after_click"], json.loads(claim["detail_json"])["stopped_by"]), ("needs_you", 0, "student"))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM automation_notices").fetchone()[0], 0)
        self.assertEqual([item for item in urgent.urgent_queue(self.conn, user_id=USER)["items"] if item["kind"].startswith("apply_")], [])
        again = self.send("POST", f"{self.BASE}/runs/{run_id}/cancel")
        self.assertEqual((again.status_code, again.json()["detail"]), (409, apply_runner.FINISHED_ALREADY))
        retry = self.handoff()
        self.assertEqual(retry.status_code, 202, "the next attempt releases the stopped one")
        self.send("POST", f"{self.BASE}/runs/{retry.json()['id']}/cancel")
        self.finished(retry.json()["id"])

    def test_stop_after_the_hand_over_is_refused_with_its_own_sentence(self):
        self.factory.handoff = {"wait": 0.2, "outcome": "hang_after_hand_over"}
        run_id = self.handoff().json()["id"]
        self.assertTrue(wait_until(lambda: (self.claim() or {}).get("state") == "clicking"), "the student pressed Submit")
        refused = self.send("POST", f"{self.BASE}/runs/{run_id}/cancel")
        self.assertEqual((refused.status_code, refused.json()["detail"]), (409, apply_runner.HANDED_OVER))
        self.assertEqual(self.claim()["state"], "clicking", "nothing changed")
        view = self.get(f"{self.BASE}/runs/{run_id}").json()
        self.assertEqual((view["phase"], view["can_cancel"], view["summary"]), ("submitting", False, "Submitting to Greenhouse…"))
        self.assertTrue(view["can_front"], "the window can still be brought forward")
        self.assertEqual(self.claim()["cancel_requested"], 0)
        self.runner.shutdown(30)

    def test_a_run_whose_claim_is_already_settled_is_finished_already_not_handed_over(self):
        self.factory.handoff = {"wait": 30, "outcome": "submitted"}
        run_id = self.handoff().json()["id"]
        self.turn(run_id)
        with self.conn:
            self.conn.execute("UPDATE application_submit_claims SET state='needs_you', after_click=0")
        refused = self.send("POST", f"{self.BASE}/runs/{run_id}/cancel")
        self.assertEqual((refused.status_code, refused.json()["detail"]), (409, apply_runner.FINISHED_ALREADY))
        with self.conn:
            self.conn.execute("UPDATE application_submit_claims SET state='claimed'")
        self.assertEqual(self.send("POST", f"{self.BASE}/runs/{run_id}/cancel").status_code, 200)
        self.finished(run_id)


class HandoffFrontTests(HandoffApiCase):
    def test_front_is_for_a_running_handoff_this_app_runs(self):
        rehearsal = self.rehearse().json()["id"]
        self.finished(rehearsal)
        response = self.send("POST", f"{self.BASE}/runs/{rehearsal}/front")
        self.assertEqual((response.status_code, response.json()["detail"]), (409, apply_runner.NOT_HANDOFF))
        self.factory.handoff = {"wait": 2.0, "outcome": "submitted"}
        run_id = self.handoff().json()["id"]
        self.turn(run_id)
        ok = self.send("POST", f"{self.BASE}/runs/{run_id}/front")
        self.assertEqual(ok.status_code, 200, ok.text)
        self.assertTrue(ok.json()["can_front"])
        view = self.finished(run_id)
        self.assertEqual(json.loads(self.conn.execute("SELECT evidence_json FROM apply_runs WHERE id=?", (run_id,)).fetchone()[0])["fronts"], 1)
        done = self.send("POST", f"{self.BASE}/runs/{run_id}/front")
        self.assertEqual((done.status_code, done.json()["detail"]), (409, apply_runner.NOT_RUNNING))
        self.assertEqual(self.send("POST", f"{self.BASE}/runs/run-{'0' * 32}/front").status_code, 404)
        elsewhere = apply_runs.create_run(
            self.conn, user_id=USER, opportunity_id=ACME, kind="handoff", started_by="student", ats="greenhouse", adapter_version="greenhouse-1", board_token="b",
            page_url=JOB_URL, company="acme", deadline_seconds=300,
        )
        response = self.send("POST", f"{self.BASE}/runs/{elsewhere}/front")
        self.assertEqual((response.status_code, response.json()["detail"]), (409, apply_runner.NOT_RUNNING), "a run another process is running")
        self.assertEqual(view["status"], "finished")


class HandoffPauseTests(HandoffApiCase):
    def test_a_pause_during_the_turn_says_the_window_stays_open(self):
        self.factory.handoff = {"wait": 30, "outcome": "submitted"}
        run_id = self.handoff().json()["id"]
        self.turn(run_id)
        paused = self.client.put("/api/v1/automation/settings", headers=AUTH, json={"paused": True})
        self.assertEqual(paused.status_code, 200, paused.text)
        items = paused.json()["in_flight"]
        self.assertEqual([(item["action"], item["label"]) for item in items],
                         [("window", "A Finish in browser window is open. Pausing doesn't stop your own Submit; press Stop to end it.")])
        health = self.get("/api/v1/automation").json()["health"]
        self.assertEqual([item["action"] for item in health["in_flight"]], ["window"])
        self.send("POST", f"{self.BASE}/runs/{run_id}/cancel")
        self.finished(run_id)
        self.assertEqual(self.client.put("/api/v1/automation/settings", headers=AUTH, json={"paused": False}).status_code, 200)


if __name__ == "__main__":
    unittest.main()
