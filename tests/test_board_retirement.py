"""A board fetch retires a posting only when it proved the posting is gone.

Retirement is not a soft state here: purge-expired deletes retired rows the
same day. So an answer that merely looks empty -- a 200 with `"jobs": []`, or a
changed shape that parses as nothing -- must not retire the board's postings,
and neither may a listing that a page cap or a partial answer cut short.
"""

from __future__ import annotations

import contextlib
import io
import unittest
import unittest.mock
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

import pipeline
from pipeline_core import paths, sources, store

SOURCE = {"kind": "greenhouse", "company": "Acme", "token": "acme"}
KEY = "greenhouse:acme"
NOW = datetime(2026, 9, 27, 12, tzinfo=timezone.utc)

try:
    import realdata_guard
except ImportError:  # imported as tests.<module>, with tests/ not on sys.path
    from tests import realdata_guard
# Guards this module's own run against opening data/*.db (AGENTS.md hard rule 1); see tests/realdata_guard.py.
realdata_guard.install()


def posting(job_id: int, title: str = "Mechanical Engineering Intern") -> dict:
    return {
        "id": job_id,
        "title": title,
        "location": {"name": "Austin, TX"},
        "absolute_url": f"https://boards.greenhouse.io/acme/jobs/{job_id}",
        "content": "Design fixtures and test assemblies. " * 10,
        "updated_at": "2026-09-20T00:00:00Z",
    }


def iso(moment: datetime) -> str:
    return moment.replace(microsecond=0).isoformat()


