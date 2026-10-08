"""Apply for me on Lever, read only (docs/phase5-lever-handoff-spec.md, milestone LV2): a saved Lever role gets the same "what is missing" check
as a Greenhouse one, from the posting's own application page, with no browser. Nothing here opens a browser or the network: the page is a
fixture served by ``FakeLeverPageClient`` (tests/fixtures/apply/lever/), and every company, person and posting is fictional.

Covers the check for a Lever role (6.0), the two switches' effect on it, and that opening the section writes nothing and changes nothing in the tracker.
The plan's Lever rows are in test_apply_lever_plan.py, the refusal of every mode but Finish in browser in test_apply_lever_modes.py.
"""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import realdata_guard

realdata_guard.install()

from opportunity_app.apply import preflight as apply_preflight, sensitive as apply_sensitive

from apply_fake_ats import FakeLeverPageClient, LEVER_COMPANY, LEVER_JOB_ID, LEVER_ROLE_ID, LEVER_SITE, LEVER_TITLE, LEVER_URL, seed_lever_role
from helpers_apply import USER, setUpModule, tearDownModule  # noqa: F401
from test_apply_policy import PolicyCase, StaticClient


# --- The check for a saved Lever role (spec 6.0) --------------------------------------------------------------------------

class LeverCheckTests(PolicyCase):
    def setUp(self):
        super().setUp()
        with self.conn:
            seed_lever_role(self.conn, USER)
        self.pages = FakeLeverPageClient()
        self.switch("apply_agent", "on")
        self.switch("apply_agent_lever", "on")

    def switch(self, key, value):
        with self.conn:
            self.conn.execute(
                "INSERT INTO user_settings(user_id, key, value, updated_at) VALUES(?, ?, ?, ?) ON CONFLICT(user_id, key) DO UPDATE SET value=excluded.value",
                (USER, key, value, "2026-09-29T12:00:00+00:00"),
            )

    def lever_check(self, role=LEVER_ROLE_ID, **kwargs):
        kwargs.setdefault("page_client", self.pages)
        return apply_preflight.check(
            self.conn, USER, role, client=kwargs.pop("client", StaticClient({})), cache=kwargs.pop("cache", None), resume_root=self.resumes,
            now=self.at(0), **kwargs,
        )

    def test_a_saved_lever_role_shows_what_the_app_would_fill_and_what_is_missing(self):
        result = self.lever_check()
        self.assertEqual((result["ats"], result["ats_name"], result["board_token"], result["job_id"]), ("lever", "Lever", LEVER_SITE, LEVER_JOB_ID))
        self.assertEqual(result["canonical_url"], f"{LEVER_URL}/apply")
        self.assertEqual(result["status"], "needs_you")
        self.assertEqual([(item["key"], item["kind"], item["action"]["type"]) for item in result["problems"]], [("org", "window", "manual")])
        self.assertEqual(result["message"], "The app has everything it can fill. 1 question is yours to answer on the Lever form")
        self.assertEqual(result["counts"]["left_for_you"], 1)
        self.assertEqual(result["posting"], {"title": f"{LEVER_COMPANY} - {LEVER_TITLE}", "company": "", "url": f"{LEVER_URL}/apply", "differs": False, "difference": ""})
        names = {item["key"]: item for item in result["fields"]}
        self.assertEqual(names["name"]["source"], "Profile")
        self.assertIn("disability_status", names)

    def test_it_says_plainly_that_nothing_can_start_yet_and_offers_no_window_action(self):
        result = self.lever_check()
        self.assertEqual(result["offers"], {"rehearse": False, "handoff": False, "note": "Finish in browser for Lever postings is not available yet"})
        self.assertEqual(
            {name: (row_["allowed"], row_["reason"]) for name, row_ in result["eligibility"].items()},
            {"rehearse": (False, "Lever supports Finish in browser only, for now"),
             "handoff": (False, "Finish in browser for Lever postings is not available yet"),
             "submit": (False, "Lever supports Finish in browser only, for now")},
        )
        self.assertEqual(result["eligibility"]["handoff"]["ticks"], [])

    def test_the_resume_sentence_follows_the_students_choice_and_it_starts_off(self):
        self.assertIn("you attach it yourself in the window", self.lever_check()["notes"][0])
        self.switch("apply_lever_resume_upload", "on")
        notes = self.lever_check()["notes"]
        self.assertIn("sent to Lever before you press Submit", notes[0])
        field = next(item for item in self.lever_check()["fields"] if item["key"] == "resume")
        self.assertEqual(field["source"], "Your confirmed résumé")

    def test_with_the_lever_switch_off_the_answer_says_how_to_turn_it_on_and_asks_lever_nothing(self):
        self.switch("apply_agent_lever", "off")
        result = self.lever_check()
        self.assertEqual((result["status"], result["ats"], result["message"]), ("unavailable", "lever", "Apply for me works with Lever postings once you turn it on in Apply agent settings"))
        self.assertEqual(self.pages.calls, [])
        self.assertEqual(result["eligibility"]["handoff"]["allowed"], False)

    def test_a_greenhouse_role_does_not_need_the_lever_switch(self):
        self.switch("apply_agent_lever", "off")
        self.role()
        self.assertEqual(self.run_check("gh-1", page_client=self.pages)["status"], "needs_you")
        self.assertEqual(self.pages.calls, [])

    def test_only_a_404_is_closed_and_every_other_failure_is_lever_did_not_answer(self):
        self.assertEqual(self.lever_check(page_client=FakeLeverPageClient(closed=True))["message"], "The app couldn't find this posting on Lever. It may be closed")
        for pages in (FakeLeverPageClient(unavailable=True), None):
            with self.subTest(pages=pages):
                result = self.lever_check(page_client=pages)
                self.assertEqual((result["status"], result["message"]), ("failed", "Lever did not answer. Try again later"))

    def test_a_cloudflare_page_that_is_a_200_with_no_form_is_lever_did_not_answer_never_closed(self):
        interstitial = FakeLeverPageClient(pages={f"{LEVER_SITE}/{LEVER_JOB_ID}": "cloudflare_interstitial.html"})
        result = self.lever_check(page_client=interstitial)
        self.assertEqual((result["status"], result["message"]), ("failed", "Lever did not answer. Try again later"))
        thanks = FakeLeverPageClient(pages={f"{LEVER_SITE}/{LEVER_JOB_ID}": "thanks.html"})
        self.assertEqual(self.lever_check(page_client=thanks)["message"], "Lever did not answer. Try again later", "a confirmation page has no form to read")

    def test_the_answer_is_kept_for_an_hour_per_ats_site_and_posting_and_only_when_it_came_back(self):
        cache = apply_preflight.SchemaCache()
        results = [self.lever_check(cache=cache) for _ in range(3)]
        self.assertEqual(len(self.pages.calls), 1)
        self.assertEqual([item["from_cache"] for item in results], [False, True, True])
        self.assertIsNone(cache.get(("greenhouse", LEVER_SITE, LEVER_JOB_ID)), "another ATS with the same names is another posting")
        self.assertIsNotNone(cache.get(("lever", LEVER_SITE, LEVER_JOB_ID)))
        closed = FakeLeverPageClient(closed=True)
        fresh = apply_preflight.SchemaCache()
        self.lever_check(cache=fresh, page_client=closed)
        self.assertIsNone(fresh.get(("lever", LEVER_SITE, LEVER_JOB_ID)), "a 404 is asked about again")

    def test_a_page_for_another_company_is_flagged_and_the_students_word_is_needed(self):
        with self.conn:
            self.conn.execute("UPDATE opportunities SET company='Orbit Systems' WHERE id=?", (LEVER_ROLE_ID,))
        result = self.lever_check()
        self.assertTrue(result["posting"]["differs"])
        self.assertIn("Lever's page is titled", result["posting"]["difference"])
        self.assertEqual(result["status"], "needs_you")
        with self.assertRaises(apply_preflight.AnswerRefused):
            apply_preflight.answer_missing(self.conn, USER, LEVER_ROLE_ID, key="org", answer="x", client=StaticClient({}), page_client=self.pages, resume_root=self.resumes)

    def test_a_role_whose_title_has_a_dash_in_it_still_matches_its_page(self):
        with self.conn:
            self.conn.execute("UPDATE opportunities SET company='Tidewater Games', title='Associate Producer - Summer Intern' WHERE id=?", (LEVER_ROLE_ID,))
        pages = FakeLeverPageClient(pages={f"{LEVER_SITE}/{LEVER_JOB_ID}": "variants.html"})
        self.assertFalse(self.lever_check(page_client=pages)["posting"]["differs"])

    def test_opening_the_section_writes_nothing_and_changes_nothing_in_the_tracker(self):
        self.conn.commit()
        before = {table: self.conn.execute(f'SELECT COUNT(*), COALESCE(MAX(rowid), 0) FROM "{table}"').fetchone()[:2]
                  for (table,) in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()}
        changes = self.conn.total_changes
        for _ in range(3):
            self.lever_check()
        after = {table: self.conn.execute(f'SELECT COUNT(*), COALESCE(MAX(rowid), 0) FROM "{table}"').fetchone()[:2] for table in before}
        self.assertEqual(after, before)
        self.assertEqual(self.conn.total_changes, changes)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM applications WHERE opportunity_id=?", (LEVER_ROLE_ID,)).fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM apply_runs").fetchone()[0], 0)

    def test_the_answer_holds_no_value_of_the_student(self):
        text = json.dumps(self.lever_check())
        for secret in ("sam.rivera@example.test", "555-0100", "Sam Rivera", "Rivera"):
            self.assertNotIn(secret, text)

    def test_a_posting_in_the_eu_is_read_from_the_eu_host(self):
        with self.conn:
            self.conn.execute("UPDATE opportunities SET url=? WHERE id=?", (LEVER_URL.replace("jobs.lever.co", "jobs.eu.lever.co"), LEVER_ROLE_ID))
        result = self.lever_check()
        self.assertEqual(self.pages.calls, [(LEVER_SITE, LEVER_JOB_ID, "jobs.eu.lever.co")])
        self.assertTrue(result["canonical_url"].startswith("https://jobs.eu.lever.co/"))

    def test_the_optional_questions_the_app_could_answer_never_include_the_disability_question_or_its_signature(self):
        apply_sensitive.set_allowed_categories(self.conn, USER, ["eeo_gender", "eeo_race", "eeo_veteran", "eeo_disability"])
        offered = [item["key"] for item in self.lever_check()["optional_sensitive"]]
        self.assertEqual(sorted(offered), ["gender", "race", "veteran_status"])
        for key in ("disability_status", "eeo[disabilitySignature]", "eeo[disabilitySignatureDate]"):
            with self.assertRaises(apply_preflight.AnswerRefused):
                apply_preflight.answer_sensitive(
                    self.conn, USER, LEVER_ROLE_ID, key=key, answer="I do not want to answer", consent=True, client=StaticClient({}),
                    page_client=self.pages, resume_root=self.resumes,
                )

    def test_a_decline_answered_on_a_lever_role_is_stored_and_then_planned(self):
        apply_sensitive.set_allowed_categories(self.conn, USER, ["eeo_gender"])
        saved = apply_preflight.answer_sensitive(
            self.conn, USER, LEVER_ROLE_ID, key="gender", answer="Decline to self-identify", consent=True, client=StaticClient({}),
            page_client=self.pages, resume_root=self.resumes,
        )
        gender = next(item for item in saved["check"]["fields"] if item["key"] == "gender")
        self.assertTrue(gender["source"].startswith("Sensitive answer you added"))

    def test_the_greenhouse_answer_is_unchanged_in_shape_and_offers_both_actions(self):
        self.role()
        result = self.run_check("gh-1")
        self.assertEqual(result["offers"], {"rehearse": True, "handoff": True, "note": ""})
        self.assertEqual(result["notes"], [])
        self.assertEqual(result["ats"], "greenhouse")

    def test_a_role_on_neither_ats_says_which_it_works_with(self):
        self.opportunity("plain-1")
        self.assertEqual(self.lever_check("plain-1")["message"], "Apply for me works with Greenhouse and Lever postings only, for now")


if __name__ == "__main__":
    unittest.main()
