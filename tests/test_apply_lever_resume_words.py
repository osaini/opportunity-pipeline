"""What a Finish in browser run on Lever says once Lever already holds the student's résumé (docs/phase5-lever-handoff-spec.md, L1, 9, 10.4 item 15).

Lever reads a résumé as soon as it is attached, so the file is with Lever before the student presses Submit, whether the app attached it or
the student did. A run that then ends without a submission must not say "Nothing was sent" alone. Nothing here opens a browser or the
network: the settlement table is pure, the run view reads a stored row, and every company and posting is fictional.
Greenhouse's sentences, and every sentence of a run that never saw a résumé reach Lever, are asserted to be the old ones.
"""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import realdata_guard

realdata_guard.install()

from opportunity_app.apply import agent_types, runner as apply_runner, runs as apply_runs
from opportunity_app.apply.agent_types import (
    HANDOFF_NOT_SUBMITTED, HANDOFF_UNRECORDED, RESUME_PLANNED_KEY, WINDOW_CLOSED, RunResult, resume_may_be_with_ats, resume_with_ats, with_resume_note,
)
from opportunity_app.apply.runner import handoff_settlement

from helpers_apply import USER, ApplyCase, setUpModule, tearDownModule  # noqa: F401

FOREIGN = "another-process"
RECEIVED = "Lever received your résumé."
NOT_SENT = f"Your application was not sent. {RECEIVED}"
MAYBE = "Lever may have received your résumé."
MAYBE_NOT_SENT = f"Your application was not sent. {MAYBE}"
PARSE_TIMEOUT = "Lever did not finish reading your résumé. Nothing was filled. Lever may still have the file."


class WordsTests(unittest.TestCase):
    def test_every_way_of_saying_it_was_not_sent_becomes_the_one_sentence(self):
        cases = {
            HANDOFF_NOT_SUBMITTED: f"You didn't submit it in the window. {NOT_SENT}",
            HANDOFF_UNRECORDED: f"The app couldn't record this submission, so it stopped it. {NOT_SENT} Try again.",
            "The browser stopped before the run finished. No application was sent.": f"The browser stopped before the run finished. {NOT_SENT}",
            "The form tried to send before the app finished. Nothing was sent. Try again.": f"The form tried to send before the app finished. {NOT_SENT} Try again.",
            WINDOW_CLOSED: f"You closed the window. {NOT_SENT}",
        }
        for before, after in cases.items():
            with self.subTest(before=before):
                self.assertEqual(with_resume_note(before, "Lever"), after)

    def test_a_sentence_that_does_not_say_it_gets_the_second_sentence_and_one_that_has_it_is_left(self):
        self.assertEqual(with_resume_note("The app stopped before filling it. Apply from the posting instead.", "Lever"),
                         f"The app stopped before filling it. Apply from the posting instead. {RECEIVED}")
        once = with_resume_note(HANDOFF_NOT_SUBMITTED, "Lever")
        self.assertEqual(with_resume_note(once, "Lever"), once, "said once, never twice")
        self.assertEqual(with_resume_note("", "Lever"), "")

    def test_the_evidence_says_it_by_the_apps_attach_or_the_students_own(self):
        self.assertTrue(resume_with_ats({"resume_sent_to_lever": True}))
        self.assertTrue(resume_with_ats({"student_attached_resume": {"count": 1}}))
        self.assertTrue(resume_with_ats({"student_attached_resume": {"count": 0, "sha256": "ab12"}}), "a file's hash is a file")
        empty = {"count": 0, "sha256": ""}
        for evidence in ({}, {"resume_sent_to_lever": False}, {"student_attached_resume": 0}, None, "resume_sent_to_lever", [],
                         {"resume_sent_to_lever": 1}, {"resume_sent_to_lever": "yes"},
                         {"student_attached_resume": empty}, {"student_attached_resume": {"count": 0}}, {"student_attached_resume": {}},
                         {"student_attached_resume": True}):
            with self.subTest(evidence=evidence):
                self.assertFalse(resume_with_ats(evidence), "a record set up empty is not a file that reached Lever")

    def test_a_planned_attach_is_a_maybe_until_something_confirms_it(self):
        self.assertTrue(resume_may_be_with_ats({RESUME_PLANNED_KEY: True}))
        self.assertFalse(resume_may_be_with_ats({RESUME_PLANNED_KEY: True, "resume_sent_to_lever": True}), "confirmed is not a maybe")
        self.assertFalse(resume_may_be_with_ats({RESUME_PLANNED_KEY: True, "resume_sent_to_lever": False}), "the window said no file went")
        for evidence in ({}, {RESUME_PLANNED_KEY: False}, None, "x"):
            with self.subTest(evidence=evidence):
                self.assertFalse(resume_may_be_with_ats(evidence))

    def test_a_sentence_that_already_speaks_of_the_file_is_left_alone(self):
        # Spec 6.5 step 3: the parse-timeout sentence is fixed, and it already says Lever may still have the file.
        self.assertEqual(with_resume_note(PARSE_TIMEOUT, "Lever"), PARSE_TIMEOUT)
        self.assertEqual(with_resume_note(PARSE_TIMEOUT, "Lever", sure=False), PARSE_TIMEOUT)

    def test_a_not_sent_clause_inside_a_sentence_is_rewritten_in_place_and_the_resume_follows(self):
        got = with_resume_note("The form did not send, so nothing was sent", "Lever")
        self.assertEqual(got, f"The form did not send, so your application was not sent. {RECEIVED}")
        self.assertEqual(with_resume_note("The form did not send, so nothing was sent.", "Lever"), f"The form did not send, so your application was not sent. {RECEIVED}")
        self.assertEqual(with_resume_note("The form did not send, so nothing was sent", "Lever", sure=False),
                         f"The form did not send, so your application was not sent. {MAYBE}")

    def test_the_maybe_wording_is_used_when_the_app_only_planned_the_attach(self):
        self.assertEqual(with_resume_note(HANDOFF_NOT_SUBMITTED, "Lever", sure=False), f"You didn't submit it in the window. {MAYBE_NOT_SENT}")
        self.assertEqual(with_resume_note("The app stopped during this run. No application was sent.", "Lever", sure=False),
                         f"The app stopped during this run. {MAYBE_NOT_SENT}")


