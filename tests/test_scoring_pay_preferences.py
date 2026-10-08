"""The pay preferences a student saves (minimum hourly pay, paid roles only) now move the score.

``compensation_preferences`` was validated, edited in the profile form and sent to the model review, and read by
nothing that ranks: a $12 an hour posting and an unpaid one scored like any other. Each test failed before the
change.

Pay comes only from what the posting states in dollars per hour; a figure with no period, a yearly salary (turning it
into an hourly rate would be an assumption) and a posting with no pay at all change nothing.
"""

from __future__ import annotations

import unittest

from pipeline_core import scoring

try:
    import realdata_guard
except ImportError:  # imported as tests.<module>, with tests/ not on sys.path
    from tests import realdata_guard
# Guards this module's own run against opening data/*.db (AGENTS.md hard rule 1); see tests/realdata_guard.py.
realdata_guard.install()


def _profile(**preferences):
    profile = {
        "preferred_role_types": [],
        "degree_keywords": [],
        "interest_keywords": [],
        "skills": [],
        "preferred_locations": [],
        "remote_ok": False,
        "max_years_experience": 1,
    }
    if preferences:
        profile["compensation_preferences"] = preferences
    return profile


def _job(description, title="Engineering Intern"):
    return {"title": title, "description": description, "role_type": "internship", "location": "", "posted_at": None}


def _score(description, **preferences):
    return scoring.score_job(_job(description), _profile(**preferences))


def _pay_reasons(description, **preferences):
    return [
        reason for reason in _score(description, **preferences)[1]
        if ("pay" in reason.lower() or "unpaid" in reason.lower()) and reason[0] in "+-"
    ]


class HourlyFloorTests(unittest.TestCase):
    def test_pay_below_the_minimum_costs_points_and_says_why(self):
        score, reasons = _score("This internship pays $18 - $22 per hour.", minimum_hourly=25)
        self.assertIn("-15 pays up to $22/hour, below your $25/hour minimum", reasons)
        self.assertEqual(score, _score("This internship pays well.")[0] - 15)

    def test_one_figure_says_pays_not_up_to(self):
        self.assertEqual(
            _pay_reasons("Compensation is $15/hour.", minimum_hourly=20),
            ["-15 pays $15/hour, below your $20/hour minimum"],
        )

    def test_a_range_that_reaches_the_minimum_is_not_penalised(self):
        self.assertEqual(_pay_reasons("Pay: $20 to $30 per hour.", minimum_hourly=25), [])
        self.assertEqual(_pay_reasons("Pay: $25 per hour.", minimum_hourly=25), [])

    def test_cents_are_kept(self):
        self.assertEqual(
            _pay_reasons("Pay: $19.50 per hour.", minimum_hourly=20),
            ["-15 pays $19.5/hour, below your $20/hour minimum"],
        )

    def test_a_yearly_salary_is_not_turned_into_an_hourly_rate(self):
        self.assertEqual(_pay_reasons("Salary: $40,000 per year.", minimum_hourly=50), [])

    def test_no_stated_pay_changes_nothing(self):
        self.assertEqual(_pay_reasons("Great team, great mentors.", minimum_hourly=50), [])

    def test_a_dollar_figure_with_no_period_is_not_pay(self):
        self.assertEqual(_pay_reasons("We raised $12 million. Housing stipend of $15.", minimum_hourly=50), [])

    def test_no_minimum_or_an_unusable_one_changes_nothing(self):
        for preferences in ({}, {"minimum_hourly": None}, {"minimum_hourly": 0}, {"minimum_hourly": -5},
                            {"minimum_hourly": True}, {"minimum_hourly": "25"}, {"minimum_hourly": float("nan")}):
            with self.subTest(preferences=preferences):
                self.assertEqual(_pay_reasons("Pays $10 per hour.", **preferences), [])

    def test_a_profile_with_no_pay_preferences_at_all_is_unchanged(self):
        profile = _profile()
        self.assertNotIn("compensation_preferences", profile)
        _, reasons = scoring.score_job(_job("Pays $10 per hour."), profile)
        self.assertEqual([r for r in reasons if "pay" in r.lower() and r[0] in "+-"], [])

    def test_a_malformed_preferences_value_changes_nothing(self):
        for value in ("a lot", ["25"], 25, True):
            with self.subTest(value=value):
                profile = _profile()
                profile["compensation_preferences"] = value
                _, reasons = scoring.score_job(_job("Pays $10 per hour."), profile)
                self.assertEqual([r for r in reasons if "pay" in r.lower() and r[0] in "+-"], [])

    def test_another_currency_is_not_compared_with_dollars(self):
        self.assertEqual(_pay_reasons("Pays $10 per hour.", minimum_hourly=25, currency="CAD"), [])
        self.assertEqual(
            _pay_reasons("Pays $10 per hour.", minimum_hourly=25, currency="usd"),
            ["-15 pays $10/hour, below your $25/hour minimum"],
        )
        self.assertEqual(
            _pay_reasons("Pays $10 per hour.", minimum_hourly=25, currency=""),
            ["-15 pays $10/hour, below your $25/hour minimum"],
        )


