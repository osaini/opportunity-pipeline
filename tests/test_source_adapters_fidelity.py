"""Regression guards for what the Ashby and Lever adapters kept of a posting.

Each test failed on the code before the fix:

- an Ashby job with ``isListed: false`` (hidden from the company's public board) was stored as an active opening;
- Ashby's structured pay (``compensation.summaryComponents``) was requested and then thrown away, so pay was read
  from the description alone and most Ashby postings ended up with unknown pay;
- Lever's description kept only the opening paragraph and the closing note, dropping the bulleted lists where a
  posting says "What we require", so a years or sponsorship gate in those bullets never reached the score;
- Lever's ``createdAt`` was not read, so no Lever posting had a posting date and none was ever fresh or stale.

The record shapes below follow the live APIs (checked 2026-10-04 against one public Ashby board).
"""

from __future__ import annotations

import unittest
import unittest.mock

from opportunity_app.opportunity_metadata import extract_opportunity_metadata
from pipeline_core import sources as core_sources

try:
    import realdata_guard
except ImportError:  # imported as tests.<module>, with tests/ not on sys.path
    from tests import realdata_guard
# Guards this module's own run against opening data/*.db (AGENTS.md hard rule 1); see tests/realdata_guard.py.
realdata_guard.install()


def _ashby(**overrides):
    job = {
        "id": "j1",
        "title": "Software Engineer Intern",
        "location": "San Francisco, CA",
        "publishedAt": "2026-09-01T00:00:00.000+00:00",
        "jobUrl": "https://jobs.ashbyhq.com/acme/j1",
        "descriptionHtml": "<p>Build things.</p>",
        "isListed": True,
    }
    job.update(overrides)
    return job


def _salary(low, high, interval="1 YEAR", currency="USD"):
    return {
        "compensationTierSummary": "$1 – $2 • Offers Equity",
        "summaryComponents": [
            {"compensationType": "EquityPercentage", "interval": "NONE", "currencyCode": None, "minValue": None, "maxValue": None},
            {"compensationType": "Salary", "interval": interval, "currencyCode": currency, "minValue": low, "maxValue": high},
        ],
    }


def _ashby_jobs(*jobs):
    with unittest.mock.patch.object(core_sources, "request_json", return_value={"jobs": list(jobs)}):
        return core_sources.ashby_jobs({"company": "Acme", "board": "acme"}, ["intern"])


class AshbyListedTests(unittest.TestCase):
    def test_a_job_hidden_from_the_public_board_is_not_stored(self):
        jobs = _ashby_jobs(_ashby(), _ashby(id="j2", isListed=False))
        self.assertEqual([job["external_id"] for job in jobs], ["j1"])

    def test_a_job_with_no_listed_flag_is_kept(self):
        job = _ashby()
        del job["isListed"]
        self.assertEqual(len(_ashby_jobs(job)), 1)

    def test_the_unlisted_job_still_counts_toward_what_the_board_returned(self):
        jobs = _ashby_jobs(_ashby(), _ashby(id="j2", isListed=False))
        self.assertEqual(jobs.listed, 2)


