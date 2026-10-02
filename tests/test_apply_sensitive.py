"""Apply for me's sensitive-answers store (apply_sensitive.py) and the plan that reads it (spec 5.4, 7.1, 7.5, 12.7).

No browser and no network. Every company, form and answer is fictional. The store holds only what the student chose to
let the app type into an application form, so these tests pin what it refuses as much as what it keeps: nothing but a
decline for an EEO question, nothing for export control or salary, and a statement that cites a document for one
company only. The plan tests run rows 4 to 10 and 38 of section 7.5 against a real database instead of a stand-in.
"""

import ast
import copy
import json
import re
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from opportunity_app import apply_classify, apply_policy, apply_preflight, apply_sensitive
from opportunity_app.apply_checks import question_key
from opportunity_app.apply_policy import SchemaField
from opportunity_app.apply_sensitive import StoreRefused, add_entry

from apply_fake_ats import fixture_json
import helpers_apply as apply_helpers
from helpers_apply import ACCURATE, BASE, COMPANY, F, FACTS, KEY, LETTER_NONE, MULTI, OTHER, RESUME_OK, SINGLE, USER, StoreCase
# unittest and pytest run the module fixtures they find in the test module's namespace.
from helpers_apply import setUpModule, tearDownModule  # noqa: F401

REPO = Path(__file__).resolve().parent.parent
AUTH = "Are you legally authorized to work in the United States?"
SPONSOR = "Will you now or in the future require sponsorship for employment visa status?"
PRIVACY = "I have read the Example Robotics privacy notice"
GENDER = "Gender"
NOTICE_URL = "https://example-robotics.test/legal/privacy"
DECLINE = "Decline To Self Identify"


class AllowedCategoryTests(StoreCase):
    def test_nothing_is_switched_on_until_the_student_does_it(self):
        self.assertEqual(apply_sensitive.allowed_categories(self.conn, USER), frozenset())
        self.refused("Allow answers about work authorization first", category="work_authorization", question=AUTH, answer="Yes")
        self.assertEqual(self.rows(), [])

    def test_a_kind_is_switched_on_in_the_settings_order_and_off_again(self):
        saved = apply_sensitive.set_allowed_categories(self.conn, USER, ["consent", "work_authorization", "eeo_gender"])
        self.assertEqual(saved, ["work_authorization", "eeo_gender", "consent"])
        self.assertEqual(apply_sensitive.allowed_categories(self.conn, USER), {"work_authorization", "eeo_gender", "consent"})
        apply_sensitive.set_allowed_categories(self.conn, USER, [])
        self.assertEqual(apply_sensitive.allowed_categories(self.conn, USER), frozenset())

    def test_export_control_salary_and_anything_unknown_cannot_be_switched_on(self):
        for category, needle in (("export_control", "never answers export control"), ("salary", "never answers salary"),
                                 ("uncategorized", "never stores"), ("favorite_color", "Unknown kind")):
            with self.subTest(category=category), self.assertRaises(StoreRefused) as caught:
                apply_sensitive.set_allowed_categories(self.conn, USER, ["work_authorization", category])
            self.assertIn(needle, str(caught.exception))
        self.assertEqual(apply_sensitive.allowed_categories(self.conn, USER), frozenset(), "a refused change changes nothing")

    def test_a_value_written_to_the_setting_by_hand_still_never_allows_salary(self):
        with self.conn:
            self.conn.execute(
                "INSERT INTO user_settings(user_id, key, value, updated_at) VALUES(?, 'apply_sensitive_categories', 'salary,export_control,age_18', ?)",
                (USER, self.base.isoformat()))
        self.assertEqual(apply_sensitive.allowed_categories(self.conn, USER), {"age_18"})
        src = apply_policy.sources_for(self.conn, USER, "job-a", company=COMPANY)
        self.assertEqual(src.sensitive_allowed, {"age_18"})

    def test_every_group_the_settings_show_is_made_of_storable_categories_only(self):
        listed = [category for _key, _label, categories in apply_sensitive.CATEGORY_GROUPS for category in categories]
        self.assertEqual(sorted(listed), sorted(apply_sensitive.STORABLE))
        for banned in ("export_control", "salary", "uncategorized"):
            self.assertNotIn(banned, apply_sensitive.STORABLE)


class WritingTests(StoreCase):
    def test_the_consent_tick_is_required_and_it_is_recorded_with_its_scope_and_time(self):
        self.allow("work_authorization")
        for consent in (False, None, "yes", 1):
            with self.subTest(consent=consent), self.assertRaises(StoreRefused) as caught:
                add_entry(self.conn, USER, category="work_authorization", question=AUTH, answer="Yes", consent=consent)
            self.assertIn("Tick the box", str(caught.exception))
        self.assertEqual(self.rows(), [])
        entry = self.add(category="work_authorization", question=AUTH, answer="Yes", now=self.base)
        row = self.rows()[0]
        self.assertEqual((row["consent_scope"], row["consented_at"], row["last_used_at"]), ("confirmed", self.base.isoformat(timespec="microseconds"), None))
        self.assertEqual((row["answer_kind"], row["answer"], row["company_key"], row["question_key"]), ("option", "Yes", "", question_key(AUTH)))
        self.assertEqual(entry["question"], AUTH)
        self.assertTrue(entry["any_company"])
        self.assertIn("only to fill in application forms", apply_sensitive.CONSENT_TEXT)

    def test_the_same_question_for_the_same_company_is_replaced_and_consent_is_given_again(self):
        self.allow("work_authorization")
        first = self.add(category="work_authorization", question=AUTH, answer="Yes", now=self.base)
        again = self.add(category="work_authorization", question=AUTH.upper(), answer="No", now=self.base.replace(year=2030))
        self.assertEqual(first["id"], again["id"], "one row, edited in place")
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual((self.rows()[0]["answer"], self.rows()[0]["consented_at"][:4]), ("No", "2030"))
        self.add(category="work_authorization", question=AUTH, answer="Yes", company="Example Robotics")
        self.assertEqual(len(self.rows()), 2, "another company is another entry")

    def test_the_kinds_of_answer_and_their_lengths_are_checked(self):
        self.allow("work_authorization", "sponsorship")
        self.refused("Give the answer first", category="work_authorization", question=AUTH, answer="   ")
        self.refused("too long", category="work_authorization", question=AUTH, answer="x" * 501)
        self.refused("not stored", category="work_authorization", question=AUTH, answer="Yes", answer_kind="anything")
        self.refused("Give the question", category="work_authorization", question="  ?! ", answer="Yes")
        self.refused("too long", category="work_authorization", question="Are you authorized to work? " * 300, answer="Yes")
        # A list of options or a typed answer is kept for one company (spec 5.4 "As built"); only a select's one exact label is kept for any.
        self.refused("never for any company", category="sponsorship", question=SPONSOR, answer=["No", "Not now"], answer_kind="options")
        self.refused("never for any company", category="sponsorship", question=SPONSOR, answer="No, I will not", answer_kind="text")
        entry = self.add(category="sponsorship", question=SPONSOR, answer=["No", "Not now"], answer_kind="options", company="Example Robotics")
        self.assertEqual((entry["answer_kind"], entry["answer"], entry["any_company"]), ("options", "No\nNot now", False))

    def test_the_number_of_entries_is_capped(self):
        self.allow("work_authorization")
        original = apply_sensitive.MAX_ENTRIES
        self.addCleanup(setattr, apply_sensitive, "MAX_ENTRIES", original)
        apply_sensitive.MAX_ENTRIES = 2
        self.add(category="work_authorization", question=AUTH, answer="Yes", company="Alpha Labs")
        self.add(category="work_authorization", question=AUTH, answer="Yes", company="Beta Labs")
        self.refused("too many", category="work_authorization", question=AUTH, answer="Yes", company="Gamma Labs")
        self.add(category="work_authorization", question=AUTH, answer="No", company="Beta Labs")  # replacing one is not adding one
        self.assertEqual(len(self.rows()), 2)

    def test_a_company_name_the_app_cannot_match_on_is_refused_rather_than_saved_for_everyone(self):
        self.allow("work_authorization")
        self.refused("no company name", category="work_authorization", question=AUTH, answer="Yes", company="Inc.")
        self.assertEqual(self.rows(), [])

    def test_the_list_shows_the_company_as_it_was_typed_never_the_matching_key_rebuilt(self):
        self.allow("work_authorization")
        for typed, key in (("Zeta Alpha Labs, Inc.", "alpha labs zeta"), ("McKinsey & Company", "mckinsey"), ("IBM", "ibm")):
            with self.subTest(typed=typed):
                saved = self.add(category="work_authorization", question=f"{AUTH} ({typed})", answer="Yes", company=typed)
                self.assertEqual(saved["company"], typed)
                self.assertEqual(self.rows()[-1]["company_key"], key, "matching still uses the key")
        self.assertEqual([entry["company"] for entry in apply_sensitive.list_entries(self.conn, USER)],
                         ["Zeta Alpha Labs, Inc.", "McKinsey & Company", "IBM"])
        # A row saved before the name was kept shows its key as it is, not words nobody wrote.
        with self.conn:
            self.conn.execute("UPDATE apply_sensitive_answers SET company_name='' WHERE company_key='alpha labs zeta'")
        self.assertIn("alpha labs zeta", [entry["company"] for entry in apply_sensitive.list_entries(self.conn, USER)])

    def test_the_company_name_column_is_added_once_and_repairs_a_database_that_lacks_it(self):
        from opportunity_app import schema

        migration = REPO / "migrations" / "0046_apply_sensitive_company_name.sql"
        self.assertIn("0046_apply_sensitive_company_name.sql", {row[0] for row in self.conn.execute("SELECT name FROM schema_migrations")})
        schema._apply_apply_sensitive_company_name(self.conn, migration.read_text(encoding="utf-8"))  # a second run changes nothing
        with self.conn:
            self.conn.execute("ALTER TABLE apply_sensitive_answers DROP COLUMN company_name")
        schema._apply_apply_sensitive_company_name(self.conn, migration.read_text(encoding="utf-8"))
        columns = {row[1] for row in self.conn.execute("PRAGMA table_info(apply_sensitive_answers)")}
        self.assertIn("company_name", columns)

    def test_an_entry_says_how_many_of_the_students_roles_its_company_names(self):
        self.allow("work_authorization")
        self.opportunity("job-zephyr-1", company="Zephyr Robotics")
        self.opportunity("job-zephyr-2", company="Zephyr Robotics, Inc.")
        self.opportunity("job-other", company="Beta Labs")
        short = self.add(category="work_authorization", question=AUTH, answer="Yes", company="Zephyr")
        self.assertEqual(short["matched_roles"], 0, "a short name never matches the role's key, and the entry says so")
        full = self.add(category="work_authorization", question=f"{AUTH} (for this role)", answer="No", company="ZEPHYR robotics")
        self.assertEqual(full["matched_roles"], 2)
        anyone = self.add(category="work_authorization", question=f"{AUTH} Again", answer="Yes")
        self.assertIsNone(anyone["matched_roles"])
        self.assertEqual({entry["question"]: entry["matched_roles"] for entry in apply_sensitive.list_entries(self.conn, USER)},
                         {AUTH: 0, f"{AUTH} (for this role)": 2, f"{AUTH} Again": None})

    def test_a_question_that_depends_on_its_company_is_never_saved_for_any_company(self):
        self.allow("sponsorship")
        self.refused("this company only", category="sponsorship", question=f"{SPONSOR} / If yes, please explain", answer="x", company_only=True)
        self.assertEqual(self.add(category="sponsorship", question=f"{SPONSOR} / If yes, please explain", answer="x", company_only=True,
                                  company=COMPANY)["company"], "Example Robotics")