class SeveralFiguresTests(unittest.TestCase):
    """Found in review: only the first hourly figure was read, so a posting's other rates were ignored."""

    def test_the_highest_stated_rate_is_the_one_compared(self):
        for text in (
            "$22.00/hr - $30.00/hr",
            "Parking is $5/hr. The internship pays $45 per hour.",
            "Undergraduates $20/hour; graduate students $32/hour.",
        ):
            with self.subTest(text=text):
                self.assertEqual(_pay_reasons(text, minimum_hourly=25), [])

    def test_every_rate_below_the_minimum_costs_points_and_names_the_highest(self):
        self.assertEqual(
            _pay_reasons("Juniors earn $18/hr and seniors earn $22/hr.", minimum_hourly=25),
            ["-15 pays up to $22/hour, below your $25/hour minimum"],
        )


class PaidOnlyTests(unittest.TestCase):
    def test_an_explicitly_unpaid_role_costs_points_when_the_student_wants_paid_only(self):
        for text in (
            "This is an unpaid internship.",
            "Unpaid internship for credit.",
            "An unpaid position with great mentors.",
            "This is a volunteer role.",
            "The role carries no compensation.",
            "The internship is unpaid.",
        ):
            with self.subTest(text=text):
                self.assertIn(
                    "-35 unpaid, and you asked for paid roles only", _score(text, paid_only=True)[1]
                )

    def test_nothing_changes_when_paid_only_is_off_or_unanswered(self):
        for preferences in ({"paid_only": False}, {"paid_only": None}, {"minimum_hourly": 20}, {"paid_only": "yes"}):
            with self.subTest(preferences=preferences):
                self.assertEqual(_pay_reasons("This is an unpaid internship.", **preferences), [])

    def test_ordinary_unpaid_wording_in_benefits_is_not_an_unpaid_role(self):
        for text in (
            "Benefits include unpaid time off and a paid internship stipend.",
            "Unpaid leave is available for family reasons.",
            "This is a paid internship.",
            "Candidates must not be unpaid interns elsewhere.",
            "Time off without pay is not offered; leave is unpaid in some regions.",
        ):
            with self.subTest(text=text):
                self.assertEqual(_pay_reasons(text, paid_only=True), [])

    def test_wording_that_only_resembles_unpaid_is_not_unpaid(self):
        for text in (
            "This is a paid internship, not an unpaid internship.",
            "Experience can include a prior internship, co-op or volunteer role.",
            "This role offers no compensation for relocation.",
            "The program has no pay gap.",
            "The role is not unpaid.",
        ):
            with self.subTest(text=text):
                self.assertEqual(_pay_reasons(text, paid_only=True), [])

    def test_a_volunteer_role_the_posting_says_it_is_is_unpaid(self):
        self.assertEqual(
            _pay_reasons("This is a volunteer role with a flexible schedule.", paid_only=True),
            ["-35 unpaid, and you asked for paid roles only"],
        )

    def test_a_monthly_weekly_or_stipend_figure_is_stated_pay(self):
        for text in (
            "Not your typical unpaid internship: it pays $4,000 per month.",
            "This unpaid internship... actually, it carries a $900 a week stipend.",
            "The internship is unpaid in name only; the stipend is $2,500 a month.",
            "This is an unpaid internship role with a stipend of $1,200.",
        ):
            with self.subTest(text=text):
                self.assertEqual(_pay_reasons(text, paid_only=True), [])

    def test_a_stated_wage_beats_the_word_unpaid_elsewhere(self):
        # A posting that states an hourly wage is not unpaid, whatever else it says about other programs.
        self.assertEqual(
            _pay_reasons("Pays $25 per hour. Our volunteer program is separate.", paid_only=True), []
        )


