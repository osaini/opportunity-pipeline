"""The pay reader accepts the explicit ways a posting writes a wage, and still refuses a bare figure.

"$45 - $55 USD per hour", "$38 an hour" and "$180,000 - $200,000 USD per year" state an amount, a currency and a period,
and each read as no pay at all. A figure with no period ("$180,000 - $200,000 USD", "$15,000 housing stipend") is still
not pay: reading it as a salary would present an inferred value as confirmed.
"""

from __future__ import annotations

import time
import unittest

from opportunity_app.opportunity_metadata import extract_opportunity_metadata
from pipeline_core import scoring

try:
    import realdata_guard
except ImportError:  # imported as tests.<module>, with tests/ not on sys.path
    from tests import realdata_guard
# Guards this module's own run against opening data/*.db (AGENTS.md hard rule 1); see tests/realdata_guard.py.
realdata_guard.install()


def _pay(description):
    meta = extract_opportunity_metadata("Engineer", "Austin, TX", description)
    return meta["pay_period"], meta["pay_min"], meta["pay_max"]


class ExplicitWageTests(unittest.TestCase):
    def test_a_currency_code_before_the_period_is_still_a_stated_period(self):
        self.assertEqual(_pay("Pay: $45 - $55 USD per hour."), ("hour", 45.0, 55.0))
        self.assertEqual(_pay("Pay: $180,000 - $200,000 USD per year."), ("year", 180000.0, 200000.0))
        self.assertEqual(_pay("Pay: $180,000 USD annually."), ("year", 180000.0, 180000.0))

    def test_an_hour_reads_like_per_hour(self):
        self.assertEqual(_pay("You will earn $38 an hour."), ("hour", 38.0, 38.0))
        self.assertEqual(_pay("You will earn $22.50 an hr."), ("hour", 22.5, 22.5))

    def test_the_forms_that_already_worked_still_do(self):
        self.assertEqual(_pay("$25 - $30 per hour"), ("hour", 25.0, 30.0))
        self.assertEqual(_pay("$20.50/hr"), ("hour", 20.5, 20.5))
        self.assertEqual(_pay("$180,000-$200,000 per year"), ("year", 180000.0, 200000.0))
        self.assertEqual(_pay("$120,000 a year"), ("year", 120000.0, 120000.0))

    def test_a_figure_with_no_period_is_not_pay_even_with_a_currency_code(self):
        for text in (
            "$180,000 - $200,000 USD",
            "Base salary range: $180,000 - $200,000 USD plus equity.",
            "A $15,000 USD housing stipend.",
            "We raised $120,000,000 USD in funding.",
            "$45 - $55 USD",
        ):
            with self.subTest(text=text):
                self.assertEqual(_pay(text), ("", None, None))

    def test_another_currency_is_not_read_as_dollars(self):
        for text in ("$45 CAD per hour", "$180,000 CAD per year", "$45 - $55 GBP per hour"):
            with self.subTest(text=text):
                self.assertEqual(_pay(text), ("", None, None))


class LongWhitespaceTests(unittest.TestCase):
    """Found in review: three adjacent optional spaces made the pay readers cubic in a run of whitespace after a dollar figure.

    A description pasted from a CSV keeps its raw whitespace, so one posting could stall a refresh for minutes. The bound
    here is generous (seconds); the cubic pattern needed minutes at this size.
    """

    SIZE = 4000
    BOUND_SECONDS = 5.0

    def timed(self, text):
        started = time.perf_counter()
        extract_opportunity_metadata("Engineer", "Austin, TX", text)
        scoring.score_job(
            {"title": "Engineer", "description": text, "role_type": "internship", "location": "", "posted_at": None},
            {"max_years_experience": 1, "compensation_preferences": {"minimum_hourly": 25, "paid_only": True}},
        )
        return time.perf_counter() - started

    def test_a_long_run_of_whitespace_after_a_figure_is_read_in_linear_time(self):
        gap = " " * self.SIZE
        for text in (f"$5{gap}", f"$5 -{gap}", f"$12,345{gap}", f"$5{gap}$", f"$5 to{gap}x", f"$5	{gap}USD{gap}"):
            with self.subTest(text=text[:12]):
                self.assertLess(self.timed(text), self.BOUND_SECONDS)

    def test_the_readers_still_find_pay_across_ordinary_spacing(self):
        self.assertEqual(_pay("$20  -  $30   per hour"), ("hour", 20.0, 30.0))
        self.assertEqual(_pay("$20 to 30 per hour"), ("hour", 20.0, 30.0))
        self.assertEqual(_pay("$180,000  to  $200,000   USD  per  year"), ("year", 180000.0, 200000.0))
        self.assertEqual(_pay("$25 USD/hour"), ("hour", 25.0, 25.0))


if __name__ == "__main__":
    unittest.main()