class AshbyPayTests(unittest.TestCase):
    def pay(self, compensation):
        (job,) = _ashby_jobs(_ashby(compensation=compensation))
        meta = extract_opportunity_metadata(job["title"], job["location"], job["description"])
        return meta["pay_period"], meta["pay_min"], meta["pay_max"], job["description"]

    def test_a_yearly_salary_is_read_from_the_structured_field(self):
        period, low, high, _ = self.pay(_salary(211400, 290600))
        self.assertEqual((period, low, high), ("year", 211400.0, 290600.0))

    def test_an_hourly_salary_is_read_from_the_structured_field(self):
        period, low, high, _ = self.pay(_salary(45, 55.5, interval="1 HOUR"))
        self.assertEqual((period, low, high), ("hour", 45.0, 55.5))

    def test_the_description_says_where_the_pay_came_from(self):
        _, _, _, description = self.pay(_salary(120000, 120000))
        self.assertIn("Pay listed on the Ashby posting: $120,000 per year.", description)
        self.assertTrue(description.startswith("Build things."))

    def test_pay_in_another_currency_is_not_labelled_dollars(self):
        # The metadata reader names any pay it finds USD, so a CAD or GBP figure must stay out of the text.
        for currency in ("CAD", "GBP", None):
            with self.subTest(currency=currency):
                period, low, high, description = self.pay(_salary(90000, 120000, currency=currency))
                self.assertEqual((period, low, high), ("", None, None))
                self.assertNotIn("Pay listed", description)

    def test_a_period_the_reader_does_not_know_is_left_out(self):
        period, _, _, description = self.pay(_salary(9000, 12000, interval="1 MONTH"))
        self.assertEqual(period, "")
        self.assertNotIn("Pay listed", description)

    def test_equity_bonus_and_commission_are_not_pay(self):
        compensation = {"summaryComponents": [
            {"compensationType": kind, "interval": "1 YEAR", "currencyCode": "USD", "minValue": 10000, "maxValue": 50000}
            for kind in ("Bonus", "Commission", "EquityCashValue")
        ]}
        period, _, _, description = self.pay(compensation)
        self.assertEqual(period, "")
        self.assertNotIn("Pay listed", description)

    def test_a_components_value_that_is_not_a_list_does_not_break_the_fetch(self):
        for components in (5, "x", {"a": 1}, True):
            with self.subTest(components=components):
                period, _, _, description = self.pay({"summaryComponents": components})
                self.assertEqual((period, description), ("", "Build things."))

    def test_a_posting_with_no_compensation_is_unchanged(self):
        for compensation in (None, {}, {"compensationTierSummary": "", "summaryComponents": []}):
            with self.subTest(compensation=compensation):
                period, _, _, description = self.pay(compensation)
                self.assertEqual((period, description), ("", "Build things."))

    def test_a_posting_with_pay_but_no_description_still_reads_as_no_description(self):
        # Found in review: the pay sentence alone made the description non-empty, so "-3 description unavailable"
        # no longer fired for an Ashby posting with no text.
        from pipeline_core import scoring

        for html in ("", None):
            with self.subTest(html=html):
                (job,) = _ashby_jobs(_ashby(descriptionHtml=html, compensation=_salary(90000, 120000)))
                self.assertIn("Pay listed on the Ashby posting", job["description"])
                _, reasons = scoring.score_job({**job, "role_type": "internship"}, {"max_years_experience": 1})
                self.assertIn("-3 description unavailable", reasons)
        (job,) = _ashby_jobs(_ashby(compensation=_salary(90000, 120000)))
        _, reasons = scoring.score_job({**job, "role_type": "internship"}, {"max_years_experience": 1})
        self.assertNotIn("-3 description unavailable", reasons)

    def test_ashby_multiple_ranges_wording_is_kept(self):
        # Found in review: a posting with several pay ranges (by level or place) read as one plain range.
        compensation = _salary(90000, 160000)
        compensation["compensationTierSummary"] = "$90K – $160K • Offers Equity • Multiple Ranges"
        period, low, high, description = self.pay(compensation)
        self.assertEqual((period, low, high), ("year", 90000.0, 160000.0))
        self.assertIn("Pay listed on the Ashby posting: $90,000 - $160,000 per year (Multiple Ranges).", description)
        _, _, _, plain = self.pay(_salary(90000, 160000))
        self.assertNotIn("Multiple Ranges", plain)

    def test_a_malformed_component_does_not_break_the_fetch(self):
        compensation = {"summaryComponents": [
            None, "x", {"compensationType": "Salary", "minValue": "a", "maxValue": []},
            {"compensationType": "Salary", "interval": ["1 YEAR"], "currencyCode": "USD", "minValue": 1, "maxValue": 2},
        ]}
        period, _, _, description = self.pay(compensation)
        self.assertEqual((period, description), ("", "Build things."))