class EeoTests(StoreCase):
    def test_only_a_decline_answer_is_ever_stored_for_an_eeo_question(self):
        self.allow(*apply_sensitive.EEO_CATEGORIES)
        for category, question, label in (
            ("eeo_gender", GENDER, "Decline To Self Identify"), ("eeo_hispanic", "Are you Hispanic/Latino?", "Decline To Self Identify"),
            ("eeo_race", "Race", "Decline To Self Identify"), ("eeo_veteran", "Veteran Status", "I don't wish to answer"),
            ("eeo_disability", "Disability Status", "I do not want to answer"), ("eeo_gender", "What is your gender?", "Prefer not to say"),
        ):
            with self.subTest(category=category, label=label):
                entry = self.add(category=category, question=question, answer=label, company="Example Robotics")
                self.assertEqual((entry["answer"], entry["answer_kind"]), (label, "option"))

    def test_any_other_value_is_refused_by_the_service_not_just_the_form(self):
        self.allow(*apply_sensitive.EEO_CATEGORIES)
        for label in ("Male", "Female", "Yes", "No", "Hispanic or Latino", "I am not a protected veteran", "No, I do not have a disability",
                      "Two or more races", "Decline To Self Identify as female", "Yes, and I do not wish to say more", "  ", "Non-binary"):
            with self.subTest(label=label), self.assertRaises(StoreRefused) as caught:
                self.add(category="eeo_gender", question=GENDER, answer=label)
            self.assertRegex(str(caught.exception), r"only a decline|Give the answer")
        for kind in ("options", "text"):
            with self.subTest(kind=kind), self.assertRaises(StoreRefused):
                self.add(category="eeo_gender", question=GENDER, answer=DECLINE, answer_kind=kind)
        self.assertEqual(self.rows(), [], "nothing demographic was stored")

    def test_no_stored_row_can_hold_a_demographic_value_after_every_write_above(self):
        self.allow(*apply_sensitive.EEO_CATEGORIES)
        for label in ("Female", DECLINE, "Asian", "I don't wish to answer"):
            try:
                self.add(category="eeo_race", question="Race", answer=label)
            except StoreRefused:
                pass
        self.assertTrue(all(apply_sensitive.is_decline(row["answer"]) for row in self.rows() if row["category"].startswith("eeo_")))

    def test_the_decline_list_matches_the_whole_label_only(self):
        for label in ("Decline To Self Identify", "decline to self-identify", "I decline to self-identify", "I don't wish to answer",
                      "I DO NOT WISH TO ANSWER", "I do not want to answer", "Prefer not to say", "I prefer not to answer"):
            self.assertTrue(apply_sensitive.is_decline(label), label)
        for label in ("", "Yes", "Male", "Decline", "I decline to self-identify as a veteran", "I do not wish to answer, I am a veteran",
                      "Not a veteran", "I don't know", None):
            self.assertFalse(apply_sensitive.is_decline(label), label)


class NeverStoredTests(StoreCase):
    def test_export_control_and_salary_are_refused_even_when_named_as_a_permitted_category(self):
        self.allow("work_authorization", "sponsorship", "age_18", "consent")
        self.refused("never answers export control", category="export_control", question="Are you a U.S. person?", answer="Yes")
        self.refused("never answers salary", category="salary", question="What are your salary expectations?", answer="90000")
        self.refused("never stores", category="uncategorized", question="What is your age?", answer="21")
        self.refused("Unknown kind", category="favorite_color", question="Pick one", answer="blue")

    def test_the_wording_decides_too_so_a_citizenship_question_is_not_stored_as_work_authorization(self):
        self.allow("work_authorization", "sponsorship", "age_18", "acknowledgment", "consent")
        for category, question, needle in (
            ("work_authorization", "Are you a U.S. citizen or authorized to work in the U.S.?", "export control"),
            ("work_authorization", "Do you hold an active security clearance?", "export control"),
            ("sponsorship", "What are your salary expectations?", "salary"),
            ("age_18", "What is your age?", "personal question"),
            ("age_18", "What is your date of birth?", "personal question"),
            ("sponsorship", "Have you ever been convicted of a felony?", "personal question"),
            ("acknowledgment", "I certify that I have no criminal record and am not bound by a non-compete agreement", "personal question"),
            ("consent", "I consent to a background check", "personal question"),
        ):
            with self.subTest(question=question):
                self.refused(needle, category=category, question=question, answer="Yes")
        self.assertEqual(self.rows(), [])

    def test_an_eighteen_or_older_question_is_stored_and_a_plain_age_question_is_not(self):
        self.allow("age_18")
        self.assertEqual(self.add(category="age_18", question="Are you at least 18 years of age?", answer="Yes")["category"], "age_18")
        self.refused("personal question", category="age_18", question="What is your age?", answer="Yes")


class StatementTests(StoreCase):
    def test_a_statement_is_stored_word_for_word_as_ticked_and_a_changed_word_is_a_miss(self):
        self.allow("acknowledgment", "consent")
        # Every statement is kept for one company, even a plain "what I wrote is true" (D9 B, spec 5.4 "As built").
        self.refused("never for any company", category="acknowledgment", question=ACCURATE, answer="checked")
        entry = self.add(category="acknowledgment", question=ACCURATE, answer="checked", company="Example Robotics")
        row = self.rows()[0]
        self.assertEqual((row["answer_kind"], row["answer"], row["question_text"], row["company_key"]), ("checkbox", "checked", ACCURATE, "example robotics"))
        self.assertFalse(entry["any_company"])
        found = apply_sensitive.lookup(self.conn, USER, category="acknowledgment", question_key=question_key(ACCURATE), company_key="example robotics", mode="submit")
        self.assertEqual((found["answer_kind"], found["answer"]), ("checkbox", "checked"))
        for changed in (ACCURATE + " today", ACCURATE.replace("accurate", "correct"), "I certify the information I provided is accurate"):
            self.assertIsNone(apply_sensitive.lookup(self.conn, USER, category="acknowledgment", question_key=question_key(changed), company_key="example robotics", mode="submit"), changed)
        self.refused("stored only as ticked", category="acknowledgment", question=ACCURATE, answer="No")
        self.refused("whole statement", category="acknowledgment", question="I agree", answer="checked")

    def test_a_statement_that_reads_or_links_a_document_is_saved_for_one_company_never_any(self):
        self.allow("acknowledgment", "consent")
        for question in (PRIVACY, "I acknowledge receipt of the Example Robotics applicant notice", "I have read and agree to the Candidate Privacy Statement",
                         "I agree to the Terms and Conditions", f"I agree to the terms at {NOTICE_URL}"):
            with self.subTest(question=question):
                self.refused("never for any company", category="acknowledgment", question=question, answer="checked")
        self.refused("never for any company", category="acknowledgment", question="I certify that all of this is true", answer="checked", links=[NOTICE_URL])
        self.assertEqual(self.rows(), [])
        saved = self.add(category="acknowledgment", question=PRIVACY, answer="checked", company="Example Robotics, Inc.", links=[NOTICE_URL])
        self.assertEqual((saved["company"], saved["any_company"], saved["links"]), ("Example Robotics, Inc.", False, [NOTICE_URL]))
        self.assertEqual(self.rows()[0]["company_key"], "example robotics")

    def test_the_addresses_a_statement_points_to_are_kept_and_only_web_addresses_are(self):
        self.allow("acknowledgment")
        pasted = f'I have read the <a href="{NOTICE_URL}?x=1&amp;y=2">privacy notice</a> and {NOTICE_URL}/terms.'
        saved = self.add(category="acknowledgment", question=pasted, answer="checked", company="Example Robotics", links=["https://example-robotics.test/extra"])
        self.assertEqual(saved["links"], ["https://example-robotics.test/extra", f"{NOTICE_URL}?x=1&y=2", f"{NOTICE_URL}/terms"])
        for bad in ("javascript:alert(1)", "ftp://example.test/notice", "not a link", "https://" + "a" * 600):
            with self.subTest(link=bad):
                self.refused("full http or https address", category="acknowledgment", question=PRIVACY, answer="checked", company="Example Robotics", links=[bad])
        self.refused("too many", category="acknowledgment", question=PRIVACY, answer="checked", company="Example Robotics",
                     links=[f"https://example.test/{n}" for n in range(9)])
        self.assertEqual(apply_sensitive.links_in("no links here", None, "see http://example.test/a."), ("http://example.test/a",))
        self.assertTrue(apply_sensitive.cites_document("I have read the notice"))
        self.assertFalse(apply_sensitive.cites_document("I certify that all of this is true"), "the word test alone; a statement is kept for one company either way")

    def test_a_consent_statement_is_stored_the_same_way(self):
        self.allow("consent")
        statement = "I consent to Example Robotics storing my application data for 365 days"
        # A consent agrees to one employer's own terms, and the app cannot prove it names no document: one company only (broad net).
        self.refused("never for any company", category="consent", question=statement, answer="checked")
        saved = self.add(category="consent", question=statement, answer="checked", company="Example Robotics")
        self.assertEqual((saved["category"], saved["answer"], saved["any_company"]), ("consent", "checked", False))


