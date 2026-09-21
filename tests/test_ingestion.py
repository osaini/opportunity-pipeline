"""Tests for scheduled ingestion: CLI stages, RSS parsing, and cadence."""

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from opportunity_app.worker import enqueue_due_schedules  # noqa: E402
from opportunity_app.schema import connect_product  # noqa: E402

from helpers_platform import build_and_migrate


class PipelineStageTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.legacy_path, self.platform_path = build_and_migrate(root)

    def tearDown(self):
        self.tempdir.cleanup()

    def test_score_stage_runs_hermetically_against_fixture_db(self):
        from opportunity_app.ingestion import make_stage_handler, record_ingestion_run

        with closing(connect_product(self.platform_path)) as conn:
            handler = make_stage_handler(conn, db_path=self.legacy_path)
            result = handler({"stage": "score"})
            runs = conn.execute("SELECT stage, status FROM ingestion_runs").fetchall()
        self.assertEqual(result["status"], "success")
        self.assertEqual(runs[0]["stage"], "score")
        self.assertEqual(runs[0]["status"], "success")

    def test_failing_stage_records_failed_run(self):
        from opportunity_app.ingestion import make_stage_handler

        with closing(connect_product(self.platform_path)) as conn:
            handler = make_stage_handler(conn, db_path=self.legacy_path)
            # An unknown CLI flag makes the subprocess exit non-zero.
            with self.assertRaises(RuntimeError):
                handler({"stage": "score", "extra_args": ["--not-a-real-flag"]})
            row = conn.execute("SELECT stage, status FROM ingestion_runs ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(row["status"], "failed")

    def test_report_stage_writes_artifacts_into_temp_output(self):
        from opportunity_app.ingestion import run_pipeline_stage

        result = run_pipeline_stage("report", db_path=self.legacy_path)
        self.assertEqual(result["status"], "success")

    def test_import_discovered_stage_runs_the_cli_subcommand(self):
        """Regression: rss_discovery_handler called this stage, but it was missing
        from STAGE_COMMANDS, so every configured feed raised ValueError."""
        from opportunity_app.ingestion import run_pipeline_stage

        discovered = Path(self.tempdir.name) / "discovered.json"
        discovered.write_text(json.dumps([{
            "channel": "rss",
            "title": "Robotics Lab Undergraduate Researcher",
            "company": "UT Austin Research Listings",
            "location": "",
            "url": "https://example.edu/robotics-role",
        }]), encoding="utf-8")
        # A fresh path, so pipeline.py creates its real schema; the shared fixture's
        # minimal legacy schema lacks the unique key upsert_jobs relies on.
        legacy_db = Path(self.tempdir.name) / "import-discovered.db"
        result = run_pipeline_stage("import-discovered", db_path=legacy_db, extra_args=[str(discovered)])
        self.assertEqual(result["status"], "success", result["stderr_tail"])

    def test_rss_discovery_handler_imports_feed_entries_into_the_target_db(self):
        from opportunity_app import ingestion

        root = Path(self.tempdir.name)
        config = root / "sources.json"
        config.write_text(json.dumps({"agent_discovery": {"rss": {"feeds": ["https://example.edu/feed.xml"]}}}), encoding="utf-8")
        discovered = root / "discovered.json"
        legacy_db = root / "rss-pipeline.db"
        entries = ingestion.parse_feed(RssParsingTests.RSS)
        with closing(connect_product(self.platform_path)) as conn, \
                mock.patch.object(ingestion, "SOURCES_CONFIG", config), \
                mock.patch.object(ingestion, "fetch_rss_discoveries", return_value=(entries, [])) as fetch:
            result = ingestion.rss_discovery_handler(conn, discovered_path=discovered, db_path=legacy_db)
            run = conn.execute("SELECT stage, status FROM ingestion_runs ORDER BY id DESC LIMIT 1").fetchone()
        fetch.assert_called_once()
        self.assertEqual(result, {"fetched": 1, "new": 1, "imported_run": True})
        self.assertEqual((run["stage"], run["status"]), ("rss_import", "success"))
        with closing(sqlite3.connect(legacy_db)) as legacy:
            rows = legacy.execute("SELECT source_key, title, url FROM jobs WHERE source_key LIKE 'agent:%'").fetchall()
        self.assertEqual(
            rows,
            [("agent:rss", "Robotics Lab Undergraduate Researcher", "https://example.edu/robotics-role")],
        )


class RssParsingTests(unittest.TestCase):
    RSS = """<?xml version="1.0"?>
    <rss version="2.0"><channel>
      <title>UT Austin Research Listings</title>
      <item>
        <title>Robotics Lab Undergraduate Researcher</title>
        <link>https://example.edu/robotics-role</link>
        <pubDate>Tue, 18 Aug 2026 10:00:00 GMT</pubDate>
        <description>Assist with manipulator design in a university lab.</description>
      </item>
      <item><title></title><link>https://example.edu/empty</link></item>
    </channel></rss>"""

    ATOM = """<?xml version="1.0"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
      <title>Society Job Board</title>
      <entry>
        <title>Manufacturing Co-op</title>
        <link href="https://example.org/coop"/>
        <updated>2026-08-19T08:00:00Z</updated>
        <summary>CNC and process work for fall co-op.</summary>
      </entry>
    </feed>"""

    def test_rss_entries_normalize_to_discovered_shape(self):
        from opportunity_app.ingestion import parse_feed

        items = parse_feed(self.RSS, channel_company="UT Austin")
        self.assertEqual(len(items), 1, "entries without title or link are skipped")
        entry = items[0]
        self.assertEqual(entry["channel"], "rss")
        self.assertEqual(entry["company"], "UT Austin")
        self.assertEqual(entry["url"], "https://example.edu/robotics-role")
        self.assertIn("posted_at", entry)
        self.assertTrue(entry["posted_at"].endswith("+00:00"))
        self.assertIn("description", entry)

    def test_rss_channel_title_is_company_fallback(self):
        from opportunity_app.ingestion import parse_feed

        items = parse_feed(self.RSS)
        self.assertEqual(items[0]["company"], "UT Austin Research Listings")

    def test_atom_entries_parse_with_href_links(self):
        from opportunity_app.ingestion import parse_feed

        items = parse_feed(self.ATOM)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["url"], "https://example.org/coop")
        self.assertEqual(items[0]["title"], "Manufacturing Co-op")

    def test_malformed_xml_raises_parse_error(self):
        from opportunity_app.ingestion import parse_feed

        from xml.etree import ElementTree

        with self.assertRaises(ElementTree.ParseError):
            parse_feed("<rss><channel>")


class CadenceTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        _, self.platform_path = build_and_migrate(Path(self.tempdir.name))

    def tearDown(self):
        self.tempdir.cleanup()

    def test_no_schedule_env_means_nothing_enqueued(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PIPELINE_SCHEDULE_SCORE_HOURS", None)
            with closing(connect_product(self.platform_path)) as conn:
                enqueued = enqueue_due_schedules(conn)
                count = conn.execute("SELECT COUNT(*) FROM job_queue").fetchone()[0]
        self.assertEqual(enqueued, [])
        self.assertEqual(count, 0)

    def test_cadence_enqueues_stage_once_per_hour_bucket(self):
        env = {"PIPELINE_SCHEDULE_SCORE_HOURS": "6"}
        with mock.patch.dict(os.environ, env, clear=False):
            with closing(connect_product(self.platform_path)) as conn:
                first = enqueue_due_schedules(conn)
                second = enqueue_due_schedules(conn)
                jobs = conn.execute("SELECT job_type, state FROM job_queue").fetchall()
        self.assertEqual(first, ["score"])
        self.assertEqual(second, [], "idempotency key prevents duplicate hourly enqueues")
        self.assertEqual(jobs[0]["job_type"], "pipeline_score")
        self.assertEqual(jobs[0]["state"], "queued")


if __name__ == "__main__":
    unittest.main()
