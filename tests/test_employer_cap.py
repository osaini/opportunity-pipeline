"""Ranked views show each employer's top postings and say how many more it has.

The read model's own cap is pinned in test_read_model_contract.py on both
repository paths. These cover the two ranked views built on it: the API the
Discover deck reads, and the CLI's Markdown shortlist.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi.testclient import TestClient

import pipeline
from helpers_platform import LEGACY_SCHEMA, build_profile
from opportunity_app import STATIC_DIR
from opportunity_app.api import create_app
from opportunity_app.schema import migrate_legacy_database
from pipeline_core import RANKED_VIEW_PER_COMPANY


def legacy_job(job_id: str, company: str, score: int) -> tuple:
    return (
        job_id, "greenhouse:test", "Test Board", job_id, company, f"Mechanical Intern {job_id}",
        "Remote", "internship", f"https://example.com/{job_id}", "CAD work.", None,
        "2026-08-10T00:00:00+00:00", "2026-08-10T00:00:00+00:00", 1, f"fp-{job_id}", f"cfp-{job_id}",
        None, score, "[]", "discovered", "", None, None,
    )


class EmployerCapApiTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        legacy = root / "pipeline.db"
        self.platform = root / "platform.db"
        rows = [legacy_job(f"big-{index}", "Bigco", 90 - index) for index in range(8)]
        rows += [legacy_job("small-0", "Smallco", 50)]
        with closing(sqlite3.connect(legacy)) as conn:
            conn.executescript(LEGACY_SCHEMA)
            conn.executemany("INSERT INTO jobs VALUES(" + ",".join("?" * 23) + ")", rows)
            conn.commit()
        migrate_legacy_database(legacy, self.platform, build_profile(root))
        app = create_app(db_path=self.platform, access_token="cap-secret", static_dir=STATIC_DIR)
        self.client = TestClient(app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.client.post("/api/v1/session", json={"token": "cap-secret"})

    def listing(self, **params):
        response = self.client.get("/api/v1/opportunities", params={"limit": 50, **params})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_the_default_list_is_uncapped(self):
        payload = self.listing()
        self.assertEqual(payload["total"], 9)
        self.assertEqual(payload["per_company"], 0)
        self.assertNotIn("company_total", payload["items"][0])

    def test_per_company_caps_each_employer_and_reports_what_it_holds_back(self):
        payload = self.listing(per_company=RANKED_VIEW_PER_COMPANY)
        self.assertEqual(payload["per_company"], RANKED_VIEW_PER_COMPANY)
        self.assertEqual(payload["total"], RANKED_VIEW_PER_COMPANY + 1)
        bigco = [item for item in payload["items"] if item["company"] == "Bigco"]
        self.assertEqual(len(bigco), RANKED_VIEW_PER_COMPANY)
        self.assertEqual({item["company_total"] for item in bigco}, {8})
        self.assertEqual(bigco[-1]["company_rank"], RANKED_VIEW_PER_COMPANY)

    def test_a_company_filter_lists_every_posting_and_lifts_the_cap(self):
        payload = self.listing(company="bigco", per_company=RANKED_VIEW_PER_COMPANY)
        self.assertEqual(payload["total"], 8)
        self.assertEqual(payload["per_company"], 0)
        self.assertEqual({item["company"] for item in payload["items"]}, {"Bigco"})

    def test_per_company_is_validated(self):
        for value in (-1, 51, "five"):
            with self.subTest(value=value):
                response = self.client.get("/api/v1/opportunities", params={"per_company": value})
                self.assertEqual(response.status_code, 422)
        response = self.client.get("/api/v1/opportunities", params={"company": "x" * 201})
        self.assertEqual(response.status_code, 422)


class EmployerCapShortlistTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        for name, value in (
            ("DB_PATH", self.root / "pipeline.db"),
            ("ROOT", self.root),
            ("OUTPUT_MD", self.root / "output" / "shortlist.md"),
            ("OUTPUT_CSV", self.root / "output" / "shortlist.csv"),
        ):
            patcher = mock.patch.object(pipeline, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.conn = pipeline.connect()
        self.addCleanup(self.conn.close)

    def add(self, company: str, scores: list[int], start: int = 0):
        # `start` keeps titles distinct across employers the dedupe would
        # otherwise fold together.
        records = [
            {
                "external_id": f"{company}-{index}",
                "company": company,
                "title": f"Mechanical Intern {index}",
                "location": "Remote",
                "url": f"https://example.com/{company}/{index}",
                "description": "",
            }
            for index in range(start, start + len(scores))
        ]
        pipeline.upsert_jobs(self.conn, f"greenhouse:{company}", company, records)
        for index, score in enumerate(scores, start=start):
            self.conn.execute(
                "UPDATE jobs SET score=? WHERE external_id=?", (score, f"{company}-{index}")
            )
        self.conn.commit()

    def write(self, limit: int) -> str:
        with redirect_stdout(StringIO()):
            pipeline.report(self.conn, {"manual_check_sources": []}, limit)
        return (self.root / "output" / "shortlist.md").read_text(encoding="utf-8")

    def listed_companies(self, markdown: str) -> list[str]:
        return [line.rsplit(" — ", 1)[1].split(" (")[0] for line in markdown.splitlines() if line.startswith("### ")]

    def test_the_shortlist_keeps_each_employers_top_five_and_says_how_many_more(self):
        self.add("Bigco", [95, 94, 93, 92, 91, 90, 89, 88])
        self.add("Smallco", [70, 60])
        markdown = self.write(limit=20)
        self.assertEqual(self.listed_companies(markdown), ["Bigco"] * 5 + ["Smallco"] * 2)
        self.assertIn("+3 more from Bigco", markdown)
        self.assertNotIn("more from Smallco", markdown)
        # The note sits right after Bigco's last listed posting.
        lines = [line for line in markdown.splitlines() if line.startswith(("### ", "*+"))]
        self.assertTrue(lines[5].startswith("*+3 more from Bigco"), lines)

    def test_the_limit_fills_from_other_employers_and_the_csv_stays_uncapped(self):
        self.add("Bigco", [95, 94, 93, 92, 91, 90, 89, 88])
        self.add("Smallco", [70, 60])
        markdown = self.write(limit=6)
        self.assertEqual(self.listed_companies(markdown), ["Bigco"] * 5 + ["Smallco"])
        csv_rows = (self.root / "output" / "shortlist.csv").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(csv_rows), 1 + 6)
        self.assertTrue(all(",Bigco," in row for row in csv_rows[1:]))

    def test_an_employer_cut_short_by_the_limit_gets_no_note(self):
        self.add("Bigco", [95, 94, 93, 92, 91, 90])
        markdown = self.write(limit=3)
        self.assertEqual(self.listed_companies(markdown), ["Bigco"] * 3)
        self.assertNotIn("more from", markdown)

    def test_employers_are_grouped_by_the_same_fold_as_the_web_view(self):
        self.add("Bigco", [95, 94, 93])
        self.add("BIGCO", [92, 91, 90], start=3)
        markdown = self.write(limit=20)
        self.assertEqual(len(self.listed_companies(markdown)), RANKED_VIEW_PER_COMPANY)
        self.assertIn("+1 more from", markdown)


if __name__ == "__main__":
    unittest.main()
