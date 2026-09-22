"""Regression guards for the 2026-09-21 scoring and source-integrity fixes.

Each test failed on the code before the fix:

- a bare dollar figure (stipend, funding) was stored as a yearly salary;
- any "N years" (age, degree length) read as an experience requirement;
- seniority matched substrings ("Leadership" -> "lead") and penalised intern
  titles that happen to name a manager role;
- the cover letter defaulted to an "engineering" student and only stripped
  "B.S." from the degree;
- abbreviated months in a deadline were dropped;
- a null list/number in the profile crashed scoring or lost an explicit 0.
"""

from __future__ import annotations

import re
import sqlite3
import unittest

import pipeline
from opportunity_app.opportunity_metadata import extract_deadline, extract_opportunity_metadata


def _profile(**overrides):
    profile = {
        "preferred_role_types": [],
        "degree_keywords": [],
        "interest_keywords": [],
        "skills": [],
        "preferred_locations": [],
        "remote_ok": False,
        "max_years_experience": 1,
    }
    profile.update(overrides)
    return profile


def _job(**overrides):
    job = {
        "title": "Engineering Intern",
        "description": "Hands-on work.",
        "role_type": "internship",
        "location": "",
        "posted_at": None,
    }
    job.update(overrides)
    return job


def _reason_total(reasons):
    """Sum of every signed number that leads a reason ("35 base", "+8 ...", "-18 ...")."""
    total = 0
    for reason in reasons:
        match = re.match(r"^([+-]?\d+)\s", reason)
        if match:
            total += int(match.group(1))
    return total


class YearlyPayRequiresPeriodTests(unittest.TestCase):
    def pay(self, text):
        return extract_opportunity_metadata("Intern", "Austin, TX", text)

    def test_bare_stipend_is_not_a_yearly_salary(self):
        meta = self.pay("Interns receive a $15,000 housing stipend.")
        self.assertEqual(meta["pay_period"], "")
        self.assertIsNone(meta["pay_min"])
        self.assertEqual(meta["currency"], "")

    def test_company_funding_is_not_a_yearly_salary(self):
        meta = self.pay("We recently raised $120,000,000 in funding.")
        self.assertEqual(meta["pay_period"], "")
        self.assertIsNone(meta["pay_max"])

    def test_genuine_yearly_ranges_still_parse(self):
        meta = self.pay("Compensation: $80,000 - $95,000 per year.")
        self.assertEqual((meta["pay_period"], meta["pay_min"], meta["pay_max"]), ("year", 80000.0, 95000.0))
        for text in ("$90,000/yr", "$90,000 a year", "$90,000 annually", "$90,000 per annum"):
            with self.subTest(text=text):
                meta = self.pay(text)
                self.assertEqual((meta["pay_period"], meta["pay_min"]), ("year", 90000.0))

    def test_hourly_pay_is_unchanged(self):
        meta = self.pay("$25 - $30 per hour")
        self.assertEqual((meta["pay_period"], meta["pay_min"], meta["pay_max"]), ("hour", 25.0, 30.0))


class ExperienceYearsTests(unittest.TestCase):
    def penalties(self, description):
        _, reasons = pipeline.score_job(_job(description=description), _profile())
        return [reason for reason in reasons if "years" in reason and reason.startswith("-")]

    def test_age_requirement_is_not_experience(self):
        self.assertEqual(self.penalties("Applicants must be at least 18 years of age."), [])

    def test_degree_length_is_not_experience(self):
        self.assertEqual(self.penalties("Pursuing a 4 year degree in engineering."), [])

    def test_age_or_degree_followed_later_by_experience_is_not_experience(self):
        for text in (
            "Applicants must be at least 18 years of age and have experience using spreadsheets.",
            "A 4 year degree with practical experience is preferred.",
            "Must be 18 years old with experience in customer service.",
        ):
            with self.subTest(text=text):
                self.assertEqual(self.penalties(text), [])

    def test_experience_described_with_a_conjunction_is_still_penalised(self):
        for text in (
            "Requires 5 years of research and development experience.",
            "3 years of sales or marketing experience preferred.",
        ):
            with self.subTest(text=text):
                self.assertTrue(self.penalties(text))

    def test_real_experience_requirement_is_penalised(self):
        self.assertEqual(
            self.penalties("Requires 3+ years of professional experience."), ["-18 asks for 3+ years"]
        )
        self.assertEqual(self.penalties("5 years experience with CAD."), ["-18 asks for 5+ years"])

    def test_explicit_zero_max_experience_is_preserved(self):
        description = "1 year of experience preferred."
        _, zero = pipeline.score_job(_job(description=description), _profile(max_years_experience=0))
        _, one = pipeline.score_job(_job(description=description), _profile(max_years_experience=1))
        self.assertIn("-18 asks for 1+ years", zero)
        self.assertNotIn("-18 asks for 1+ years", one)