class LookupTests(StoreCase):
    def ask(self, category="work_authorization", question=AUTH, company="example robotics", mode="submit"):
        return apply_sensitive.lookup(self.conn, USER, category=category, question_key=question_key(question), company_key=company, mode=mode)

    def test_it_matches_the_exact_key_category_and_company_and_nothing_else(self):
        self.allow("work_authorization", "sponsorship")
        self.add(category="work_authorization", question=AUTH, answer="Yes", company="Example Robotics")
        self.assertEqual(self.ask()["answer"], "Yes")
        self.assertIsNone(self.ask(company="orbit systems"), "another company's entry is not this company's")
        self.assertIsNone(self.ask(category="sponsorship"), "the category is part of the match")
        self.assertIsNone(self.ask(question=AUTH + " Please answer"), "the key is exact")
        self.assertIsNone(self.ask(company=""), "no company to compare is not everyone")
        self.assertIsNone(apply_sensitive.lookup(self.conn, "someone-else", category="work_authorization", question_key=question_key(AUTH),
                                                 company_key="example robotics", mode="submit"))

    def test_an_entry_for_any_company_answers_every_company_and_this_companys_own_entry_wins(self):
        self.allow("work_authorization")
        self.add(category="work_authorization", question=AUTH, answer="Yes")
        self.assertEqual(self.ask(company="orbit systems")["answer"], "Yes")
        self.assertEqual(self.ask(company="")["answer"], "Yes")
        self.add(category="work_authorization", question=AUTH, answer="No", company="Example Robotics")
        self.assertEqual(self.ask()["answer"], "No")
        self.assertEqual(self.ask(company="orbit systems")["answer"], "Yes")

    def test_the_consent_scope_must_cover_the_mode(self):
        self.allow("work_authorization")
        self.add(category="work_authorization", question=AUTH, answer="Yes")
        for mode in ("check", "rehearse", "handoff", "submit"):
            self.assertIsNotNone(self.ask(mode=mode), mode)
        self.assertIsNone(self.ask(mode="unattended"), "unattended runs need the separate, later consent")
        with self.conn:
            self.conn.execute("UPDATE apply_sensitive_answers SET consent_scope='unattended'")
        for mode in ("submit", "unattended"):
            self.assertIsNotNone(self.ask(mode=mode), mode)

    def test_an_entry_with_no_consent_time_or_a_category_that_is_never_stored_is_not_used(self):
        self.allow("work_authorization")
        self.add(category="work_authorization", question=AUTH, answer="Yes")
        with self.conn:
            self.conn.execute("UPDATE apply_sensitive_answers SET consented_at=''")
        self.assertIsNone(self.ask())
        stamp = self.base.isoformat()
        with self.conn:
            self.conn.execute("DELETE FROM apply_sensitive_answers")
            for category, question in (("salary", "What are your salary expectations?"), ("export_control", "Are you a U.S. person?")):
                self.conn.execute(
                    "INSERT INTO apply_sensitive_answers(id, user_id, category, question_text, question_key, question_hash, answer_kind, answer, company_key, "
                    "consent_scope, consented_at, created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?, 'option', 'Yes', '', 'confirmed', ?, ?, ?)",
                    (f"s-{category}", USER, category, question, question_key(question),
                     __import__("hashlib").sha256(question_key(question).encode()).hexdigest(), stamp, stamp, stamp))
        self.assertIsNone(self.ask(category="salary", question="What are your salary expectations?"), "the table allows salary; the store does not")
        self.assertIsNone(self.ask(category="export_control", question="Are you a U.S. person?"))

    def test_a_statement_that_cites_a_document_is_ignored_for_any_company_even_if_a_row_says_so(self):
        self.allow("acknowledgment")
        self.add(category="acknowledgment", question=PRIVACY, answer="checked", company="Example Robotics")
        with self.conn:
            self.conn.execute("UPDATE apply_sensitive_answers SET company_key=''")
        self.assertIsNone(self.ask(category="acknowledgment", question=PRIVACY), "one employer's notice is not another's")

    def test_reading_writes_nothing(self):
        self.allow("work_authorization")
        self.add(category="work_authorization", question=AUTH, answer="Yes")
        before = self.rows()
        for _ in range(3):
            self.ask()
        self.assertEqual(self.rows(), before, "last_used_at and updated_at are unchanged: the check and the plan write nothing")

    def test_deleting_removes_it_at_once_and_only_for_its_owner(self):
        self.allow("work_authorization")
        entry = self.add(category="work_authorization", question=AUTH, answer="Yes")
        self.assertFalse(apply_sensitive.delete_entry(self.conn, "someone-else", entry["id"]))
        self.assertIsNotNone(self.ask())
        self.assertTrue(apply_sensitive.delete_entry(self.conn, USER, entry["id"]))
        self.assertIsNone(self.ask())
        self.assertFalse(apply_sensitive.delete_entry(self.conn, USER, entry["id"]))

    def test_the_settings_list_shows_each_entry_and_whether_its_kind_is_switched_on(self):
        self.allow("work_authorization", "acknowledgment")
        self.add(category="work_authorization", question=AUTH, answer="Yes")
        self.add(category="acknowledgment", question=PRIVACY, answer="checked", company="Example Robotics", links=[NOTICE_URL])
        self.allow("work_authorization")
        listed = apply_sensitive.list_entries(self.conn, USER)
        self.assertEqual([(item["category"], item["switched_on"]) for item in listed], [("work_authorization", True), ("acknowledgment", False)])
        self.assertEqual(listed[1]["links"], [NOTICE_URL])
        self.assertEqual(apply_sensitive.list_entries(self.conn, "someone-else"), [])


