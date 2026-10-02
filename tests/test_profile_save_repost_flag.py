"""Saving the profile in the web app must not delete the repost FLAG from score explanations.

The refresh (``pipeline_core.scoring.score_all``) appends "FLAG: this role has been
listed under N different URLs since ..." to a posting whose role was retired and
came back at a new URL, and the sync copies it into ``fit_scores.explanation_json``.
``student.profile._compute_scores`` re-scored with ``score_job`` alone and overwrote
that row without the flag, so a re-listed posting looked fresh until the next
refresh, and the explanation depended on which write ran last.
"""

import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi.testclient import TestClient

from opportunity_app import STATIC_DIR
from opportunity_app.api import create_app
from opportunity_app.core.schema import LOCAL_USER_ID
from opportunity_app.opportunities.legacy_sync import migrate_legacy_database
from opportunity_app.student import profile as profile_module
from pipeline_core import paths, scoring, store

from helpers_platform import build_and_migrate, build_profile, fast_throwaway_databases

try:
    import realdata_guard
except ImportError:  # imported as tests.<module>, with tests/ not on sys.path
    from tests import realdata_guard
# Guards this module's own run against opening data/*.db (AGENTS.md hard rule 1); see tests/realdata_guard.py.
realdata_guard.install()

OWNER = {"Authorization": "Bearer repost-owner"}
FLAG_PREFIX = "FLAG: this role has been listed under"


def _record(external_id: str, url: str, title: str) -> dict:
    return {
        "external_id": external_id,
        "company": "Relist Robotics",
        "title": title,
        "location": "Austin, TX",
        "url": url,
        "description": f"Mechanical design with SolidWorks, listing {external_id}.",
    }


def _flags(explanation_json: str) -> list[str]:
    return [reason for reason in json.loads(explanation_json) if reason.startswith(FLAG_PREFIX)]


class ProfileSaveKeepsRepostFlagTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.legacy_path = self.root / "pipeline.db"
        self.platform_path = self.root / "platform.db"
        self.profile_path = build_profile(self.root)
        self.relisted_id, self.retired_id = self._seed_legacy_and_migrate()

    def _seed_legacy_and_migrate(self) -> tuple[str, str]:
        """A role retired at one URL and listed again at another, scored and synced by the refresh."""

        profile = json.loads(self.profile_path.read_text(encoding="utf-8"))
        long_ago = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        with mock.patch.object(paths, "DB_PATH", self.legacy_path):
            conn = store.connect()
            try:
                store.upsert_jobs(
                    conn, "greenhouse:relist", "Relist Robotics",
                    [_record("1", "https://boards.example.test/relist/1", "Mechanical Intern (Summer 2026)")],
                    seen=long_ago, dedupe=False,
                )
                conn.execute("UPDATE jobs SET active=0")
                store.upsert_jobs(
                    conn, "greenhouse:relist", "Relist Robotics",
                    [_record("2", "https://boards.example.test/relist/2", "Mechanical Intern (Summer 2027)")],
                    dedupe=False,
                )
                conn.commit()
                scoring.score_all(conn, profile)
                ids = {row["external_id"]: row["id"] for row in conn.execute("SELECT id, external_id FROM jobs")}
            finally:
                conn.close()
        with fast_throwaway_databases():
            migrate_legacy_database(self.legacy_path, self.platform_path, self.profile_path)
        return ids["2"], ids["1"]

    def _client(self) -> TestClient:
        app = create_app(
            db_path=self.platform_path,
            access_token="repost-owner",
            admin_token="repost-admin",
            static_dir=STATIC_DIR,
            resume_storage=self.root / "resumes",
            capture_storage=self.root / "captures",
            interview_storage=self.root / "mock-interviews",
            profile_file=self.profile_path,
            rate_limit_per_minute=10**6,
        )
        client = TestClient(app, raise_server_exceptions=False)
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        return client

    def _explanation(self, opportunity_id: str) -> str:
        with closing(sqlite3.connect(self.platform_path)) as conn:
            row = conn.execute(
                "SELECT explanation_json FROM fit_scores WHERE opportunity_id=? AND user_id=?",
                (opportunity_id, LOCAL_USER_ID),
            ).fetchone()
        return row[0]

    def test_the_sync_carries_the_flag_to_the_product_database(self):
        # The precondition: the refresh does put the flag on the re-listed posting.
        flags = _flags(self._explanation(self.relisted_id))
        self.assertEqual(len(flags), 1, self._explanation(self.relisted_id))
        self.assertEqual(_flags(self._explanation(self.retired_id)), [])

    def test_saving_the_profile_keeps_the_flag(self):
        before = _flags(self._explanation(self.relisted_id))
        saved = self._client().put(
            "/api/v1/profile",
            json={"updates": {"skills": ["SolidWorks"]}, "confirmed_fields": ["skills"]},
            headers=OWNER,
        )
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(_flags(self._explanation(self.relisted_id)), before)
        # Only the re-listed posting is flagged; the retired one never is.
        self.assertEqual(_flags(self._explanation(self.retired_id)), [])

    def test_rescoring_the_profile_keeps_the_flag_and_adds_it_once(self):
        before = _flags(self._explanation(self.relisted_id))
        profile = json.loads(self.profile_path.read_text(encoding="utf-8"))
        with closing(sqlite3.connect(self.platform_path)) as conn:
            profile_module.rescore_profile(conn, profile, user_id=LOCAL_USER_ID)
            profile_module.rescore_profile(conn, profile, user_id=LOCAL_USER_ID)
        self.assertEqual(_flags(self._explanation(self.relisted_id)), before)

    def test_a_save_and_a_refresh_write_the_same_explanation(self):
        """Which write ran last must not change what the explanation says."""

        self._client().put(
            "/api/v1/profile",
            json={"updates": {"skills": ["SolidWorks"]}, "confirmed_fields": ["skills"]},
            headers=OWNER,
        )
        after_save = json.loads(self._explanation(self.relisted_id))
        with mock.patch.object(paths, "DB_PATH", self.legacy_path):
            conn = store.connect()
            try:
                scoring.score_all(conn, json.loads(self.profile_path.read_text(encoding="utf-8")))
                legacy = json.loads(
                    conn.execute("SELECT score_explanation FROM jobs WHERE id=?", (self.relisted_id,)).fetchone()[0]
                )
            finally:
                conn.close()
        self.assertEqual([r for r in after_save if r.startswith("FLAG")], [r for r in legacy if r.startswith("FLAG")])


class ProfileSaveRepostSharedDefinitionTests(unittest.TestCase):
    """The product database's postings go through the same repost rule as the legacy ones."""

    def test_a_save_flags_a_relisted_posting_that_has_no_flag_yet(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, platform_path = build_and_migrate(root)
            old = (datetime.now(timezone.utc) - timedelta(days=20)).isoformat()
            new = datetime.now(timezone.utc).isoformat()
            with closing(sqlite3.connect(platform_path)) as conn, conn:
                for opp_id, title, url, seen, active in (
                    ("rp-old", "Controls Intern (Summer 2026)", "https://rp.example.test/1", old, 0),
                    ("rp-new", "Controls Intern (Summer 2027)", "https://rp.example.test/2", new, 1),
                ):
                    conn.execute(
                        "INSERT INTO opportunities(id, company, title, url, first_seen_at, last_seen_at, active, "
                        "created_at, updated_at) VALUES(?, 'Repost Co', ?, ?, ?, ?, ?, ?, ?)",
                        (opp_id, title, url, seen, seen, active, seen, seen),
                    )
                scores = profile_module._compute_scores(conn, {})
            by_id = {opp_id: reasons for opp_id, _score, reasons in scores}
            self.assertEqual(len([r for r in by_id["rp-new"] if r.startswith(FLAG_PREFIX)]), 1, by_id["rp-new"])
            self.assertEqual([r for r in by_id["rp-old"] if r.startswith(FLAG_PREFIX)], [])
            self.assertTrue(
                any("2 different URLs" in r and old[:10] in r for r in by_id["rp-new"]), by_id["rp-new"]
            )


if __name__ == "__main__":
    unittest.main()