class BoardRetirementTests(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = unittest.mock.patch.object(paths, "DB_PATH", Path(tmp.name) / "pipeline.db")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.conn = store.connect()
        self.addCleanup(self.conn.close)

    def fetch(self, body, seen: datetime = NOW) -> sources.Listing:
        """One board fetch answering `body`, recorded the way fetch_all records it."""
        with unittest.mock.patch.object(sources, "request_json", return_value=body):
            records = sources.greenhouse_jobs(SOURCE, ["intern"])
        with contextlib.redirect_stdout(io.StringIO()):
            store.upsert_jobs(self.conn, KEY, "Acme", records, iso(seen))
        self.conn.execute(
            "INSERT INTO fetch_runs(source_key, started_at, finished_at, outcome, fetched_count, listed_count) "
            "VALUES (?, ?, ?, 'success', ?, ?)",
            (KEY, iso(seen), iso(seen), len(records), records.listed),
        )
        self.conn.commit()
        return records

    def active(self) -> set[str]:
        return {
            row["external_id"]
            for row in self.conn.execute("SELECT external_id FROM jobs WHERE active=1")
        }

    def test_an_empty_answer_retires_nothing(self):
        self.fetch({"jobs": [posting(1), posting(2), posting(3)]})
        self.conn.execute("UPDATE jobs SET status='shortlisted' WHERE external_id='1'")

        for body in ({"jobs": []}, {}):
            with self.subTest(body=body):
                records = self.fetch(body)
                self.assertEqual(records.listed, 0)
                self.assertEqual(self.active(), {"1", "2", "3"})

        with contextlib.redirect_stdout(io.StringIO()):
            tally = pipeline.purge_expired(self.conn, dry_run=True)
        self.assertEqual(tally["retired"], 0)

    def test_a_board_that_stays_empty_retires_once_the_streak_and_grace_are_met(self):
        self.fetch({"jobs": [posting(1)]}, NOW - timedelta(days=4))
        self.fetch({"jobs": []}, NOW - timedelta(days=2))
        self.fetch({"jobs": []}, NOW - timedelta(days=1))
        self.assertEqual(self.active(), {"1"})

        # Third empty answer in a row, and the posting was last listed four days ago.
        self.fetch({"jobs": []}, NOW)
        self.assertEqual(self.active(), set())

    def test_an_empty_streak_does_not_retire_a_posting_seen_within_the_grace(self):
        # A laptop asleep for days, then three quick empty answers: the posting
        # was listed an hour before the first, so it is not yet proven gone.
        self.fetch({"jobs": [posting(1)]}, NOW - timedelta(hours=4))
        for hours in (3, 2, 1):
            self.fetch({"jobs": []}, NOW - timedelta(hours=hours))
        self.assertEqual(self.active(), {"1"})

    def test_a_board_still_listing_other_jobs_retires_a_closed_internship_at_once(self):
        self.fetch({"jobs": [posting(1), posting(2), posting(10, "Senior Engineer")]})
        # The board answered with real postings, so absence here is evidence.
        self.fetch({"jobs": [posting(2), posting(10, "Senior Engineer")]})
        self.assertEqual(self.active(), {"2"})

    def test_a_listing_that_shrank_by_half_retires_only_postings_past_the_grace(self):
        others = [posting(100 + n, "Senior Engineer") for n in range(20)]
        self.fetch({"jobs": [posting(1), *others]}, NOW - timedelta(days=3))
        self.fetch({"jobs": [posting(1), posting(2), *others]}, NOW - timedelta(hours=6))
        # A partial answer: 3 of the 22 postings listed six hours ago.
        self.fetch({"jobs": [posting(3), *others[:2]]}, NOW)
        # Posting 1 was last seen six hours ago, posting 2 likewise; both are kept.
        self.assertEqual(self.active(), {"1", "2", "3"})

        self.conn.execute(
            "UPDATE jobs SET last_seen_at=? WHERE external_id='1'", (iso(NOW - timedelta(days=3)),)
        )
        self.fetch({"jobs": [posting(3), *others[:2]]}, NOW + timedelta(hours=1))
        # The small listing is now the baseline, so absence counts again.
        self.assertEqual(self.active(), {"3"})

    def test_a_plain_batch_still_retires_what_it_omits(self):
        # CSV, email and agent imports vouch for their whole batch.
        self.fetch({"jobs": [posting(1)]})
        with contextlib.redirect_stdout(io.StringIO()):
            store.upsert_jobs(self.conn, KEY, "Acme", [], iso(NOW))
        self.assertEqual(self.active(), set())

    def test_fetch_all_records_how_many_postings_the_board_listed(self):
        config = {"discovery_title_terms": ["intern"], "ats_sources": [SOURCE]}
        body = {"jobs": [posting(1), posting(10, "Senior Engineer"), posting(11, "Recruiter")]}
        with unittest.mock.patch.object(sources, "request_json", return_value=body), contextlib.redirect_stdout(
            io.StringIO()
        ):
            pipeline.fetch_all(self.conn, config)
        row = self.conn.execute("SELECT fetched_count, listed_count FROM fetch_runs").fetchone()
        self.assertEqual((row["fetched_count"], row["listed_count"]), (1, 3))


class WorkdayPagingTests(unittest.TestCase):
    SOURCE = {"company": "NVIDIA", "tenant": "nvidia", "datacenter": "wd5", "site": "External"}

    @staticmethod
    def page(titles: list[str], total: int, start: int) -> dict:
        return {
            "total": total,
            "jobPostings": [
                {"title": title, "externalPath": f"/job/{start + n}", "locationsText": "Santa Clara, CA"}
                for n, title in enumerate(titles)
            ],
        }

    def test_reads_past_the_first_page_though_later_pages_report_no_total(self):
        # Workday sends `total` on the first page and 0 on every later one.
        pages = [
            self.page(["Hardware Intern"] * 20, 45, 0),
            self.page(["Software Intern"] * 20, 0, 20),
            self.page(["Firmware Intern"] * 5, 0, 40),
        ]
        with unittest.mock.patch.object(sources, "request_json_post", side_effect=pages):
            jobs = sources.workday_jobs(self.SOURCE, ["intern"])
        self.assertEqual(len(jobs), 45)
        self.assertEqual(jobs.listed, 45)
        self.assertTrue(jobs.complete)

    def test_stops_a_term_after_pages_without_matches_and_marks_the_listing_partial(self):
        pages = [
            self.page(["Hardware Intern"] * 20, 900, 0),
            self.page(["Director, Internal Audit"] * 20, 0, 20),
            self.page(["Sales Engineer"] * 20, 0, 40),
        ]
        with unittest.mock.patch.object(sources, "request_json_post", side_effect=pages) as post:
            jobs = sources.workday_jobs(self.SOURCE, ["intern"])
        self.assertEqual(post.call_count, 3)
        self.assertEqual(len(jobs), 20)
        self.assertFalse(jobs.complete)


if __name__ == "__main__":
    unittest.main()