def _lever(**overrides):
    item = {
        "id": "l1",
        "text": "Software Engineer Intern",
        "categories": {"location": "Austin, TX"},
        "hostedUrl": "https://jobs.lever.co/acme/l1",
        "descriptionPlain": "Join our team.",
        "additionalPlain": "We are an equal opportunity employer.",
        "lists": [
            {"text": "What you'll do", "content": "<li>Write services</li><li>Review code</li>"},
            {"text": "What we require", "content": "<li>3+ years of professional experience</li><li>No visa sponsorship is offered</li>"},
        ],
        "createdAt": 1788220800000,  # 2026-09-01T00:00:00Z
    }
    item.update(overrides)
    return item


def _lever_jobs(*items):
    with unittest.mock.patch.object(core_sources, "request_json", return_value=list(items)):
        return core_sources.lever_jobs({"company": "Acme", "site": "acme"}, ["intern"])


class LeverTests(unittest.TestCase):
    def test_the_bulleted_lists_are_part_of_the_description(self):
        (job,) = _lever_jobs(_lever())
        for text in ("What you'll do", "Review code", "What we require", "3+ years of professional experience"):
            self.assertIn(text, job["description"])

    def test_the_lists_sit_between_the_opening_and_the_closing_note(self):
        (job,) = _lever_jobs(_lever())
        description = job["description"]
        self.assertLess(description.index("Join our team."), description.index("What we require"))
        self.assertLess(description.index("What we require"), description.index("equal opportunity"))

    def test_a_requirement_in_a_list_now_reaches_the_score(self):
        from pipeline_core import scoring

        (job,) = _lever_jobs(_lever())
        profile = {"max_years_experience": 1, "requires_sponsorship": True}
        _, reasons = scoring.score_job({**job, "role_type": "internship"}, profile)
        self.assertIn("-18 asks for 3+ years", reasons)
        self.assertIn("-35 sponsorship appears unavailable", reasons)

    def test_a_posting_without_lists_is_unchanged(self):
        for lists in (None, [], [None, {"text": "", "content": ""}]):
            with self.subTest(lists=lists):
                (job,) = _lever_jobs(_lever(lists=lists))
                self.assertEqual(job["description"], "Join our team. We are an equal opportunity employer.")

    def test_plain_text_lines_stay_apart(self):
        # Found in review: descriptionPlain puts each requirement on its own line with a bare newline, which was collapsed,
        # so one line read as the tail of the one before.
        from pipeline_core import scoring

        plain = "What you'll need\n1+ years of hands-on experience\nFollowing graduation you may join our rotational program"
        (job,) = _lever_jobs(_lever(descriptionPlain=plain, lists=[]))
        self.assertIn("1+ years of hands-on experience\nFollowing graduation", job["description"])
        profile = {"max_years_experience": 1, "graduation_year": 2027}
        _, reasons = scoring.score_job({**job, "role_type": "internship"}, profile)
        self.assertFalse([reason for reason in reasons if "years" in reason])

    def test_the_posting_date_is_the_creation_time(self):
        (job,) = _lever_jobs(_lever())
        self.assertEqual(job["posted_at"], "2026-09-01T00:00:00+00:00")

    def test_a_lists_value_that_is_not_a_list_does_not_break_the_fetch(self):
        for lists in (5, "x", {"text": "a"}, True):
            with self.subTest(lists=lists):
                (job,) = _lever_jobs(_lever(lists=lists))
                self.assertEqual(job["description"], "Join our team. We are an equal opportunity employer.")

    def test_a_missing_or_unusable_creation_time_stays_unknown(self):
        for created in (None, 0, "soon", -5, True, float("nan"), 10 ** 20):
            with self.subTest(created=created):
                (job,) = _lever_jobs(_lever(createdAt=created))
                self.assertIsNone(job["posted_at"])


if __name__ == "__main__":
    unittest.main()