class SettlementTests(unittest.TestCase):
    """The rows that end a run before the hand-over (5.3 rows 9 to 15) say the résumé is with Lever; the rows that may have sent do not change."""

    def settle(self, result=None, *, ats_name="Lever", resume_sent=False, resume_planned=False, **kwargs):
        facts = dict(stop="", shutting_down=False, claim_state="claimed", cancel_requested=False, handed_over=False, closed_confirmed=True, minutes=48)
        facts.update(kwargs)
        return handoff_settlement(result, ats_name=ats_name, resume_sent=resume_sent, resume_planned=resume_planned, **facts)

    def stopped(self, **evidence):
        return RunResult("needs_you", [HANDOFF_NOT_SUBMITTED], handed_over=False, after_click=False, evidence=dict(evidence))

    def test_the_students_stop_says_the_resume_is_with_lever_whichever_way_the_parent_learned_it(self):
        plain = self.settle(self.stopped(), cancel_requested=True)
        self.assertEqual((plain.row, plain.note), (9, HANDOFF_NOT_SUBMITTED))
        for label, kwargs in (("the parent's own record", dict(resume_sent=True)), ("the result's evidence", {})):
            with self.subTest(label):
                result = self.stopped(resume_sent_to_lever=True) if not kwargs else self.stopped()
                got = self.settle(result, cancel_requested=True, **kwargs)
                self.assertEqual((got.row, got.state, got.after_click), (9, "needs_you", False))
                self.assertEqual((got.note, got.reasons), (f"You didn't submit it in the window. {NOT_SENT}", [f"You didn't submit it in the window. {NOT_SENT}"]))

    def test_a_closed_window_a_dead_child_and_a_server_stop_say_it_too(self):
        closed = self.settle(RunResult("needs_you", [WINDOW_CLOSED], evidence={"handoff_end": "closed", "browser_closed": True}), resume_sent=True)
        self.assertEqual((closed.row, closed.note), (11, f"You closed the window. {NOT_SENT}"))
        died = self.settle(None, resume_sent=True, stop="child_died")
        self.assertEqual(died.row, 15)
        self.assertEqual(died.note, f"The browser stopped before the run finished. {NOT_SENT}")
        server = self.settle(None, resume_sent=True, shutting_down=True)
        self.assertEqual((server.row, server.note), (10, f"The app stopped during this run. {NOT_SENT}"))
        late = self.settle(None, resume_sent=True, stop=apply_runner.STOP_DEADLINE)
        self.assertEqual(late.row, 14)
        self.assertTrue(late.note.endswith(NOT_SENT), late.note)

    def test_the_parse_timeout_sentence_is_not_followed_by_a_claim_that_lever_got_the_file(self):
        result = RunResult("needs_you", [PARSE_TIMEOUT], handed_over=False, after_click=False, evidence={"resume_sent_to_lever": True, "handoff_end": ""})
        got = self.settle(result)
        self.assertEqual(got.note, PARSE_TIMEOUT)
        self.assertEqual(got.reasons, [PARSE_TIMEOUT])

    def test_an_ending_with_no_result_after_a_planned_attach_says_lever_may_have_the_file(self):
        for label, kwargs, row in (
            ("a server stop", dict(shutting_down=True), 10), ("the deadline", dict(stop=apply_runner.STOP_DEADLINE), 14), ("a dead child", dict(stop="child_died"), 15),
        ):
            with self.subTest(label):
                got = self.settle(None, resume_planned=True, **kwargs)
                self.assertEqual(got.row, row)
                self.assertTrue(got.note.endswith(MAYBE_NOT_SENT), got.note)
                self.assertNotIn("Nothing was sent", got.note)
                self.assertNotIn("No application was sent", got.note)

    def test_a_planned_attach_the_window_confirmed_says_it_received_it(self):
        got = self.settle(None, resume_planned=True, resume_sent=True, shutting_down=True)
        self.assertEqual(got.note, f"The app stopped during this run. {NOT_SENT}")

    def test_a_result_that_says_it_either_way_is_believed_over_the_plan(self):
        said_no = RunResult("needs_you", [HANDOFF_NOT_SUBMITTED], handed_over=False, after_click=False, evidence={"resume_sent_to_lever": False})
        got = self.settle(said_no, cancel_requested=True, resume_planned=True)
        self.assertEqual(got.note, HANDOFF_NOT_SUBMITTED, "the window reported that no file was attached")

    def test_without_a_resume_the_sentences_are_exactly_the_old_ones(self):
        for kwargs, row in (
            (dict(cancel_requested=True), 9), (dict(shutting_down=True), 10), (dict(stop="child_died"), 15),
        ):
            with self.subTest(row=row):
                got = self.settle(None if row != 9 else self.stopped(), **kwargs)
                self.assertEqual(got.row, row)
                self.assertNotIn("résumé", got.note + " ".join(got.reasons))

    def test_an_ending_that_may_have_sent_is_never_touched(self):
        # Row 6: handed over and nothing confirmed. The sentence says "may have been sent"; the résumé does not make it "not sent".
        got = self.settle(RunResult("needs_you", [HANDOFF_NOT_SUBMITTED], handed_over=True, after_click=True), claim_state="clicking", handed_over=True, resume_sent=True)
        self.assertEqual((got.row, got.state, got.after_click), (6, "unconfirmed", True))
        self.assertNotIn("résumé", got.note)
        unclosed = self.settle(self.stopped(), resume_sent=True, closed_confirmed=False)
        self.assertEqual((unclosed.row, unclosed.state), (7, "unconfirmed"))
        self.assertNotIn("résumé", unclosed.note)

    def test_greenhouse_is_unchanged_because_it_never_sends_a_resume_before_submit(self):
        got = self.settle(self.stopped(), ats_name="Greenhouse", cancel_requested=True)
        self.assertEqual(got.note, HANDOFF_NOT_SUBMITTED)
        self.assertEqual(agent_types.RESUME_EVIDENCE_KEYS, ("resume_sent_to_lever", "student_attached_resume"), "Lever's own evidence keys")