class PlanFromTheStoreTests(StoreCase):
    """Rows of section 7.5 that used to run against a stand-in now run against the real store."""

    def sources(self):
        src = apply_policy.sources_for(self.conn, USER, "job-a", company=COMPANY, key=KEY)
        src.facts, src.resume, src.cover_letter = copy.deepcopy(FACTS), dict(RESUME_OK), dict(LETTER_NONE)
        return src

    def plan(self, fields, mode="submit", company=COMPANY):
        return apply_helpers.plan(fields, self.sources(), mode, company=company)

    def assert_needs(self, fields, kind, key, company=COMPANY):
        submit, handoff = self.plan(fields, "submit", company), self.plan(fields, "handoff", company)
        self.assertEqual(apply_helpers.kinds(submit)[key], kind)
        self.assertFalse(submit.ready)
        self.assertEqual((handoff.get(key).disposition, handoff.get(key).source.kind, handoff.get(key).value), ("left_for_you", "none", None))
        return submit

    def test_rows_4_to_7_work_authorization_comes_from_the_store_only(self):
        field = F("q", AUTH, SINGLE, options=("Yes", "No"))
        # 5: nothing is switched on (D5 A), so even a stored entry is left for the student.
        self.allow("work_authorization")
        self.add(category="work_authorization", question=AUTH, answer="Yes")
        self.allow()
        self.assert_needs(BASE + [field], "sensitive_not_allowed", "q")
        # 4: switched on, entry present.
        self.allow("work_authorization")
        ready = self.plan(BASE + [field])
        self.assertEqual(ready.status, "ready")
        got = ready.get("q")
        self.assertEqual((got.value, got.source.kind, got.disposition, got.sensitive), ("Yes", "sensitive", "fill", "work_authorization"))
        self.assertRegex(got.source.label, r"^Sensitive answer you added \d{4}-\d\d-\d\d$")
        self.assertEqual(self.plan(BASE + [field], "handoff").get("q").disposition, "fill")
        # A rehearsal defers a sensitive field: the option is checked, nothing is put in the page.
        self.assertEqual(self.plan(BASE + [field], "rehearse").get("q").disposition, "deferred")
        # Another kind switched on is not this kind.
        self.allow("sponsorship")
        self.assert_needs(BASE + [field], "sensitive_not_allowed", "q")
        # 7: an entry for another company is not this company's.
        self.allow("work_authorization")
        with self.conn:
            self.conn.execute("DELETE FROM apply_sensitive_answers")
        self.add(category="work_authorization", question=AUTH, answer="Yes", company=OTHER)
        self.assert_needs(BASE + [field], "sensitive_missing", "q")

    def test_row_6_an_answer_in_the_library_only_is_not_the_store(self):
        field = F("q", AUTH, SINGLE, options=("Yes", "No"))
        self.allow("work_authorization")
        src = self.sources()
        src.answers = [apply_helpers.answer(AUTH, "Yes")]
        result = apply_helpers.plan(BASE + [field], src, "submit")
        self.assertEqual(apply_helpers.kinds(result)["q"], "sensitive_missing")
        self.assertIsNone(result.get("q").value)

    def test_the_stored_answer_must_be_one_of_this_forms_options_by_label(self):
        field = F("q", AUTH, SINGLE, options=("Yes", "No"))
        self.allow("work_authorization")
        self.add(category="work_authorization", question=AUTH, answer="Absolutely")
        self.assert_needs(BASE + [field], "sensitive_mismatch", "q")

    def test_row_10_and_38_salary_and_citizenship_are_not_answered_whatever_is_in_the_setting_or_the_table(self):
        salary = F("q", "What are your salary expectations?")
        citizen = F("c", "Are you a U.S. citizen or authorized to work in the U.S.?", SINGLE, options=("Yes", "No"))
        self.allow("work_authorization")
        with self.conn:
            self.conn.execute("UPDATE user_settings SET value='salary,export_control,work_authorization' WHERE key='apply_sensitive_categories'")
        self.assert_needs(BASE + [salary], "sensitive_not_allowed", "q")
        self.assertEqual(self.plan(BASE + [citizen]).get("c").sensitive, "export_control")
        self.assert_needs(BASE + [citizen], "sensitive_not_allowed", "c")

    def test_rows_8_and_9_a_box_is_ticked_only_on_an_exact_statement_for_this_company(self):
        box = F("q", PRIVACY, MULTI, options=(PRIVACY,))
        self.allow("acknowledgment")
        self.add(category="acknowledgment", question=apply_classify.statement_of(box, "checkbox"), answer="checked", company="Example Robotics")
        ready = self.plan(BASE + [box])
        self.assertEqual(ready.status, "ready")
        got = ready.get("q")
        self.assertEqual((got.control, got.value, got.sensitive, got.source.kind, got.disposition), ("checkbox", True, "acknowledgment", "sensitive", "fill"))
        self.assertEqual(got.source.label, "Your acknowledgment for Example Robotics")
        self.assertEqual(self.plan(BASE + [box], "handoff").get("q").disposition, "fill", "ticked before the window is handed over")
        changed = F("q", "I have read the Example Robotics privacy notices", MULTI, options=("I have read the Example Robotics privacy notices",))
        self.assert_needs(BASE + [changed], "sensitive_missing", "q")
        self.assert_needs(BASE + [box], "sensitive_missing", "q", company=OTHER)

    def test_the_preview_shows_the_address_the_statement_links_to_and_a_changed_address_is_not_agreed_to(self):
        described = dict(name="q", label="Candidate Privacy Notice", required=True, type=MULTI, options=("I have read and agree to the notice",))
        box =apply_policy.SchemaField(**described, description=f'<p>Read it at <a href="{NOTICE_URL}">the notice</a>.</p>')
        self.allow("acknowledgment")
        key_text = apply_classify.statement_of(box, "checkbox")
        self.assertIn("I have read and agree to the notice", key_text)
        self.add(category="acknowledgment", question=key_text, answer="checked", company="Example Robotics", links=[NOTICE_URL])
        got = self.plan(BASE + [box]).get("q")
        self.assertEqual((got.value, got.source.links), (True, (NOTICE_URL,)))
        self.assertEqual(apply_policy.plan_entries(self.plan(BASE + [box]))[-1]["source"]["links"], [NOTICE_URL])
        moved = apply_policy.SchemaField(**described, description='<p>Read it at <a href="https://example-robotics.test/legal/privacy-2027">the notice</a>.</p>')
        self.assertEqual(self.plan(BASE + [moved]).get("q").problem_kind, "sensitive_mismatch")
        self.assertIn("not the one you agreed to", self.plan(BASE + [moved]).get("q").problem)

    def test_a_plain_certification_ticks_at_the_company_it_was_saved_for_and_at_no_other(self):
        box = F("q", ACCURATE, MULTI, options=(ACCURATE,))
        self.allow("acknowledgment")
        statement = apply_classify.statement_of(box, "checkbox")
        self.refused("never for any company", category="acknowledgment", question=statement, answer="checked")
        self.add(category="acknowledgment", question=statement, answer="checked", company=COMPANY)
        got = self.plan(BASE + [box], company=COMPANY).get("q")
        self.assertEqual((got.value, got.source.label), (True, f"Your acknowledgment for {COMPANY}"))
        self.assertTrue(got.company_only)
        other = self.plan(BASE + [box], company=OTHER).get("q")
        self.assertEqual((other.value, other.problem_kind), (None, "sensitive_missing"))
        # A row for any company put there some other way (an older version) is used at no company.
        with self.conn:
            self.conn.execute("UPDATE apply_sensitive_answers SET company_key=''")
        for company in (COMPANY, OTHER):
            legacy = self.plan(BASE + [box], company=company).get("q")
            self.assertEqual((legacy.value, legacy.problem_kind), (None, "sensitive_missing"), company)

    def test_an_eeo_decline_fills_the_form_label_and_a_different_label_needs_its_own_entry(self):
        gender = F("gender", GENDER, SINGLE, required=False, options=("Male", "Female", DECLINE), section="compliance")
        veteran = F("veteran_status", "Veteran Status", SINGLE, required=True, options=("I am not a protected veteran", "I don't wish to answer"), section="compliance")
        self.allow(*apply_sensitive.EEO_CATEGORIES)
        self.add(category="eeo_gender", question=GENDER, answer=DECLINE)
        self.add(category="eeo_veteran", question="Veteran Status", answer="Decline To Self Identify")
        result = self.plan(BASE + [gender, veteran])
        self.assertEqual((result.get("gender").value, result.get("gender").disposition), (DECLINE, "fill"))
        self.assertEqual(result.get("veteran_status").problem_kind, "sensitive_mismatch", "this form calls the decline something else")
        self.add(category="eeo_veteran", question="Veteran Status", answer="I don't wish to answer", company="Example Robotics")
        again = self.plan(BASE + [gender, veteran])
        self.assertEqual(again.get("veteran_status").value, "I don't wish to answer", "this company's own entry wins over the any-company one")
        self.assertEqual(again.status, "ready")

    def test_a_demographic_section_question_has_no_name_so_it_is_never_filled_even_with_entries(self):
        question = F("question_9", "What is your gender identity?", SINGLE, required=True, options=("Woman", "Man", "Decline To Self Identify"), section="demographic")
        self.allow(*apply_sensitive.EEO_CATEGORIES)
        self.add(category="eeo_gender", question="What is your gender identity?", answer=DECLINE)
        result = self.plan(BASE + [question])
        self.assertEqual((result.get("question_9").sensitive, result.get("question_9").problem_kind), ("uncategorized", "sensitive_never"))

    def test_building_a_plan_writes_nothing(self):
        field = F("q", AUTH, SINGLE, options=("Yes", "No"))
        self.allow("work_authorization")
        self.add(category="work_authorization", question=AUTH, answer="Yes")
        before = self.rows()
        self.plan(BASE + [field])
        self.plan(BASE + [field], "handoff")
        self.assertEqual(self.rows(), before)

    def test_deleting_an_entry_changes_the_plan_hash_so_a_plan_that_used_it_is_not_the_one_approved(self):
        field = F("q", AUTH, SINGLE, options=("Yes", "No"))
        self.allow("work_authorization")
        entry = self.add(category="work_authorization", question=AUTH, answer="Yes")
        approved = self.plan(BASE + [field]).plan_hash
        self.assertEqual(self.plan(BASE + [field]).plan_hash, approved)
        apply_sensitive.delete_entry(self.conn, USER, entry["id"])
        self.assertNotEqual(self.plan(BASE + [field]).plan_hash, approved)
        self.add(category="work_authorization", question=AUTH, answer="No")
        self.assertNotEqual(self.plan(BASE + [field]).plan_hash, approved)

    def test_the_stored_run_record_holds_no_value_for_a_sensitive_field(self):
        field = F("q", AUTH, SINGLE, options=("Yes", "No"))
        self.allow("work_authorization")
        self.add(category="work_authorization", question=AUTH, answer="Yes")
        entries = apply_policy.plan_entries(self.plan(BASE + [field]))
        held = next(item for item in entries if item["key"] == "q")
        self.assertEqual(held["source"]["kind"], "sensitive")
        self.assertRegex(held["value_mac"], r"^[0-9a-f]{64}$")
        self.assertNotIn("Yes", [held["value_mac"], held["source"]["ref"], held["problem"]])
        self.assertFalse({"value", "answer"} & set(held), "no field holds the answer")


DECLINES = (
    ("eeo_gender", "gender", GENDER, DECLINE),
    ("eeo_hispanic", "hispanic_ethnicity", "Are you Hispanic/Latino?", DECLINE),
    ("eeo_veteran", "veteran_status", "Veteran Status", "I don't wish to answer"),
    ("eeo_disability", "disability_status", "Disability Status", "I do not want to answer"),
)


