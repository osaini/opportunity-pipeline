"""First-run setup (opportunity_app.setup) against a throwaway project root."""

import io
import json
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from opportunity_app import setup

REPO = Path(__file__).resolve().parents[1]


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        (root / "config").mkdir()
        for name in (".env.example", "config/profile.example.json", "config/sources.json"):
            shutil.copyfile(REPO / name, root / name)
        self.paths = setup.Paths(root)

    def tearDown(self):
        self.tempdir.cleanup()

    def test_init_creates_a_working_copy_with_generated_secrets(self):
        with mock.patch.object(setup, "detect_agent_cli", return_value="codex-cli"):
            report = setup.init(self.paths)
        self.assertEqual(
            sorted(report["created"]),
            [".env", "config/profile.json", "config/sources.local.json", "data/platform.db"],
        )
        env = setup.read_env(self.paths.env)
        for name in setup.GENERATED_SECRETS:
            self.assertGreaterEqual(len(env[name]), 32, name)
        self.assertNotEqual(env["PIPELINE_WEB_TOKEN"], env["PIPELINE_ADMIN_TOKEN"])
        self.assertEqual(env["PIPELINE_OUTREACH_DISCOVERY_PROVIDER"], "codex-cli")
        # Secrets are reported by name only.
        self.assertNotIn(env["PIPELINE_WEB_TOKEN"], json.dumps(report))
        # The comments in the template survive.
        self.assertIn("# --- Optional: more job sources", self.paths.env.read_text(encoding="utf-8"))
        with closing(sqlite3.connect(self.paths.platform_db)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM users WHERE id='local-user'").fetchone()[0], 1)

    def test_init_is_idempotent_and_never_overwrites_what_the_student_set(self):
        setup.init(self.paths)
        first = setup.read_env(self.paths.env)
        setup.set_env_values(self.paths.env, {"ADZUNA_APP_ID": "mine"}, overwrite=True)
        profile = json.loads(self.paths.profile.read_text(encoding="utf-8"))
        self.paths.profile.write_text(json.dumps({**profile, "name": "Sam"}), encoding="utf-8")

        report = setup.init(self.paths)
        second = setup.read_env(self.paths.env)
        self.assertEqual(report["created"], [])
        self.assertEqual(report["generated_secrets"], [])
        self.assertEqual(second["PIPELINE_WEB_TOKEN"], first["PIPELINE_WEB_TOKEN"])
        self.assertEqual(second["ADZUNA_APP_ID"], "mine")
        self.assertEqual(json.loads(self.paths.profile.read_text(encoding="utf-8"))["name"], "Sam")

    def test_init_fills_only_the_blank_secrets_in_an_existing_env(self):
        self.paths.env.write_text("PIPELINE_WEB_TOKEN=keep-me\nPIPELINE_ADMIN_TOKEN=\n", encoding="utf-8")
        report = setup.init(self.paths, migrate=False)
        env = setup.read_env(self.paths.env)
        self.assertEqual(env["PIPELINE_WEB_TOKEN"], "keep-me")
        self.assertTrue(env["PIPELINE_ADMIN_TOKEN"])
        self.assertNotIn("PIPELINE_WEB_TOKEN", report["generated_secrets"])

    def test_set_key_reads_the_value_without_echoing_it(self):
        setup.init(self.paths, migrate=False)
        with mock.patch("sys.stdin", io.StringIO("adzuna-secret\n")):
            message = setup.set_key(self.paths, "ADZUNA_APP_KEY")
        self.assertNotIn("adzuna-secret", message)
        self.assertEqual(setup.read_env(self.paths.env)["ADZUNA_APP_KEY"], "adzuna-secret")
        for bad in ("lower", "A B", "1ABC"):
            with self.subTest(name=bad), self.assertRaises(SystemExit):
                setup.set_key(self.paths, bad, "x")
        with self.assertRaises(SystemExit):
            setup.set_key(self.paths, "ADZUNA_APP_KEY", "   ")

    def test_status_reports_integrations_without_values(self):
        setup.init(self.paths, migrate=False)
        setup.set_env_values(self.paths.env, {"TYPESAFE_API_KEY": "jev-secret"}, overwrite=True)
        with mock.patch.dict("os.environ", {}, clear=False), mock.patch.object(setup.shutil, "which", return_value=None):
            report = setup.status(self.paths)
        by_id = {item["id"]: item for item in report["integrations"]}
        self.assertTrue(by_id["jev"]["configured"])
        self.assertFalse(by_id["claude-code"]["configured"])
        self.assertNotIn("jev-secret", json.dumps(report))
        self.assertTrue(report["sign_in_token"])
        self.assertIn("name", report["profile"]["missing"])
        self.assertGreater(report["sources"]["enabled"], 0)
        self.assertEqual(report["sources"]["needs_keys"], [], "key-gated sources ship disabled")
        self.assertTrue(any("Interview the student" in step for step in report["next"]))


class ProfileValidationTests(unittest.TestCase):
    def test_the_template_is_valid_but_incomplete(self):
        profile = json.loads((REPO / "config" / "profile.example.json").read_text(encoding="utf-8"))
        report = setup.validate_profile(profile)
        self.assertTrue(report["ok"], report)
        self.assertIn("school", report["missing"])

    def test_the_test_fixture_profile_is_complete_enough_to_score(self):
        profile = json.loads((REPO / "tests" / "fixtures" / "profile_student.json").read_text(encoding="utf-8"))
        self.assertTrue(setup.validate_profile(profile)["ok"])

    def test_mistakes_an_agent_might_make_are_caught(self):
        report = setup.validate_profile({
            "graduation_year": "2029",
            "hours_per_week": True,
            "regions": [{"name": "Atlanta", "places": ["atlanta"]}],
            "available_terms": ["Summer 27"],
            "preferred_role_types": ["internship", "gig"],
        })
        self.assertFalse(report["ok"])
        joined = " ".join(report["errors"])
        self.assertIn("graduation_year", joined)
        self.assertIn("hours_per_week", joined)
        self.assertIn("regions[0].state_markers", joined)
        self.assertIn("Summer 27", joined)
        self.assertTrue(any("gig" in warning for warning in report["warnings"]))


if __name__ == "__main__":
    unittest.main()
