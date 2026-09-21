"""Greenhouse boards are read in one request, not one per posting.

`greenhouse_jobs` used to fetch the board listing and then call back once per
candidate posting, solely to get its description. On 2026-09-20 that was 262
extra requests to one host. The listing returns the same description for every
job when asked with `?content=true`; checked against 13 live boards, all 257
resulting records were field-for-field identical to the per-job calls.

Nothing tested this function before, so a mistake here -- dropping postings,
storing descriptions still HTML-escaped, or losing them entirely -- would have
passed the suite and quietly degraded every fit score.
"""

from __future__ import annotations

import unittest
import unittest.mock

import pipeline

BASE = "https://boards-api.greenhouse.io/v1/boards/acme"
TERMS = ["intern", "co-op"]


def job(job_id, title, *, content=True, location="Austin, TX"):
    item = {
        "id": job_id,
        "title": title,
        "location": {"name": location},
        "absolute_url": f"https://boards.greenhouse.io/acme/jobs/{job_id}",
        "updated_at": "2026-09-01T12:00:00-04:00",
    }
    if content:
        # Greenhouse sends descriptions HTML-escaped, exactly like this.
        item["content"] = (
            "&lt;div class=&quot;intro&quot;&gt;&lt;p&gt;Design mechanisms in "
            f"SolidWorks for {title}.&lt;/p&gt;&lt;/div&gt;"
        )
    return item


class GreenhouseFetchTests(unittest.TestCase):
    def fetch(self, jobs, details=None):
        calls = []

        def fake(url, retries=2):
            calls.append(url)
            if url == f"{BASE}/jobs?content=true":
                return {"jobs": jobs}
            for detail in details or []:
                if url == f"{BASE}/jobs/{detail['id']}":
                    return detail
            raise AssertionError(f"unexpected request {url}")

        with unittest.mock.patch.object(pipeline, "request_json", side_effect=fake):
            records = pipeline.greenhouse_jobs(
                {"kind": "greenhouse", "company": "Acme", "token": "acme"}, TERMS
            )
        return records, calls

    def test_a_whole_board_is_read_in_one_request(self):
        records, calls = self.fetch([
            job(1, "Mechanical Engineering Intern"),
            job(2, "Controls Co-op"),
            job(3, "Test Engineering Intern"),
        ])
        self.assertEqual(calls, [f"{BASE}/jobs?content=true"])
        self.assertEqual(len(records), 3)

    def test_only_discovery_candidates_are_kept(self):
        records, _ = self.fetch([
            job(1, "Mechanical Engineering Intern"),
            job(2, "Senior Staff Engineer"),
        ])
        self.assertEqual([r["external_id"] for r in records], ["1"])

    def test_the_description_is_unescaped_and_stripped_of_markup(self):
        """The raw listing is HTML-escaped; storing it that way would put
        `&lt;p&gt;` into every description the scorer reads."""

        records, _ = self.fetch([job(1, "Mechanical Engineering Intern")])
        description = records[0]["description"]
        self.assertIn("Design mechanisms in SolidWorks", description)
        for leftover in ("&lt;", "&gt;", "&quot;", "<p>", "<div"):
            self.assertNotIn(leftover, description)

    def test_every_field_the_pipeline_stores_is_carried_over(self):
        records, _ = self.fetch([job(7, "Mechanical Engineering Intern", location="Hawthorne, CA")])
        record = records[0]
        self.assertEqual(record["external_id"], "7")
        self.assertEqual(record["company"], "Acme")
        self.assertEqual(record["title"], "Mechanical Engineering Intern")
        self.assertEqual(record["location"], "Hawthorne, CA")
        self.assertEqual(record["url"], "https://boards.greenhouse.io/acme/jobs/7")
        self.assertEqual(record["posted_at"], "2026-09-01T12:00:00-04:00")

    def test_a_posting_listed_without_a_description_is_fetched_individually(self):
        """Never store an empty description when the board can supply one.

        Scoring reads the description, so if the listing ever stopped carrying
        content, silently storing blanks would degrade every score with nothing
        failing. Only the affected posting costs an extra request.
        """

        detail = job(2, "Controls Co-op")
        records, calls = self.fetch(
            [job(1, "Mechanical Engineering Intern"), job(2, "Controls Co-op", content=False)],
            details=[detail],
        )
        self.assertEqual(calls, [f"{BASE}/jobs?content=true", f"{BASE}/jobs/2"])
        by_id = {r["external_id"]: r for r in records}
        self.assertIn("Design mechanisms", by_id["2"]["description"])
        self.assertIn("Design mechanisms", by_id["1"]["description"])

    def test_an_empty_board_makes_one_request_and_returns_nothing(self):
        records, calls = self.fetch([])
        self.assertEqual(records, [])
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