class KeyAndCategoryRuleTests(StoreCase):
    """Review round one: what a stored answer is keyed on, which kind it may be filed under, and where it may be reused."""

    sources = PlanFromTheStoreTests.sources
    plan = PlanFromTheStoreTests.plan
    assert_needs = PlanFromTheStoreTests.assert_needs

    def box(self, label, option, description="", name="q"):
        return SchemaField(name=name, label=label, required=True, type=MULTI, options=(option,), description=description)

    # --- The EEOC fields are found by their own names (7.3 step 2) ---

    def test_an_eeoc_decline_typed_as_the_form_shows_it_fills_the_live_fields_at_every_company(self):
        schema = apply_policy.parse_schema(fixture_json("schema_new.json"))
        self.allow(*apply_sensitive.EEO_CATEGORIES)
        for category, _name, question, decline in DECLINES:
            self.add(category=category, question=question, answer=decline)
        for company in (COMPANY, OTHER):
            result = self.plan(schema, company=company)
            for _category, name, question, decline in DECLINES:
                with self.subTest(company=company, field=name):
                    got = result.get(name)
                    self.assertEqual((got.disposition, got.value, got.statement, got.company_only), ("fill", decline, question, False))

    def test_an_eeoc_field_is_keyed_on_its_own_label_whatever_question_comes_before_it(self):
        salary_first = {
            "questions": [{"label": "What are your salary expectations?", "required": False, "fields": [{"name": "question_9", "type": "input_text", "values": []}]}],
            "compliance": [{"type": "eeoc", "questions": [{"label": GENDER, "required": False, "fields": [
                {"name": "gender", "type": SINGLE, "values": [{"label": "Male", "value": 1}, {"label": DECLINE, "value": 3}]}]}]}],
        }
        schema = apply_policy.parse_schema(salary_first)
        self.assertEqual(next(item for item in schema if item.name == "gender").parent, "", "an EEOC field continues no other question")
        self.allow("eeo_gender")
        entry = self.plan(schema).get("gender")
        self.assertEqual((entry.answer_key, entry.context_dependent, entry.company_only), (GENDER, False, False))
        form = apply_preflight._sensitive_form(entry, "sensitive_missing")
        self.assertFalse(form["company_only"], "one decline serves every company")
        self.add(category=entry.sensitive, question=entry.statement, answer=DECLINE)
        self.assertEqual(self.plan(schema, company=OTHER).get("gender").value, DECLINE)

    # --- What a question may be filed under ---

    def test_a_question_that_reads_as_another_kind_is_refused_and_nothing_is_written(self):
        self.allow("work_authorization", "sponsorship", "acknowledgment", "age_18")
        for category, question, answer, needle in (
            ("work_authorization", "What is your race?", "Asian", "voluntary self-identification"),
            ("work_authorization", "Gender", "Female", "voluntary self-identification"),
            ("work_authorization", "Do you have a disability?", "Yes, I have a disability", "voluntary self-identification"),
            ("sponsorship", "Are you Hispanic or Latino?", "Yes", "voluntary self-identification"),
            ("work_authorization", "Are you a protected veteran?", "I am a protected veteran", "voluntary self-identification"),
            ("acknowledgment", "I certify that I am a protected veteran", "checked", "voluntary self-identification"),
            ("age_18", "Are you authorized to work in the United States without sponsorship?", "Yes", "visa sponsorship"),
        ):
            with self.subTest(category=category, question=question):
                self.refused(needle, category=category, question=question, answer=answer)
        self.assertEqual(self.rows(), [], "no row holds what was refused, so the account export has none either")

    def test_a_consent_worded_as_an_acknowledgment_is_still_stored_as_a_consent(self):
        self.allow("consent")
        saved = self.add(category="consent", question="I acknowledge and consent to Example Robotics keeping my data", answer="checked", company=COMPANY)
        self.assertEqual(saved["category"], "consent")

    def test_a_single_box_that_states_18_or_older_or_work_authorization_is_stored_and_ticked(self):
        self.allow("age_18", "work_authorization", "eeo_gender")
        boxes = (
            ("age_18", "Confirmation", "I confirm that I am at least 18 years of age"),
            ("work_authorization", "Declaration", "I certify that I am legally authorized to work in the United States"),
        )
        for category, heading, option in boxes:
            with self.subTest(category=category):
                field = self.box(heading, option, name="question_5")
                # The heading is the question this box answers, so it is part of what is matched, however long the option is.
                statement = f"{heading} {option}"
                before = self.plan(BASE + [field]).get("question_5")
                self.assertEqual((before.sensitive, before.problem_kind, before.statement), (category, "sensitive_missing", statement))
                form = apply_preflight._sensitive_form(before, "sensitive_missing")
                self.assertEqual((form["control"], form["statement"], form["company_only"]), ("checkbox", statement, True))
                self.add(category=category, question=before.statement, answer="checked", answer_kind="checkbox", links=before.links, company=COMPANY)
                after = self.plan(BASE + [field]).get("question_5")
                self.assertEqual((after.value, after.source.kind, after.disposition), (True, "sensitive", "fill"))
        self.refused("not stored", category="eeo_gender", question=GENDER, answer="checked", answer_kind="checkbox")

    # --- The company rule holds on every route ---

    def test_a_question_that_depends_on_its_company_is_never_kept_for_any_company(self):
        self.allow("sponsorship", "work_authorization", "acknowledgment")
        prior = "Has this company previously filed an H-1B petition on your behalf?"
        for category, question, answer in (
            ("sponsorship", prior, "Yes"), ("work_authorization", "Work Authorization", "Yes"),
            ("acknowledgment", "I certify that I have not previously applied to this company in the last six months", "checked"),
        ):
            with self.subTest(question=question):
                self.refused("this company only", category=category, question=question, answer=answer)
        self.assertEqual(self.rows(), [])
        # A row for any company put there some other way is not read for such a question either.
        field = F("q", prior, SINGLE, options=("Yes", "No"))
        self.add(category="sponsorship", question=prior, answer="Yes", company="Example Robotics")
        with self.conn:
            self.conn.execute("UPDATE apply_sensitive_answers SET company_key=''")
        for company in (COMPANY, OTHER):
            self.assert_needs(BASE + [field], "sensitive_missing", "q", company=company)

    # --- A statement is its own text, and its documents ---

    def test_a_statement_saved_for_any_company_is_not_ticked_where_its_box_links_a_document(self):
        self.allow("acknowledgment")
        plain = self.box("Certification", ACCURATE)
        self.add(category="acknowledgment", question=apply_classify.statement_of(plain, "checkbox"), answer="checked", company=COMPANY)
        self.assertEqual(self.plan(BASE + [plain], company=COMPANY).get("q").value, True)
        self.assertEqual(self.plan(BASE + [plain], company=OTHER).get("q").value, None)
        linked = self.box("Certification", ACCURATE, '<p>Details at <a href="https://orbit.test/legal/attestation">this page</a>.</p>')
        # A row for the linked box's whole text, saved for any company where the same words linked nothing (a legacy row: the store now
        # refuses any-company for it, so it is put there by hand).
        self.add(category="acknowledgment", question=apply_classify.statement_of(linked, "checkbox"), answer="checked", company=COMPANY)
        with self.conn:
            self.conn.execute("UPDATE apply_sensitive_answers SET company_key='' WHERE question_text=?", (apply_classify.statement_of(linked, "checkbox"),))
        got = self.plan(BASE + [linked], company=OTHER).get("q")
        self.assertEqual((got.problem_kind, got.source.kind, got.value), ("sensitive_missing", "none", None))
        self.assertTrue(apply_preflight._sensitive_form(got, "sensitive_missing")["company_only"])

    def test_a_company_statement_is_ticked_only_when_the_forms_links_are_the_ones_it_was_saved_with(self):
        anchored = self.box("Candidate Privacy Notice", "I have read and agree to the notice", f'<p>Read it at <a href="{NOTICE_URL}">the notice</a>.</p>')
        bare = self.box("Candidate Privacy Notice", "I have read and agree to the notice", "<p>Read it at the notice.</p>")
        text = apply_classify.statement_of(anchored, "checkbox")
        self.assertEqual(question_key(text), question_key(apply_classify.statement_of(bare, "checkbox")))
        self.allow("acknowledgment")
        self.add(category="acknowledgment", question=text, answer="checked", company="Example Robotics", links=[NOTICE_URL])
        self.assertEqual(self.plan(BASE + [anchored]).get("q").source.links, (NOTICE_URL,))
        got = self.plan(BASE + [bare]).get("q")
        self.assertEqual(got.problem_kind, "sensitive_mismatch", "saved with a link, the form now links none")
        self.assertIn("not the one you agreed to", got.problem)
        self.assertEqual(got.links, (), "the form's own addresses are shown, never the stored ones")
        self.add(category="acknowledgment", question=text, answer="checked", company="Example Robotics")
        self.assertEqual(self.plan(BASE + [bare]).get("q").value, True)
        self.assertEqual(self.plan(BASE + [anchored]).get("q").problem_kind, "sensitive_mismatch", "saved with none, the form now links one")

    def test_a_box_that_points_elsewhere_or_has_a_description_is_matched_on_all_its_text_for_one_company(self):
        heading = "Candidate application acknowledgment"
        first = "I consent to Example Robotics keeping my application for 1 year."
        second = "I agree to arbitrate every employment dispute and waive class actions."
        self.allow("acknowledgment")
        for option in ("Yes, I agree to the above terms", "I acknowledge and agree to the terms", "By checking this box I agree to the statement above", "I agree"):
            with self.subTest(option=option):
                one, two = self.box(heading, option, f"<p>{first}</p>"), self.box(heading, option, f"<p>{second}</p>")
                self.assertTrue(apply_classify.statement_needs_company(one, "checkbox"))
                self.assertIn(first, apply_classify.statement_of(one, "checkbox"))
                self.assertNotEqual(apply_classify.statement_of(one, "checkbox"), apply_classify.statement_of(two, "checkbox"))
                saved = self.add(category="acknowledgment", question=apply_classify.statement_of(one, "checkbox"), answer="checked", company="Example Robotics")
                self.assertEqual(self.plan(BASE + [one]).get("q").value, True)
                self.assert_needs(BASE + [two], "sensitive_missing", "q")
                self.assertTrue(apply_preflight._sensitive_form(self.plan(BASE + [two]).get("q"), "sensitive_missing")["company_only"])
                # The same words saved for any company are not read for a box like this, at any company.
                with self.conn:
                    self.conn.execute("UPDATE apply_sensitive_answers SET company_key='' WHERE id=?", (saved["id"],))
                for company in (COMPANY, OTHER):
                    self.assert_needs(BASE + [one], "sensitive_missing", "q", company=company)
                apply_sensitive.delete_entry(self.conn, USER, self.rows()[0]["id"])
        # A whole statement with nothing else on the box is still matched with its heading, and needs no one company.
        alone = self.box("Anything", ACCURATE)
        self.assertEqual((apply_classify.statement_of(alone, "checkbox"), apply_classify.statement_needs_company(alone, "checkbox")), (f"Anything {ACCURATE}", False))

    def test_a_statement_built_from_a_description_the_app_cut_is_left_for_the_student(self):
        listing = {"questions": [{"label": "Acknowledgment", "required": True, "description": "<p>" + "x" * 2300 + "</p>",
                                  "fields": [{"name": "question_1", "type": MULTI, "values": [{"label": "I agree", "value": 1}]}]}]}
        box = next(item for item in apply_policy.parse_schema(listing) if item.name == "question_1")
        self.assertTrue(box.description_cut)
        self.allow("acknowledgment")
        self.add(category="acknowledgment", question=apply_classify.statement_of(box, "checkbox"), answer="checked", company="Example Robotics")
        got = self.plan(BASE + [box]).get("question_1")
        self.assertEqual((got.problem_kind, got.value, got.source.kind, got.text_cut), ("sensitive_never", None, "none", True))
        self.assertIsNone(apply_preflight._sensitive_form(got, "sensitive_missing"))
        self.assertEqual(apply_preflight._sensitive_state(got, self.sources(), COMPANY), "")

    def test_a_yes_no_question_that_asks_for_agreement_is_filled_from_a_stored_statement(self):
        question = "I acknowledge that I have read the Example Robotics candidate privacy notice"
        self.allow("acknowledgment")
        field = F("q", question, SINGLE, options=("Yes", "No"))
        got = self.plan(BASE + [field]).get("q")
        self.assertEqual((got.sensitive, got.problem_kind), ("acknowledgment", "sensitive_missing"))
        self.add(category="acknowledgment", question=question, answer="Yes", answer_kind="option", company="Example Robotics")
        ready = self.plan(BASE + [field])
        self.assertEqual((ready.status, ready.get("q").value, ready.get("q").source.label), ("ready", "Yes", "Your acknowledgment for Example Robotics"))
        self.assertEqual(apply_preflight._sensitive_state(ready.get("q"), self.sources(), COMPANY), "", "nothing is left to ask")
        self.assert_needs(BASE + [F("q", question, SINGLE, options=("No",))], "sensitive_mismatch", "q")

    # --- Review round two ---

    def yes_no(self, label, description="", name="q", type=SINGLE, options=("Yes", "No")):
        return SchemaField(name=name, label=label, required=True, type=type, options=options, description=description)

    def test_a_yes_no_agreement_question_is_matched_on_its_description_and_kept_for_one_company(self):
        label = "Do you acknowledge and agree to the statement above?"
        first, second = "I consent to Example Robotics keeping my application for 1 year.", "I agree to arbitrate every employment dispute and waive class actions."
        one, two = self.yes_no(label, f"<p>{first}</p>"), self.yes_no(label, f"<p>{second}</p>")
        self.allow("acknowledgment")
        got = self.plan(BASE + [one]).get("q")
        self.assertEqual((got.sensitive, got.problem_kind, got.control), ("acknowledgment", "sensitive_missing", "select"))
        self.assertIn(first, got.statement, "the description holds the terms, so it is part of what is matched")
        form = apply_preflight._sensitive_form(got, "sensitive_missing")
        self.assertEqual((form["control"], form["company_only"]), ("select", True), "no tick for any company")
        saved = self.add(category="acknowledgment", question=got.statement, answer="Yes", answer_kind="option", company="Example Robotics")
        self.assertEqual(self.plan(BASE + [one]).get("q").value, "Yes")
        self.assert_needs(BASE + [two], "sensitive_missing", "q")
        # Saved for any company by some other route, it is still not read for a question that leans on text elsewhere.
        with self.conn:
            self.conn.execute("UPDATE apply_sensitive_answers SET company_key='' WHERE id=?", (saved["id"],))
        for company in (COMPANY, OTHER):
            self.assert_needs(BASE + [one], "sensitive_missing", "q", company=company)

    def test_a_yes_no_agreement_question_is_answered_only_where_its_links_are_the_stored_ones(self):
        label = "I acknowledge that I have read the candidate privacy notice"
        v1, v2 = "https://example.test/privacy-v1", "https://example.test/privacy-v2-new-terms"
        self.allow("acknowledgment")
        got = self.plan(BASE + [self.yes_no(label, f'<p>See <a href="{v1}">the notice</a>.</p>')]).get("q")
        self.assertEqual(got.links, (v1,))
        self.add(category="acknowledgment", question=got.statement, answer="Yes", answer_kind="option", company="Example Robotics", links=got.links)
        self.assertEqual(self.plan(BASE + [self.yes_no(label, f'<p>See <a href="{v1}">the notice</a>.</p>')]).get("q").value, "Yes")
        for other in (self.yes_no(label, f'<p>See <a href="{v2}">the notice</a>.</p>'), self.yes_no(label, "<p>See the notice.</p>")):
            changed = self.plan(BASE + [other]).get("q")
            self.assertEqual((changed.problem_kind, changed.value), ("sensitive_mismatch", None))
            self.assertIn("not the one you agreed to", changed.problem)

    def test_a_box_that_answers_a_question_about_the_student_is_matched_on_its_heading_as_well(self):
        option = "Yes, this is true for me right now"
        self.allow("work_authorization")
        us = self.box("Are you legally authorized to work in the United States?", option)
        canada = self.box("Are you legally authorized to work in Canada?", option)
        before = self.plan(BASE + [us]).get("q")
        # A tick box is kept for one company (spec 5.4 "As built"): the statement is matched on its heading, and never carries elsewhere.
        self.assertEqual((before.sensitive, before.statement, before.company_only), ("work_authorization", f"{us.label} {option}", True))
        self.refused("never for any company", category="work_authorization", question=before.statement, answer="checked", answer_kind="checkbox")
        self.add(category="work_authorization", question=before.statement, answer="checked", answer_kind="checkbox", company=COMPANY)
        self.assertEqual(self.plan(BASE + [us]).get("q").value, True)
        self.assert_needs(BASE + [us], "sensitive_missing", "q", company=OTHER)
        self.assert_needs(BASE + [canada], "sensitive_missing", "q")

    def test_an_acknowledgment_that_leans_on_the_heading_is_kept_for_one_company_and_matched_on_it(self):
        option = "I understand and accept this condition of employment"
        self.allow("acknowledgment")
        relocation, arbitration = self.box("Relocation requirement", option), self.box("Mandatory arbitration", option)
        self.assertTrue(apply_classify.statement_needs_company(relocation, "checkbox"))
        self.assertIn("Relocation requirement", apply_classify.statement_of(relocation, "checkbox"))
        self.add(category="acknowledgment", question=apply_classify.statement_of(relocation, "checkbox"), answer="checked", company="Example Robotics")
        self.assertEqual(self.plan(BASE + [relocation]).get("q").value, True)
        self.assert_needs(BASE + [arbitration], "sensitive_missing", "q")

    def test_an_acknowledgment_on_a_text_or_list_field_is_never_offered_or_filled(self):
        self.allow("acknowledgment", "consent")
        signature = F("q", "I certify that the information in this application is accurate. Type your full name as your signature", "input_text")
        several = self.yes_no("I acknowledge the following (select all that apply)", type=MULTI, options=("The privacy notice", "The code of conduct"))
        choice = self.yes_no("Do you acknowledge the code of conduct?", options=("I acknowledge", "I do not acknowledge"))
        for field in (signature, several, choice):
            with self.subTest(label=field.label):
                got = self.plan(BASE + [field]).get("q")
                self.assertEqual((got.sensitive, got.problem_kind, got.value), ("acknowledgment", "sensitive_never", None))
                self.assertIsNone(apply_preflight._sensitive_form(got, "sensitive_missing"))
                # Even a row stored for the same words, by another route, is not typed into it.
                with self.conn:
                    self.conn.execute("DELETE FROM apply_sensitive_answers")
                self.add(category="acknowledgment", question=field.label, answer="checked", company="Example Robotics")
                again = self.plan(BASE + [field]).get("q")
                self.assertEqual((again.problem_kind, again.value), ("sensitive_never", None))

    def test_a_kind_the_plan_took_from_the_options_or_the_heading_can_be_stored_but_a_demographic_value_never(self):
        self.allow("work_authorization", "sponsorship", "eeo_race", "eeo_hispanic", "acknowledgment")
        # The wording alone reads as a more restrictive kind than the plan's own: fine when the plan's category is authoritative.
        heading = "Visa Sponsorship / Work Authorization"
        self.refused("visa sponsorship", category="work_authorization", question=heading, answer="Yes")
        self.assertEqual(self.add(category="work_authorization", question=heading, answer="Yes", from_form=True)["category"], "work_authorization")
        # An EEO question keeps to the EEO kinds, in both directions, whoever says it is authoritative.
        race = "Race (including Hispanic or Latino)"
        self.assertEqual(self.add(category="eeo_race", question=race, answer=DECLINE)["category"], "eeo_race")
        for category, question, answer in (("work_authorization", "What is your race?", "Asian"), ("eeo_race", AUTH, DECLINE), ("acknowledgment", "Gender", "checked")):
            with self.subTest(category=category, question=question):
                self.refused("", category=category, question=question, answer=answer, from_form=True)
        self.assertEqual(len(self.rows()), 2)

    def test_the_apps_own_placeholder_is_never_a_stored_consent_statement(self):
        schema = apply_policy.parse_schema(fixture_json("schema_new.json"))
        gdpr = next(item for item in schema if item.name == "gdpr_consent_given")
        self.assertTrue(gdpr.label_from_page)
        self.allow("consent")
        self.refused("placeholder", category="consent", question=gdpr.label, answer="checked")
        stamp = self.base.isoformat()
        with self.conn:
            self.conn.execute(
                "INSERT INTO apply_sensitive_answers(id, user_id, category, question_text, question_key, question_hash, answer_kind, answer, company_key, "
                "consent_scope, consented_at, created_at, updated_at) VALUES('s-gdpr', ?, 'consent', ?, ?, ?, 'checkbox', 'checked', '', 'confirmed', ?, ?, ?)",
                (USER, gdpr.label, question_key(gdpr.label), __import__("hashlib").sha256(question_key(gdpr.label).encode()).hexdigest(), stamp, stamp, stamp))
        for company in (COMPANY, OTHER):
            got = self.plan(schema, company=company).get("gdpr_consent_given")
            self.assertEqual((got.problem_kind, got.value, got.source.kind), ("sensitive_never", None, "none"), company)