class SeniorityTests(unittest.TestCase):
    def seniority(self, title):
        _, reasons = pipeline.score_job(_job(title=title, role_type="other"), _profile())
        return [reason for reason in reasons if "seniority" in reason]

    def test_seniority_words_match_whole_words_only(self):
        self.assertEqual(self.seniority("Leadership Development Program Engineer"), [])
        self.assertEqual(self.seniority("Staffing Operations Analyst"), [])
        self.assertEqual(self.seniority("Change Management Analyst"), [])

    def test_intern_title_naming_a_manager_is_not_senior(self):
        self.assertEqual(self.seniority("Technical Program Manager Intern"), [])
        self.assertEqual(self.seniority("Product Manager Co-op"), [])

    def test_real_senior_titles_are_still_penalised(self):
        self.assertEqual(self.seniority("Sr. Engineer"), ["-35 seniority mismatch: sr."])
        self.assertEqual(self.seniority("Senior Mechanical Engineer"), ["-35 seniority mismatch: senior"])
        self.assertEqual(self.seniority("Lead Test Engineer"), ["-35 seniority mismatch: lead"])
        self.assertEqual(self.seniority("Engineering Manager"), ["-35 seniority mismatch: manager"])


class ProfileNullHardeningTests(unittest.TestCase):
    def test_null_lists_score_like_empty_lists(self):
        nulls = {
            key: None
            for key in (
                "preferred_role_types",
                "degree_keywords",
                "interest_keywords",
                "deprioritize_title_keywords",
                "skills",
                "preferred_locations",
                "available_terms",
                "regions",
            )
        }
        job = _job(title="Engineering Intern Summer 2027", location="Austin, TX")
        null_score, null_reasons = pipeline.score_job(job, _profile(**nulls))
        empty_score, empty_reasons = pipeline.score_job(
            job, _profile(**{key: [] for key in nulls})
        )
        self.assertEqual((null_score, null_reasons), (empty_score, empty_reasons))

    def test_null_numbers_take_their_defaults(self):
        job = _job(description="Requires 2 years of experience.", location="Seattle, WA")
        regions = [{"name": "Austin", "state_markers": ["tx"], "places": ["austin"]}]
        _, reasons = pipeline.score_job(
            job, _profile(max_years_experience=None, out_of_region_penalty=None, regions=regions)
        )
        self.assertIn("-18 asks for 2+ years", reasons)
        self.assertTrue(any(reason.startswith("-40 outside target regions") for reason in reasons))

    def test_explicit_zero_penalty_is_preserved(self):
        regions = [{"name": "Austin", "state_markers": ["tx"], "places": ["austin"]}]
        _, reasons = pipeline.score_job(
            _job(location="Seattle, WA"), _profile(out_of_region_penalty=0, regions=regions)
        )
        self.assertTrue(any(reason.startswith("-0 outside target regions") for reason in reasons))

    def test_regions_without_a_name_are_skipped(self):
        regions = [
            {"state_markers": ["tx"], "places": ["austin"], "bonus": 15},
            {"name": None, "state_markers": ["tx"], "places": ["austin"]},
            {"name": "Austin", "state_markers": ["tx"], "places": ["austin"], "bonus": 12},
        ]
        _, reasons = pipeline.score_job(_job(location="Austin, TX"), _profile(regions=regions))
        self.assertIn("+12 location: Austin (target radius)", reasons)

    def test_null_region_bonus_adds_nothing(self):
        regions = [{"name": "Austin", "state_markers": ["tx"], "places": ["austin"], "bonus": None}]
        score, reasons = pipeline.score_job(_job(location="Austin, TX"), _profile(regions=regions))
        self.assertIn("+0 location: Austin (target radius)", reasons)
        self.assertEqual(score, _reason_total(reasons))

    def test_null_region_markers_do_not_crash(self):
        regions = [{"name": "Austin", "aliases": None, "state_markers": None, "places": None}]
        score, _ = pipeline.score_job(_job(location="Austin, TX"), _profile(regions=regions))
        self.assertGreaterEqual(score, 0)

    def test_reasons_account_for_the_raw_score_and_score_is_clamped(self):
        cases = [
            (_job(title="Senior Staff Engineer", role_type="other",
                  description="Requires 10+ years of experience.", location="Seattle, WA"),
             _profile(regions=[{"name": "Austin", "state_markers": ["tx"], "places": ["austin"]}])),
            (_job(title="Mechanical Engineering Intern", description="SolidWorks and CAD.",
                  location="Austin, TX"),
             _profile(preferred_role_types=["internship"], skills=["SolidWorks", "CAD"],
                      degree_keywords=["mechanical engineering"],
                      regions=[{"name": "Austin", "state_markers": ["tx"], "places": ["austin"], "bonus": 15}])),
        ]
        for job, profile in cases:
            score, reasons = pipeline.score_job(job, profile)
            self.assertEqual(score, max(0, min(100, _reason_total(reasons))))


