"""The Lever rows of the outcome table (docs/phase5-lever-handoff-spec.md 6.13), from what the browser saw, with no browser.

``decide_outcome`` hands over to ``lever_outcome`` for Lever's policy. Page wording is never an input: the table reads requests, statuses, the main
frame's address, whether the form is there, and whether a challenge frame is. Every company, posting and person is fictional.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import realdata_guard

realdata_guard.install()

from opportunity_app.apply import checks
from opportunity_app.apply.checks import Observation, SeenRequest, decide_outcome

POLICY = checks.LEVER_ROUTE_POLICY
SITE, JOB = "tidewatergames", "6a1f0c52-9b3e-4d17-8c40-2e5d7a91b0f3"
HOST, EU = "jobs.lever.co", "jobs.eu.lever.co"
APPLY_PATH, THANKS_PATH = f"/{SITE}/{JOB}/apply", f"/{SITE}/{JOB}/thanks"
KEYS = {"submit_post", "submit_status", "confirmation_path", "form_absent"}


def post(status, *, host=HOST, path=APPLY_PATH, passed=True, method="POST"):
    return SeenRequest(method, host, path, status, passed)


def seen(*requests, **overrides):
    values = dict(
        main_path=APPLY_PATH, main_host=HOST, form_present=True, requests=tuple(requests), submit_path=APPLY_PATH, confirmation_path=THANKS_PATH,
        board_token=SITE, job_id=JOB, board_host=HOST,
    )
    values.update(overrides)
    return Observation(**values)


def confirmed(status=302, **overrides):
    return seen(post(status), **{"main_path": THANKS_PATH, "form_present": False, "navigated": True, **overrides})


class SubmittedRowTests(unittest.TestCase):
    def test_a_2xx_or_3xx_answer_then_the_postings_thanks_page_on_the_same_host_with_the_form_gone_is_submitted(self):
        for status in (200, 201, 204, 301, 302, 303, 307, 399):
            with self.subTest(status=status):
                outcome = decide_outcome(confirmed(status), POLICY)
                self.assertEqual((outcome.outcome, outcome.after_click, outcome.resolved_by, outcome.note), ("submitted", 1, "page", ""))
                self.assertTrue(outcome.settled)
                self.assertEqual(set(outcome.evidence), KEYS)
                self.assertEqual(outcome.evidence["submit_status"], status)
                self.assertEqual(outcome.evidence["confirmation_path"], THANKS_PATH)

    def test_the_eu_host_is_the_same_cell_with_its_own_host(self):
        obs = seen(post(302, host=EU), main_path=THANKS_PATH, main_host=EU, board_host=EU, form_present=False, navigated=True)
        self.assertEqual(decide_outcome(obs, POLICY).outcome, "submitted")

    def test_the_trailing_slash_of_thanks_counts(self):
        self.assertEqual(decide_outcome(confirmed(main_path=THANKS_PATH + "/"), POLICY).outcome, "submitted")

    def test_the_other_lever_host_is_not_the_same_host(self):
        for main_host in (EU, "", "example-games.test"):
            with self.subTest(main_host=main_host):
                outcome = decide_outcome(confirmed(main_host=main_host), POLICY)
                self.assertEqual((outcome.outcome, outcome.after_click), ("unconfirmed", 1))

    def test_a_thanks_page_of_another_posting_is_not_this_ones(self):
        for path in (f"/{SITE}/ffffffff-0000-4000-8000-000000000000/thanks", f"/other/{JOB}/thanks", "/thanks", f"/{SITE}/{JOB}/thanks/x", APPLY_PATH, f"/{SITE}/{JOB}"):
            with self.subTest(path=path):
                self.assertEqual(decide_outcome(confirmed(main_path=path), POLICY).outcome, "unconfirmed")

    def test_the_thanks_page_without_the_form_gone_is_not_submitted(self):
        outcome = decide_outcome(confirmed(form_present=True), POLICY)
        self.assertEqual((outcome.outcome, outcome.after_click, outcome.note), ("unconfirmed", 1, outcome.note))
        self.assertIn("Lever did not show its confirmation page", outcome.note)

    def test_a_post_that_is_not_the_apply_post_does_not_make_it_submitted(self):
        for request in (post(302, host=EU), post(302, path=f"/{SITE}/{JOB}/other"), post(302, method="PUT"), post(302, passed=False), post(302, path="/parseResume")):
            with self.subTest(request=request):
                obs = seen(request, main_path=THANKS_PATH, form_present=False, navigated=True)
                self.assertNotEqual(decide_outcome(obs, POLICY).outcome, "submitted")

    def test_with_no_host_bound_nothing_is_a_submit_post(self):
        outcome = decide_outcome(confirmed(board_host=""), POLICY)
        self.assertNotEqual(outcome.outcome, "submitted")
        self.assertFalse(outcome.evidence["submit_post"])

    def test_a_navigation_to_thanks_with_no_post_is_not_submitted(self):
        obs = seen(main_path=THANKS_PATH, form_present=False, navigated=True)
        outcome = decide_outcome(obs, POLICY)
        self.assertEqual((outcome.outcome, outcome.after_click, outcome.settled), ("unconfirmed", 1, False))
        self.assertEqual(outcome.evidence["confirmation_path"], THANKS_PATH)
        self.assertFalse(outcome.evidence["submit_post"])

    def test_a_2xx_that_left_the_form_on_the_page_is_unconfirmed(self):
        outcome = decide_outcome(seen(post(200)), POLICY)
        self.assertEqual((outcome.outcome, outcome.after_click, outcome.settled), ("unconfirmed", 1, False))

    def test_a_answer_that_is_not_in_yet_is_unconfirmed_and_not_settled(self):
        outcome = decide_outcome(seen(post(None)), POLICY)
        self.assertEqual((outcome.outcome, outcome.after_click, outcome.settled), ("unconfirmed", 1, False))


class RefusedRowTests(unittest.TestCase):
    def test_a_4xx_with_the_form_still_there_is_failed_after_the_click_and_names_the_status(self):
        for status in (400, 401, 403, 404, 409, 422, 429, 499):
            with self.subTest(status=status):
                outcome = decide_outcome(seen(post(status)), POLICY)
                self.assertEqual((outcome.outcome, outcome.after_click, outcome.note), ("failed", 1, f"Lever refused the form (HTTP {status})"))
                self.assertTrue(outcome.settled)

    def test_the_note_names_the_first_marked_question_and_never_the_pages_text(self):
        outcome = decide_outcome(seen(post(422), first_field_error="Current company"), POLICY)
        self.assertEqual(outcome.note, 'Lever refused the form (HTTP 422). Lever marked "Current company" as wrong')

    def test_a_428_is_one_more_refusal_there_is_no_emailed_code_on_lever(self):
        outcome = decide_outcome(seen(post(428), security_code_visible=True), POLICY)
        self.assertEqual((outcome.outcome, outcome.after_click, outcome.note), ("failed", 1, "Lever refused the form (HTTP 428)"))

    def test_a_4xx_with_the_form_gone_is_unconfirmed(self):
        outcome = decide_outcome(seen(post(403), form_present=False), POLICY)
        self.assertEqual((outcome.outcome, outcome.after_click), ("unconfirmed", 1))

    def test_a_5xx_is_unconfirmed_whatever_the_form_does(self):
        for status in (500, 502, 503, 504):
            for form_present in (True, False):
                with self.subTest(status=status, form_present=form_present):
                    outcome = decide_outcome(seen(post(status), form_present=form_present), POLICY)
                    self.assertEqual((outcome.outcome, outcome.after_click, outcome.settled), ("unconfirmed", 1, False))


class NothingSentRowTests(unittest.TestCase):
    def test_no_post_and_no_navigation_is_failed_before_the_click(self):
        outcome = decide_outcome(seen(), POLICY)
        self.assertEqual((outcome.outcome, outcome.after_click, outcome.note, outcome.settled), ("failed", 0, "Nothing that could carry the application left the window", False))
        self.assertEqual(set(outcome.evidence), KEYS)

    def test_a_refused_post_is_the_same_row(self):
        outcome = decide_outcome(seen(post(None, passed=False)), POLICY)
        self.assertEqual((outcome.outcome, outcome.after_click, outcome.note), ("failed", 0, "Nothing that could carry the application left the window"))

    def test_a_post_that_is_not_the_apply_post_is_the_same_row(self):
        for request in (post(200, host="hcaptcha.com", path="/checkcaptcha"), post(200, path="/cdn-cgi/challenge-platform/h/b"), post(200, host=EU)):
            with self.subTest(request=request):
                self.assertEqual(decide_outcome(seen(request), POLICY).after_click, 0)

    def test_the_first_marked_question_is_named_by_its_question_only(self):
        outcome = decide_outcome(seen(first_field_error="Email"), POLICY)
        self.assertEqual(outcome.note, 'Nothing that could carry the application left the window. Lever marked "Email" as wrong')

    def test_a_navigation_without_a_post_is_not_this_row(self):
        outcome = decide_outcome(seen(navigated=True), POLICY)
        self.assertEqual((outcome.outcome, outcome.after_click), ("unconfirmed", 1))


class ChallengeTests(unittest.TestCase):
    def test_a_challenge_before_a_post_is_no_outcome(self):
        outcome = decide_outcome(seen(challenge_frame=True), POLICY)
        self.assertEqual((outcome.outcome, outcome.after_click, outcome.detail, outcome.settled), ("waiting", 0, {"waiting": "challenge"}, False))
        self.assertNotIn("security_code", outcome.detail)

    def test_a_challenge_before_a_post_stays_no_outcome_with_the_cloudflare_or_captcha_posts_it_makes(self):
        requests = (post(200, host="api.hcaptcha.com", path="/checkcaptcha/x"), post(204, path="/cdn-cgi/challenge-platform/h/b/jsd/oneshot/a"))
        self.assertEqual(decide_outcome(seen(*requests, challenge_frame=True), POLICY).outcome, "waiting")

    def test_when_the_waiting_is_over_a_challenge_never_finished_is_nothing_sent(self):
        outcome = decide_outcome(seen(challenge_frame=True), POLICY, code_wait_over=True)
        self.assertEqual((outcome.outcome, outcome.after_click, outcome.note), ("failed", 0, "Nothing that could carry the application left the window"))

    def test_a_challenge_after_a_post_is_needs_you_after_the_click(self):
        for status in (None, 200, 302, 422, 500):
            for wait_over in (False, True):
                with self.subTest(status=status, wait_over=wait_over):
                    outcome = decide_outcome(seen(post(status), challenge_frame=True), POLICY, code_wait_over=wait_over)
                    self.assertEqual((outcome.outcome, outcome.after_click, outcome.note), ("needs_you", 1, "Lever showed a check that wasn't finished. Look for Lever's email"))

    def test_the_confirmation_wins_over_a_challenge_frame_left_in_the_dom(self):
        self.assertEqual(decide_outcome(confirmed(challenge_frame=True), POLICY).outcome, "submitted")


class TableShapeTests(unittest.TestCase):
    def test_decide_outcome_gives_lever_its_own_table_and_greenhouse_the_shared_one(self):
        obs = seen(post(428), security_code_visible=True)
        self.assertEqual(decide_outcome(obs, POLICY), checks.lever_outcome(obs, POLICY))
        shared = decide_outcome(Observation(
            main_path="/b/jobs/1", requests=(SeenRequest("POST", "boards.greenhouse.io", "/b/jobs/1", 428),), submit_path="/b/jobs/1", security_code_visible=True,
        ), checks.GREENHOUSE_ROUTE_POLICY)
        self.assertEqual(shared.outcome, "waiting")
        self.assertEqual(shared.detail, {"waiting": "security_code"})

    def test_page_wording_is_not_an_input(self):
        self.assertFalse({"text", "title", "html", "body"} & set(Observation.__dataclass_fields__))

    def test_every_outcome_carries_the_same_evidence_keys(self):
        for obs in (seen(), seen(post(200)), seen(post(422)), confirmed(), seen(challenge_frame=True), seen(challenge_frame=True, requests=(post(200),))):
            self.assertEqual(set(decide_outcome(obs, POLICY).evidence), KEYS)

    def test_a_post_is_counted_only_for_the_host_it_went_to(self):
        obs = seen(post(302, host=EU), main_path=THANKS_PATH, main_host=EU, board_host=HOST, form_present=False, navigated=True)
        self.assertNotEqual(decide_outcome(obs, POLICY).outcome, "submitted")


if __name__ == "__main__":
    unittest.main()