class ViewTests(ApplyCase):
    """The run view carries ``resume_sent_to_lever`` for the page, and says the same thing in its summary."""

    def handoff_run(self, *, evidence=None, outcome="needs_you", reasons=(HANDOFF_NOT_SUBMITTED,), ats="lever"):
        run_id = apply_runs.create_run(
            self.conn, user_id=USER, opportunity_id="op-run", kind="handoff", started_by="student", ats=ats, board_token="harbor-demo",
            page_url="https://jobs.lever.co/harbor-demo/6f1d2c3b-4a59-4687-8c7d-9e0f1a2b3c4d/apply", company="harbor demo labs",
            deadline_seconds=300, adapter_version="lever-1", now=self.at(),
        )
        apply_runs.finish_run(self.conn, run_id, outcome=outcome, reasons=list(reasons), evidence=evidence or {}, now=self.at(1))
        return apply_runner.run_view(self.conn, dict(self.conn.execute("SELECT * FROM apply_runs WHERE id=?", (run_id,)).fetchone()))

    def test_a_run_that_saw_the_resume_attached_says_so_and_does_not_say_nothing_was_sent(self):
        view = self.handoff_run(evidence={"resume_sent_to_lever": True})
        self.assertIs(view["resume_sent_to_lever"], True)
        self.assertEqual(view["summary"], f"You didn't submit it in the window. {NOT_SENT}")
        self.assertNotIn("Nothing was sent", view["summary"])

    def test_the_students_own_attach_counts_the_same(self):
        view = self.handoff_run(evidence={"student_attached_resume": {"count": 1}})
        self.assertIs(view["resume_sent_to_lever"], True)
        self.assertTrue(view["summary"].endswith(RECEIVED), view["summary"])

    def test_a_run_with_no_resume_sent_reads_as_before(self):
        view = self.handoff_run(evidence={})
        self.assertIs(view["resume_sent_to_lever"], False)
        self.assertEqual(view["summary"], HANDOFF_NOT_SUBMITTED)

    def test_a_sentence_without_a_not_sent_clause_gets_the_resume_after_it(self):
        view = self.handoff_run(evidence={"resume_sent_to_lever": True}, outcome="failed", reasons=["The form changed under the app"])
        self.assertEqual(view["summary"], f"The form changed under the app. {NOT_SENT}", "the view's own 'No application was sent' becomes the one sentence")

    def test_a_run_that_only_planned_the_attach_says_lever_may_have_it_and_does_not_claim_it_did(self):
        view = self.handoff_run(evidence={RESUME_PLANNED_KEY: True}, outcome="failed", reasons=["The app stopped during this run"])
        self.assertIs(view["resume_sent_to_lever"], False, "nothing confirmed it")
        self.assertIn(MAYBE, view["summary"])
        self.assertNotIn("No application was sent", view["summary"])

    def test_a_rehearsal_never_carries_it(self):
        run_id = self.make_run()
        apply_runs.finish_run(self.conn, run_id, outcome="rehearsed", clean=True, evidence={"resume_sent_to_lever": True}, now=self.at(1))
        row = dict(self.conn.execute("SELECT * FROM apply_runs WHERE id=?", (run_id,)).fetchone())
        self.assertIs(apply_runner.run_view(self.conn, row)["resume_sent_to_lever"], False)