class LeftoverReviewTests(StoreCase):
    """The last review round: a statement is the whole visible statement, under the question it follows, for one company."""

    sources = PlanFromTheStoreTests.sources
    plan = PlanFromTheStoreTests.plan
    assert_needs = PlanFromTheStoreTests.assert_needs

    RELOCATION = "Relocation to Austin, TX is required within 30 days of the start date"
    ARBITRATION = "All employment disputes are resolved by binding individual arbitration, and class actions are waived"

    def box(self, label, option, description="", name="q", parent=""):
        return SchemaField(name=name, label=label, required=True, type=MULTI, options=(option,), description=description, parent=parent)

    def yes_no(self, label, description="", name="q", parent=""):
        return SchemaField(name=name, label=label, required=True, type=SINGLE, options=("Yes", "No"), description=description, parent=parent)

    def text(self, label, name="t"):
        return SchemaField(name=name, label=label, required=False, type="input_text")

    def test_a_long_generic_option_is_never_the_whole_statement(self):
        option = "I acknowledge and understand the information provided"
        self.assertGreaterEqual(len(option.split()), apply_classify._SPECIFIC_STATEMENT_WORDS, "long enough that a word count would call it specific")
        relocation, arbitration = self.box("Relocation to Austin", option), self.box("Binding arbitration", option)
        self.assertNotEqual(apply_classify.statement_of(relocation, "checkbox"), apply_classify.statement_of(arbitration, "checkbox"))
        self.allow("acknowledgment")
        # Nothing in it proves it names no document, so it is kept for the company it was saved for, on its heading and words.
        self.add(category="acknowledgment", question=apply_classify.statement_of(relocation, "checkbox"), answer="checked", company=COMPANY)
        self.assertIs(self.plan(BASE + [relocation]).get("q").value, True, "the same heading and words, at that company")
        self.assert_needs(BASE + [relocation], "sensitive_missing", "q", company=OTHER)
        self.assert_needs(BASE + [arbitration], "sensitive_missing", "q")
        self.assert_needs(BASE + [arbitration], "sensitive_missing", "q", company=OTHER)
        # The same heading and option with another description is another statement.
        described = self.box("Relocation to Austin", option, "<p>I will move at my own expense.</p>")
        self.assert_needs(BASE + [described], "sensitive_missing", "q", company=OTHER)

    def test_two_yes_no_acknowledgments_with_the_same_words_under_different_questions_never_share_an_answer(self):
        first = [self.text(self.RELOCATION, "t1"), self.yes_no("Do you acknowledge?", name="q1", parent=self.RELOCATION)]
        second = [self.text(self.ARBITRATION, "t2"), self.yes_no("Do you acknowledge?", name="q2", parent=self.ARBITRATION)]
        self.allow("acknowledgment")
        form = BASE + first + second
        got = self.plan(form)
        self.assertNotEqual(got.get("q1").statement, got.get("q2").statement)
        self.assertIn(self.RELOCATION, got.get("q1").statement)
        self.add(category="acknowledgment", question=got.get("q1").statement, answer="Yes", answer_kind="option", company="Example Robotics")
        again = self.plan(form)
        self.assertEqual((again.get("q1").value, again.get("q1").source.kind), ("Yes", "sensitive"))
        self.assertEqual((again.get("q2").problem_kind, again.get("q2").value, again.get("q2").source.kind), ("sensitive_missing", None, "none"))
        # The same words asked after another question at the same company are not the stored ones either.
        elsewhere = BASE + [self.text(self.ARBITRATION, "t2"), self.yes_no("Do you acknowledge?", name="q1", parent=self.ARBITRATION)]
        self.assert_needs(elsewhere, "sensitive_missing", "q1")

    def test_a_follow_up_yes_no_acknowledgment_is_filed_under_its_parent(self):
        label = "If yes, do you acknowledge the relocation terms?"
        one = [self.text("Are you willing to relocate?", "t1"), self.yes_no(label, parent="Are you willing to relocate?")]
        two = [self.text("Are you willing to work nights and weekends?", "t1"), self.yes_no(label, parent="Are you willing to work nights and weekends?")]
        self.allow("acknowledgment")
        got = self.plan(BASE + one).get("q")
        self.assertTrue(got.statement.startswith("Are you willing to relocate? / "), got.statement)
        self.add(category="acknowledgment", question=got.statement, answer="Yes", answer_kind="option", company="Example Robotics")
        self.assertEqual(self.plan(BASE + one).get("q").value, "Yes")
        self.assert_needs(BASE + two, "sensitive_missing", "q")

    def test_a_short_yes_no_heading_can_be_saved_under_its_question_and_alone_is_left_for_the_student(self):
        self.allow("acknowledgment")
        above = [self.text(self.RELOCATION, "t1"), self.yes_no("Acknowledgment", parent=self.RELOCATION)]
        under = self.plan(BASE + above).get("q")
        self.assertEqual(under.problem_kind, "sensitive_missing")
        form = apply_preflight._sensitive_form(under, "sensitive_missing")
        self.assertEqual((form["control"], form["company_only"]), ("select", True))
        saved = self.add(category="acknowledgment", question=under.statement, answer="Yes", answer_kind="option", company="Example Robotics",
                         company_only=form["company_only"], from_form=True)
        self.assertEqual(saved["question"], under.statement)
        self.assertEqual(self.plan(BASE + above).get("q").value, "Yes")
        # With nothing above it the statement is one word: the store could never hold it, so no form is offered for it.
        alone = self.plan(BASE + [self.yes_no("Acknowledgment")]).get("q")
        self.assertEqual((alone.problem_kind, alone.value), ("sensitive_never", None))
        self.assertEqual(apply_preflight._action(alone, {})["type"], "manual")
        self.assertEqual(apply_preflight._sensitive_state(alone, self.sources(), COMPANY), "")

    def test_a_short_option_on_a_bare_heading_is_kept_for_one_company_and_under_its_question(self):
        self.allow("acknowledgment")
        builders = (
            ("box", lambda parent: self.box("Acknowledgment", "I acknowledge", parent=parent)),
            ("yes and no", lambda parent: self.yes_no("Do you acknowledge?", parent=parent)),
        )
        for name, build in builders:
            with self.subTest(kind=name):
                relocation = [self.text(self.RELOCATION), build(self.RELOCATION)]
                arbitration = [self.text(self.ARBITRATION), build(self.ARBITRATION)]
                got = self.plan(BASE + relocation).get("q")
                form = apply_preflight._sensitive_form(got, "sensitive_missing")
                self.assertTrue(form["company_only"], "never offered for any company")
                self.assertIn(self.RELOCATION, got.statement)
                self.assertNotEqual(got.statement, self.plan(BASE + arbitration).get("q").statement)
                answer = "checked" if name == "box" else "Yes"
                with self.assertRaises(StoreRefused):
                    self.add(category="acknowledgment", question=got.statement, answer=answer, company_only=form["company_only"])
                saved = self.add(category="acknowledgment", question=got.statement, answer=answer, company="Example Robotics", company_only=True)
                self.assertFalse(saved["any_company"])
                self.assertIsNotNone(self.plan(BASE + relocation).get("q").value)
                # Under another employer's question, or at another employer, it is asked again.
                self.assert_needs(BASE + arbitration, "sensitive_missing", "q")
                self.assert_needs(BASE + relocation, "sensitive_missing", "q", company=OTHER)
                # Saved for any company by another route, it is still never read for a statement like this.
                with self.conn:
                    self.conn.execute("UPDATE apply_sensitive_answers SET company_key=''")
                self.assert_needs(BASE + relocation, "sensitive_missing", "q")
                self.assert_needs(BASE + relocation, "sensitive_missing", "q", company=OTHER)
                with self.conn:
                    self.conn.execute("DELETE FROM apply_sensitive_answers")

    def test_a_visa_question_in_any_wording_is_never_an_ordinary_answer(self):
        for label in ("What kind of visa do you have?", "What sort of visa do you hold?", "Your visa", "Visa (if applicable)"):
            with self.subTest(label=label):
                # An answer the student saved as reusable at another company is not typed here.
                rows = [apply_helpers.answer(label, "F-1", company=OTHER, tags=["reusable"])]
                got = apply_helpers.plan(BASE + [F("q", label)], apply_helpers.sources(answers=rows), "submit", company="Third Co").get("q")
                self.assertEqual((got.sensitive, got.source.kind, got.value), ("sponsorship", "none", None))
                self.assertEqual(apply_preflight._action(got, {})["type"], "manual", "never the ordinary answer form with Use for any company")

    # --- A statement that names a document any way at all is kept for one company (D9 B) ---

    NAMED_STATEMENTS = (
        ("box", "Candidate Terms", "I agree to the Candidate Terms"),
        ("box", "Data Protection", "I agree to the Candidate Data Protection Statement"),
        ("box", "Candidate Information", "I acknowledge receiving the Candidate Information Statement"),
        ("box", "Arbitration", "I agree to the Mutual Arbitration Program"),
        ("box", "Conduct", "I have reviewed and will abide by the Code of Conduct"),
        ("box", "Company Rules", "I agree to follow the Employee Guidelines"),
        ("box", "Onboarding", "I agree to be bound by the Standards of Practice Addendum"),
        ("box", "Recruiting", "I agree to the Northwind Recruiting Promise"),
        ("yes_no", "Do you agree to our Candidate Terms?", ""),
        ("yes_no", "Do you consent to the Applicant Data Statement?", ""),
    )

    def named_field(self, kind, heading, option):
        return self.box(heading, option) if kind == "box" else self.yes_no(heading)

    def test_a_statement_that_names_a_document_is_never_offered_stored_or_ticked_for_any_company(self):
        self.allow("acknowledgment", "consent")
        for kind, heading, option in self.NAMED_STATEMENTS:
            with self.subTest(statement=option or heading):
                form = BASE + [self.named_field(kind, heading, option)]
                got = self.plan(form).get("q")
                self.assertIn(got.sensitive, ("acknowledgment", "consent"))
                self.assertEqual((got.problem_kind, got.company_only), ("sensitive_missing", True))
                offered = apply_preflight._sensitive_form(got, "sensitive_missing")
                self.assertTrue(offered["company_only"], "no Use for any company")
                self.assertTrue(apply_sensitive.cites_document(got.statement, got.links))
                answer = "checked" if kind == "box" else "Yes"
                with self.assertRaises(StoreRefused):
                    self.add(category=got.sensitive, question=got.statement, answer=answer, answer_kind="option", company="", company_only=offered["company_only"], from_form=True)
                self.assertEqual(self.rows(), [])
                self.add(category=got.sensitive, question=got.statement, answer=answer, answer_kind="option", company=COMPANY, company_only=True, from_form=True)
                filled = self.plan(form).get("q")
                self.assertEqual((filled.disposition, filled.source.kind), ("fill", "sensitive"))
                self.assertIn(COMPANY, filled.source.label)
                # Another employer's form with the same words is asked again, and so is a row a legacy route saved for any company.
                self.assert_needs(form, "sensitive_missing", "q", company=OTHER)
                with self.conn:
                    self.conn.execute("UPDATE apply_sensitive_answers SET company_key=''")
                self.assert_needs(form, "sensitive_missing", "q")
                self.assert_needs(form, "sensitive_missing", "q", company=OTHER)
                with self.conn:
                    self.conn.execute("DELETE FROM apply_sensitive_answers")

    def test_a_statement_that_names_no_document_is_kept_for_one_company_too(self):
        self.allow("acknowledgment", "work_authorization")
        self.refused("never for any company", category="acknowledgment", question=f"Certification {ACCURATE}", answer="checked")
        self.add(category="acknowledgment", question=f"Certification {ACCURATE}", answer="checked", company=COMPANY)
        self.assertIs(self.plan(BASE + [self.box("Certification", ACCURATE)], company=COMPANY).get("q").value, True)
        self.assertIsNone(self.plan(BASE + [self.box("Certification", ACCURATE)], company=OTHER).get("q").value)
        # A tick box that states a fact about the student is kept for one company as well; only a select's exact label travels.
        self.refused("never for any company", category="work_authorization", question=f"{AUTH} I am authorized to work in the United States", answer="checked", answer_kind="checkbox")
        self.add(category="work_authorization", question=f"{AUTH} I am authorized to work in the United States", answer="checked", answer_kind="checkbox", company=COMPANY)
        self.assertFalse(apply_sensitive.cites_document("I am authorized to work in the United States", names=False))
        self.assertTrue(apply_sensitive.cites_document("I am authorized to work in the United States"))

    # --- A wording that asks for a demographic answer as well never stores one (D5 C (i)) ---

    def test_a_demographic_follow_up_of_a_work_authorization_question_is_never_offered_or_stored(self):
        self.allow("work_authorization", "sponsorship", "age_18")
        parent = F("q1", AUTH, SINGLE, options=("Yes", "No"))
        child = F("q2", "If other, please specify your gender", SINGLE, options=("Female", "Male", "Non-binary"), parent=AUTH)
        got = self.plan(BASE + [parent, child]).get("q2")
        self.assertEqual((got.sensitive, got.problem_kind, got.source.kind, got.value), ("uncategorized", "sensitive_never", "none", None))
        self.assertEqual(apply_preflight._action(got, {})["type"], "manual", "no form offering the demographic options")
        self.assertEqual(self.plan(BASE + [parent, child], "handoff").get("q2").disposition, "left_for_you")
        with self.assertRaises(StoreRefused):
            self.add(category="work_authorization", question=got.statement, answer="Female", from_form=True)
        self.assertEqual(self.rows(), [])
        # The settings route: a mixed wording is refused under every kind that is not EEO, and a real answer never gets in.
        for category, question, answer in (
            ("work_authorization", "Are you authorized to work in the US? Please also state your gender", "Female"),
            ("sponsorship", "Do you require visa sponsorship? What is your race?", "Asian"),
            ("age_18", "Are you 18 or older? Are you a veteran?", "I am a protected veteran"),
        ):
            with self.subTest(category=category):
                self.refused("voluntary self-identification", category=category, question=question, answer=answer)
                self.refused("voluntary self-identification", category=category, question=question, answer=answer, from_form=True)
        self.assertEqual(self.rows(), [])
        # The plain wordings of the same kinds still work.
        self.add(category="work_authorization", question=AUTH, answer="Yes")
        self.add(category="sponsorship", question=SPONSOR, answer="No")
        self.assertEqual(len(self.rows()), 2)

    def test_a_field_that_asks_work_authorization_and_a_demographic_question_together_is_uncategorized(self):
        for label in ("Do you require visa sponsorship? What is your race?", "Are you 18 or older? Are you a veteran?",
                      "Are you legally authorized to work in the US? Please also state your gender"):
            with self.subTest(label=label):
                item = F("q", label, SINGLE, options=("Yes", "No"))
                self.assertEqual(apply_classify.classify_item(item, "select"), "uncategorized")
                self.assertTrue(apply_classify.eeo_words(label))
        plain = F("q", AUTH, SINGLE, options=("Yes", "No"))
        self.assertEqual(apply_classify.classify_item(plain, "select"), "work_authorization")


