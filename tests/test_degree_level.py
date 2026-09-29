"""Degree level in scoring.

A posting whose title names the degree levels it takes ("2027 Summer Intern,
MS/PhD, ...") scored as if any student could apply, so a bachelor's student saw
graduate-only internships at the top of the shortlist. The title's levels are
now compared with the level in the profile's ``degree``, and a title that names
none of the student's takes the same -35 as a senior title.
"""

from __future__ import annotations

import re
import unittest

import pipeline
from opportunity_app.setup import validate_profile


def _profile(**overrides):
    profile = {
        "preferred_role_types": ["internship"],
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
    return sum(int(match.group(1)) for reason in reasons if (match := re.match(r"^([+-]?\d+)\s", reason)))


def _degree_reasons(reasons):
    return [reason for reason in reasons if "degree level" in reason]


class StudentDegreeLevelTests(unittest.TestCase):
    def test_levels_from_common_degree_spellings(self):
        cases = {
            "B.S. Chemical Engineering": {"bachelor"},
            "BS Mechanical Engineering": {"bachelor"},
            "Bachelor of Science in Biology": {"bachelor"},
            "B.A. Economics": {"bachelor"},
            "BSE Electrical Engineering": {"bachelor"},
            "Undergraduate, Physics": {"bachelor"},
            "M.S. Robotics": {"master"},
            "MEng Electrical Engineering": {"master"},
            "Master of Science, Physics": {"master"},
            "Ph.D. Physics": {"doctorate"},
            "PhD candidate, Chemistry": {"doctorate"},
            "B.S./M.S. Electrical Engineering": {"bachelor", "master"},
            # An MBA is a master's degree, so a title asking for master's students includes one.
            "MBA": {"mba", "master"},
        }
        for degree, levels in cases.items():
            with self.subTest(degree=degree):
                self.assertEqual(pipeline.degree_levels(degree), levels)

    def test_degree_without_a_level_names_none(self):
        for degree in ("Chemical Engineering", "", None, "Associate of Science", "Materials Science"):
            with self.subTest(degree=degree):
                self.assertEqual(pipeline.degree_levels(degree), set())


class TitleDegreeLevelTests(unittest.TestCase):
    def test_levels_named_in_titles(self):
        cases = {
            "2027 Summer Intern, MS/PhD, Perception, Machine Learning": {"master", "doctorate"},
            "2027 Summer Intern, PhD, Data Science": {"doctorate"},
            "PhD Autonomy Engineer Intern - Computer Vision Summer 2027": {"doctorate"},
            "Summer 2027 Internships: Ph.D. Engineering": {"doctorate"},
            "2027 Summer Intern, BS/MS, Embedded, Software Engineer": {"bachelor", "master"},
            "Analog Layout Intern, BS - Summer 2027": {"bachelor"},
            "Digital IC Design Intern, MS - Summer 2027": {"master"},
            "2027 Summer Intern, MS, Software Engineering, Behavior Test": {"master"},
            "Summer 2027 Supply Chain Buyer Intern- Bachelor's (Austin, TX)": {"bachelor"},
            "Software Engineering - Intern, Bachelor’s": {"bachelor"},
            "2027 Manufacturing Engineer Summer Internship (Bachelors Austin, TX)": {"bachelor"},
            "2027 Software Engineering Intern (Masters - Example City, CA)": {"master"},
            "Data Analyst Intern- Bachelor's/Master's (Albany, NY)": {"bachelor", "master"},
            "Master Thesis Student (m/f/d) Network Security": {"master"},
            "2026 Fall Materials Engineering Co-op - Doctorate (Gloucester, MA)": {"doctorate"},
            "2027 Summer Intern, MBA, Strategic Finance": {"mba"},
            "2027 Undergraduate Hardware Engineering Internships - US": {"bachelor"},
            "IT Intern: Undergrad (Tech)": {"bachelor"},
            "Perception Engineer (PhD, New Grad)": {"doctorate"},
        }
        for title, levels in cases.items():
            with self.subTest(title=title):
                self.assertEqual(pipeline.title_degree_levels(title), levels)

    def test_words_that_do_not_name_a_degree_level(self):
        for title in (
            "Scrum Master Intern",
            "Master Scheduler",
            "Master Data Management Analyst",
            "New Grad Systems Engineer",
            "Graduate Engineer Internship/Co-op",
            "Innovation Enablement Graduate Intern",
            "Engineering Intern (Jackson, MS)",
            "Field Engineer - Jackson, MS",
            "Mechanical Engineering Intern (Boston, MA)",
            "Business Analyst (BA) Intern",
            "MS Office Specialist",
            "Mastercam Programmer Intern",
            "Engineering Intern",
        ):
            with self.subTest(title=title):
                self.assertEqual(pipeline.title_degree_levels(title), set())


class DegreeLevelScoringTests(unittest.TestCase):
    def test_graduate_only_title_is_penalised_for_a_bachelors_student(self):
        profile = _profile(degree="B.S. Mechanical Engineering")
        plain, _ = pipeline.score_job(_job(title="2027 Summer Intern, Software Engineer"), profile)
        score, reasons = pipeline.score_job(_job(title="2027 Summer Intern, MS/PhD, Software Engineer"), profile)
        self.assertEqual(
            _degree_reasons(reasons), ["-35 degree level: title asks for master's or PhD, not bachelor's"]
        )
        self.assertEqual(score, plain - 35)
        self.assertEqual(_reason_total(reasons), score)

    def test_title_naming_the_students_level_is_not_penalised(self):
        profile = _profile(degree="B.S. Mechanical Engineering")
        for title in (
            "2027 Summer Intern, BS/MS, Embedded, Software Engineer",
            "Analog Layout Intern, BS - Summer 2027",
            "2027 Undergraduate Hardware Engineering Internships - US",
            "Engineering Intern",
        ):
            with self.subTest(title=title):
                _, reasons = pipeline.score_job(_job(title=title), profile)
                self.assertEqual(_degree_reasons(reasons), [])

    def test_masters_student_is_penalised_for_a_bachelors_only_title(self):
        _, reasons = pipeline.score_job(
            _job(title="Analog Layout Intern, BS - Summer 2027"), _profile(degree="M.S. Electrical Engineering")
        )
        self.assertEqual(_degree_reasons(reasons), ["-35 degree level: title asks for bachelor's, not master's"])

    def test_mba_title_takes_an_mba_student_but_not_another_masters(self):
        job = _job(title="2027 Summer Intern, MBA, Strategic Finance")
        _, mba = pipeline.score_job(job, _profile(degree="MBA"))
        _, ms = pipeline.score_job(job, _profile(degree="M.S. Industrial Engineering"))
        self.assertEqual(_degree_reasons(mba), [])
        self.assertEqual(_degree_reasons(ms), ["-35 degree level: title asks for MBA, not master's"])

    def test_combined_program_is_penalised_only_when_neither_level_fits(self):
        profile = _profile(degree="B.S./M.S. Electrical Engineering")
        _, masters = pipeline.score_job(_job(title="Digital IC Design Intern, MS - Summer 2027"), profile)
        _, phd = pipeline.score_job(_job(title="2027 Summer Intern, PhD, Data Science"), profile)
        self.assertEqual(_degree_reasons(masters), [])
        self.assertEqual(_degree_reasons(phd), ["-35 degree level: title asks for PhD, not bachelor's or master's"])

    def test_unknown_student_level_leaves_the_score_alone(self):
        job = _job(title="2027 Summer Intern, MS/PhD, Software Engineer")
        for profile in (_profile(degree="Mechanical Engineering"), _profile(degree=""), _profile(degree=None), _profile()):
            with self.subTest(degree=profile.get("degree", "missing")):
                _, reasons = pipeline.score_job(job, profile)
                self.assertEqual(_degree_reasons(reasons), [])

    def test_description_alone_never_sets_the_level(self):
        # "BS, MS, or PhD" in a description is usually inclusive; only the title is read.
        job = _job(title="Firmware Intern", description="Currently pursuing an MS or PhD in electrical engineering.")
        _, reasons = pipeline.score_job(job, _profile(degree="B.S. Mechanical Engineering"))
        self.assertEqual(_degree_reasons(reasons), [])


class DegreeLevelSetupWarningTests(unittest.TestCase):
    def _level_warnings(self, degree):
        report = validate_profile({"degree": degree})
        return [warning for warning in report["warnings"] if "degree" in warning and "level" in warning]

    def test_degree_without_a_level_warns(self):
        self.assertEqual(len(self._level_warnings("Chemical Engineering")), 1)

    def test_degree_with_a_level_or_no_degree_does_not_warn(self):
        for degree in ("B.S. Chemical Engineering", "PhD, Chemistry", ""):
            with self.subTest(degree=degree):
                self.assertEqual(self._level_warnings(degree), [])


if __name__ == "__main__":
    unittest.main()