class RecoveryTests(ApplyCase):
    """A server that stopped after the résumé was attached: the claim it leaves is settled with the same words."""

    def test_a_claim_that_never_handed_over_says_lever_has_the_resume_when_its_run_said_the_window_was_ready_with_it(self):
        token = self.raw_claim(state="claimed", mode="handoff", ats="lever", board="harbor-demo", instance=FOREIGN, heartbeat_at=self.at(-10).isoformat(timespec="microseconds"))
        run_id = apply_runs.create_run(
            self.conn, user_id=USER, opportunity_id=self.claim_row(token)["opportunity_id"], kind="handoff", started_by="student", ats="lever",
            board_token="harbor-demo", page_url="https://jobs.lever.co/harbor-demo/6f1d2c3b-4a59-4687-8c7d-9e0f1a2b3c4d/apply",
            company="harbor demo labs", deadline_seconds=300, adapter_version="lever-1", claim_token=token, now=self.at(-30),
        )
        with self.conn:
            self.conn.execute("UPDATE apply_runs SET evidence_json=? WHERE id=?", (json.dumps({"resume_sent_to_lever": True}), run_id))
            self.conn.execute("UPDATE application_submit_claims SET run_id=? WHERE token=?", (run_id, token))
        apply_runs.recover_stale(self.conn, self.at(), user_id=USER)
        self.assertEqual(self.claim_row(token)["state"], "failed")
        self.assertEqual(self.claim_row(token)["note"], f"The app stopped before handing your application to Lever. {NOT_SENT}")

    def test_a_claim_whose_run_only_planned_the_attach_says_lever_may_have_the_file(self):
        token = self.raw_claim(state="claimed", mode="handoff", ats="lever", board="harbor-demo", instance=FOREIGN, heartbeat_at=self.at(-10).isoformat(timespec="microseconds"))
        run_id = apply_runs.create_run(
            self.conn, user_id=USER, opportunity_id=self.claim_row(token)["opportunity_id"], kind="handoff", started_by="student", ats="lever",
            board_token="harbor-demo", page_url="https://jobs.lever.co/harbor-demo/6f1d2c3b-4a59-4687-8c7d-9e0f1a2b3c4d/apply",
            company="harbor demo labs", deadline_seconds=300, adapter_version="lever-1", claim_token=token, now=self.at(-30),
            evidence={RESUME_PLANNED_KEY: True},
        )
        with self.conn:
            self.conn.execute("UPDATE application_submit_claims SET run_id=? WHERE token=?", (run_id, token))
        apply_runs.recover_stale(self.conn, self.at(), user_id=USER)
        self.assertEqual(self.claim_row(token)["note"], f"The app stopped before handing your application to Lever. {MAYBE_NOT_SENT}")

    def test_a_greenhouse_claim_is_settled_with_the_old_words(self):
        token = self.raw_claim(state="claimed", mode="handoff", instance=FOREIGN, heartbeat_at=self.at(-10).isoformat(timespec="microseconds"))
        apply_runs.recover_stale(self.conn, self.at(), user_id=USER)
        self.assertEqual(self.claim_row(token)["note"], "The app stopped before handing your application to Greenhouse. Nothing was sent.")


if __name__ == "__main__":
    unittest.main()