class ReviewFindingsTests(unittest.TestCase):
    """Found in the independent review of this branch; each failed on the code before its fix."""

    UNPAID = "-35 unpaid, and you asked for paid roles only"

    def test_a_stated_salary_is_not_judged_by_a_smaller_hourly_extra(self):
        for text in (
            "Salary range: $95,000 - $120,000 per year. Night shift differential of $2.00 per hour.",
            "Compensation: $80K per year. Overtime is paid at $1.50 per hour above base.",
            "Pay: $6,000 per month. Weekend premium of $3 per hour.",
        ):
            with self.subTest(text=text):
                self.assertEqual(_pay_reasons(text, minimum_hourly=25), [])

    def test_a_parking_donation_or_reimbursement_rate_is_not_the_wage(self):
        for text in (
            "Garage parking costs $3 per hour.",
            "We donate $10 per hour you volunteer.",
            "Mileage and travel are reimbursed at $8 per hour of driving.",
            "Night shift differential: $2 per hour.",
        ):
            with self.subTest(text=text):
                self.assertEqual(_pay_reasons(text, minimum_hourly=25), [])
        self.assertEqual(
            _pay_reasons("Garage parking costs $3 per hour. The internship pays $18 per hour.", minimum_hourly=25),
            ["-15 pays $18/hour, below your $25/hour minimum"],
        )

    def test_another_currency_symbol_is_not_dollars(self):
        self.assertEqual(_pay_reasons("Pay: CA$30 per hour.", minimum_hourly=40), [])
        self.assertEqual(_pay_reasons("Pay: A$30 per hour.", minimum_hourly=40), [])
        self.assertEqual(
            _pay_reasons("Pay: US$30 per hour.", minimum_hourly=40),
            ["-15 pays $30/hour, below your $40/hour minimum"],
        )

    def test_unpaid_leave_and_negated_unpaid_internships_are_not_an_unpaid_role(self):
        for text in (
            "Our sabbatical program offers unpaid leave after five years.",
            "The program offers unpaid time off between rotations.",
            "We never offer an unpaid internship: every intern is paid.",
            "Unlike an unpaid internship, this role is fully paid with benefits.",
            "We do not offer an unpaid internship.",
            "Compensation: $80K per year. Our family leave program offers unpaid leave.",
            "Compensation: $80K per year. Former interns describe it as an unpaid internship.",
        ):
            with self.subTest(text=text):
                self.assertEqual(_pay_reasons(text, paid_only=True), [])

    def test_an_unpaid_role_is_still_unpaid(self):
        for text in (
            "This is an unpaid internship. Parking is $5 per hour.",
            "No stipend is offered. This is an unpaid internship.",
            "No prior experience is needed for this unpaid internship.",
            "The internship is unpaid.",
        ):
            with self.subTest(text=text):
                self.assertEqual(_pay_reasons(text, paid_only=True), [self.UNPAID])


class SecondReviewTests(unittest.TestCase):
    """Found in the re-review of the first round of fixes; each failed on the code before its fix."""

    UNPAID = "-35 unpaid, and you asked for paid roles only"

    def test_a_negation_word_far_from_the_role_does_not_unmake_it_unpaid(self):
        for text in (
            "Instead of a stipend, this unpaid internship offers course credit.",
            "Rather than a salary, interns in this unpaid internship earn academic credit.",
            "If you have never worked in a lab, this unpaid internship is a great start.",
            "Although not required, this unpaid internship pairs well with a capstone.",
            "This is an unpaid, for-credit internship.",
            "This is an unpaid summer research internship.",
        ):
            with self.subTest(text=text):
                self.assertEqual(_pay_reasons(text, paid_only=True), [self.UNPAID])

    def test_words_between_unpaid_and_a_role_are_not_leave_or_a_choice(self):
        for text in (
            "Benefits include unpaid leave, internship stipends and more.",
            "We offer unpaid and paid internships.",
            "We never offer an unpaid internship.",
        ):
            with self.subTest(text=text):
                self.assertEqual(_pay_reasons(text, paid_only=True), [])

    def test_a_stipend_or_benefit_beside_an_hourly_wage_does_not_stop_the_comparison(self):
        for text in (
            "Pay: $18/hour. Housing stipend of $1,500 per month.",
            "Interns earn $18 per hour. Tuition assistance up to $5K per year.",
            "Pays $18 per hour plus a relocation allowance of $2,000 per month.",
        ):
            with self.subTest(text=text):
                self.assertEqual(
                    _pay_reasons(text, minimum_hourly=25), ["-15 pays $18/hour, below your $25/hour minimum"]
                )

    def test_a_stated_salary_still_stops_the_comparison(self):
        for text in (
            "Base salary: $95,000 per year. Shift differential of $2 per hour.",
            "Compensation: $6,000 per month. Weekend rate $3 per hour.",
        ):
            with self.subTest(text=text):
                self.assertEqual(_pay_reasons(text, minimum_hourly=25), [])


if __name__ == "__main__":
    unittest.main()