class StoreReaderScanTests(unittest.TestCase):
    """12.7: only the policy, the runs, operations (export and deletion), the schema (its migration step) and the store's own module name the table."""

    # Allowed modules, as paths from the repo root without ".py". Each may be a single file or, after a split, a package of
    # the same name (opportunity_app/schema/...), but only at this location: a same-named file elsewhere (scripts/schema.py,
    # pipeline_core/operations.py) is not allowed, which the old basename check wrongly let through.
    ALLOWED = ("opportunity_app/apply_sensitive", "opportunity_app/operations", "opportunity_app/schema")

    def sources(self):
        for folder in ("opportunity_app", "pipeline_core"):
            yield from (REPO / folder).rglob("*.py")
        yield REPO / "pipeline.py"
        yield from (REPO / "scripts").rglob("*.py")

    def allowed(self, path):
        module = path.relative_to(REPO).as_posix()[:-3]
        return any(module == name or module.startswith(name + "/") for name in self.ALLOWED)

    def test_no_other_source_file_names_the_table(self):
        named = [path for path in self.sources() if "apply_sensitive_answers" in path.read_text(encoding="utf-8")]
        self.assertTrue(named, "the scan found the files it should")
        self.assertEqual([path.relative_to(REPO).as_posix() for path in named if not self.allowed(path)], [],
                         "a file that reads the store must be added here on purpose")

    def package_modules(self):
        return [path for path in (REPO / "opportunity_app").rglob("*.py")]

    # Dotted names, spelled by the import statement and then resolved from the importing file's own package, so a relative
    # `from ..apply import sensitive` and an absolute `from opportunity_app.apply.sensitive import lookup` name the same module
    # as `from . import apply_sensitive` does today. A text search for the module's old file name would go quiet the day the
    # module moves into a package, so the importer checks below read the import targets, not the words.
    STORE_MODULE = "opportunity_app.apply_sensitive"
    POLICY_MODULE = "opportunity_app.apply_policy"

    def resolved_imports(self, path):
        """Every dotted module name `path` imports: for `from X import a` both X and X.a, with relative X resolved."""
        package = list(path.relative_to(REPO).with_suffix("").parts[:-1])
        found = set()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))):
            if isinstance(node, ast.Import):
                found.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                anchor = package[: len(package) - (node.level - 1)] if node.level else []
                base = ".".join(anchor + ([node.module] if node.module else []))
                found.add(base)
                found.update(f"{base}.{alias.name}" for alias in node.names)
        return found

    def imports_module(self, path, module):
        return any(name == module or name.startswith(module + ".") for name in self.resolved_imports(path))

    def in_module(self, path, name):
        """Whether `path` is the module `name` (repo-relative, no .py) or sits inside a package of that name."""
        module = path.relative_to(REPO).as_posix()[:-3]
        return module == name or module.startswith(name + "/")

    def test_the_import_reader_finds_every_spelling_of_an_import_of_a_module(self):
        # A made-up store module, so the cases stay valid wherever the real one lives.
        module = "opportunity_app.vault.store"
        found = {
            "opportunity_app/web/x.py": ("from ..vault import store", "from ..vault.store import lookup", "from opportunity_app.vault import store",
                                         "from opportunity_app.vault.store import lookup", "import opportunity_app.vault.store as s"),
            "opportunity_app/vault/y.py": ("from . import store", "from .store import lookup"),
            "opportunity_app/x.py": ("from .vault import store", "from .vault.store import lookup"),
        }
        missed = ("from . import schema", "from .vault import storefront", "from .vault.store_other import x", "from . import vault", "import os")
        for relative, lines in found.items():
            for line in lines:
                with self.subTest(file=relative, line=line):
                    self.assertTrue(self.imports_text(relative, line, module))
        for relative in found:
            for line in missed:
                with self.subTest(file=relative, line=line):
                    self.assertFalse(self.imports_text(relative, line, module))

    def imports_text(self, relative, source, module):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / relative
            path.parent.mkdir(parents=True)
            path.write_text(source + "\n", encoding="utf-8")
            with mock.patch(f"{__name__}.REPO", Path(directory)):
                return self.imports_module(path, module)

    def test_employer_and_reporting_code_never_read_the_store_or_import_it(self):
        # Matched against the repo-relative path, so a reporting module moved into a subpackage (opportunity_app/metrics/x.py,
        # opportunity_app/reports/x.py) is still found, whatever its own file name.
        pattern = r"employer|report|metric|analytic|fairness|subgroup|export_pipeline|dossier|digest"
        reporting = [path for path in self.package_modules() if re.search(pattern, path.relative_to(REPO).as_posix())]
        self.assertIn("opportunity_app/employer.py", {path.relative_to(REPO).as_posix() for path in reporting})
        for path in reporting:
            text = path.read_text(encoding="utf-8")
            with self.subTest(file=path.relative_to(REPO).as_posix()):
                self.assertNotRegex(text, r"apply_sensitive|apply_policy|sensitive_answers|stored_sensitive_answer")
                self.assertFalse(self.imports_module(path, self.STORE_MODULE), "imports the sensitive-answer store")
                self.assertFalse(self.imports_module(path, self.POLICY_MODULE), "imports the apply policy")
        for path in [REPO / "pipeline.py"] + list((REPO / "pipeline_core").rglob("*.py")):
            self.assertNotIn("apply_sensitive", path.read_text(encoding="utf-8"), path.relative_to(REPO).as_posix())

    # The modules that may import the store, keyed like ALLOWED on the path from the repo root: the plan, the check, and the
    # settings routes (web/routers/apply_agent.py). A file or package at another path that imports it fails, however it is named.
    IMPORTERS = ("opportunity_app/apply_policy", "opportunity_app/apply_preflight", "opportunity_app/web/routers/apply_agent")

    def test_only_the_plan_the_check_and_the_settings_routes_import_the_store(self):
        importers = {path.relative_to(REPO).as_posix() for path in self.package_modules()
                     if (re.search(r"\bapply_sensitive\b", path.read_text(encoding="utf-8")) or self.imports_module(path, self.STORE_MODULE))
                     and not self.in_module(path, "opportunity_app/apply_sensitive")}
        self.assertTrue(importers, "the scan found the files it should")
        self.assertEqual([item for item in sorted(importers)
                          if not any(self.in_module(REPO / (item), name) for name in self.IMPORTERS)], [],
                         "a module that imports the store must be added to IMPORTERS on purpose")
        for name in self.IMPORTERS:
            self.assertTrue(any(item == name + ".py" or item.startswith(name + "/") for item in importers), f"{name} no longer imports the store")

    def test_the_extension_and_the_saved_answer_library_code_never_touch_it(self):
        # Every module of these names, whether it stays one file or becomes a package (preparation/...).
        for name in ("extension_apply", "preparation", "profile", "resume_variants"):
            found = [path for path in self.package_modules() if self.in_module(path, f"opportunity_app/{name}")]
            self.assertTrue(found, f"no module found for opportunity_app/{name}")
            for path in found:
                with self.subTest(file=path.relative_to(REPO).as_posix()):
                    self.assertNotRegex(path.read_text(encoding="utf-8"), r"apply_sensitive|sensitive_answers|stored_sensitive_answer")
                    self.assertFalse(self.imports_module(path, self.STORE_MODULE), "imports the sensitive-answer store")


if __name__ == "__main__":
    unittest.main()