class CoverLetterIdentityTests(unittest.TestCase):
    def _job(self):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE jobs (id TEXT, company TEXT, title TEXT, location TEXT,"
            " description TEXT, url TEXT)"
        )
        conn.execute(
            "INSERT INTO jobs VALUES ('abc123def456', 'Acme', 'Analyst Intern', 'Austin, TX',"
            " 'Data work.', 'https://x/1')"
        )
        return conn.execute("SELECT * FROM jobs").fetchone()

    def test_no_education_is_a_todo_not_engineering(self):
        markup = pipeline.build_cover_letter_html({"name": "Test"}, self._job())
        self.assertNotIn("engineering", markup.lower())
        self.assertIn("[your degree and school -- add education to config/resume.json]", markup)
        self.assertNotIn("student at ,", markup)

    def test_degree_falls_back_to_profile(self):
        markup = pipeline.build_cover_letter_html(
            {"name": "Test"}, self._job(), {"degree": "B.A. Economics", "school": "Example College"}
        )
        self.assertIn("I am an Economics student at Example College", markup)

    def test_other_degree_abbreviations_are_stripped(self):
        for degree, expected in (
            ("B.A. History", "a History student"),
            ("B.S.E. Electrical Engineering", "an Electrical Engineering student"),
            ("B.Eng. Civil Engineering", "a Civil Engineering student"),
            ("BS Chemistry", "a Chemistry student"),
            ("M.S. in Statistics", "a Statistics student"),
        ):
            with self.subTest(degree=degree):
                resume = {"education": [{"school": "Example College", "degree": degree}]}
                markup = pipeline.build_cover_letter_html(resume, self._job())
                self.assertIn(f"I am {expected} at Example College", markup)

    def test_missing_school_drops_the_at_clause(self):
        resume = {"education": [{"degree": "B.S. Biology"}]}
        markup = pipeline.build_cover_letter_html(resume, self._job())
        self.assertIn("I am a Biology student, and", markup)
        self.assertNotIn(" at ,", markup)


class AbbreviatedDeadlineTests(unittest.TestCase):
    def test_abbreviated_months_parse(self):
        self.assertEqual(extract_deadline("Apply by Sep 30, 2026"), "2026-09-30T00:00:00+00:00")
        self.assertEqual(extract_deadline("Deadline: Sept 30 2026"), "2026-09-30T00:00:00+00:00")
        self.assertEqual(extract_deadline("Applications close Oct. 1, 2026"), "2026-10-01T00:00:00+00:00")

    def test_existing_forms_and_nonsense_are_unchanged(self):
        self.assertEqual(extract_deadline("Apply by September 30, 2026"), "2026-09-30T00:00:00+00:00")
        self.assertEqual(extract_deadline("Deadline: 2026-10-01"), "2026-10-01T00:00:00+00:00")
        self.assertEqual(extract_deadline("Deadline: 10/01/2026"), "2026-10-01T00:00:00+00:00")
        self.assertIsNone(extract_deadline("Deadline: Foo 3, 2026"))
        self.assertIsNone(extract_deadline("Deadline: Feb 30, 2026"))


if __name__ == "__main__":
    unittest.main()
