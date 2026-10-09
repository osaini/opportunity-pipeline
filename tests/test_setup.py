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

try:
    import realdata_guard
except ImportError:  # imported as tests.<module>, with tests/ not on sys.path
    from tests import realdata_guard
# Guards this module's own run against opening data/*.db (AGENTS.md hard rule 1); see tests/realdata_guard.py.
realdata_guard.install()


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

    def test_init_warns_when_only_codex_is_installed_and_the_web_opt_in_is_not_set(self):
        with mock.patch.object(setup, "detect_agent_cli", return_value="codex-cli"):
            report = setup.init(self.paths)
        self.assertTrue(any("PIPELINE_OUTREACH_RESEARCH_ALLOW_CODEX" in warning for warning in report["warnings"]), report["warnings"])
        setup.set_env_values(self.paths.env, {"PIPELINE_OUTREACH_RESEARCH_ALLOW_CODEX": "1"}, overwrite=True)
        with mock.patch.object(setup, "detect_agent_cli", return_value="codex-cli"):
            self.assertFalse(any("ALLOW_CODEX" in warning for warning in setup.init(self.paths)["warnings"]))
        with mock.patch.object(setup, "detect_agent_cli", return_value="claude-code"):
            setup.set_env_values(self.paths.env, {"PIPELINE_OUTREACH_RESEARCH_ALLOW_CODEX": ""}, overwrite=True)
            self.assertFalse(any("ALLOW_CODEX" in warning for warning in setup.init(self.paths)["warnings"]))

    def test_init_warns_for_a_value_that_is_not_an_opt_in_the_way_the_app_reads_it(self):
        """Any non-empty value used to silence the warning, yet the app opts in only for 1, true, yes or on: '0' is not one."""
        for value, warns in (("0", True), ("no", True), ("off", True), ("false", True), ("1", False), ("true", False), ("YES", False), ("on", False)):
            with self.subTest(value=value):
                setup.set_env_values(self.paths.env, {"PIPELINE_OUTREACH_RESEARCH_ALLOW_CODEX": value}, overwrite=True)
                with mock.patch.object(setup, "detect_agent_cli", return_value="codex-cli"):
                    report = setup.init(self.paths)
                self.assertEqual(any("ALLOW_CODEX" in warning for warning in report["warnings"]), warns, report["warnings"])

    def test_init_records_claude_code_for_research_when_both_agents_are_installed(self):
        """Claude Code can be limited to web search and Codex cannot, so Codex is only recorded when it is the only one."""
        both = {"claude": True, "codex": True}
        for installed, recorded, warns in (
            (both, "claude-code", False), ({"claude": False, "codex": True}, "codex-cli", True), ({"claude": False, "codex": False}, None, False),
        ):
            with self.subTest(installed=installed):
                tidy = Path(tempfile.mkdtemp())
                self.addCleanup(shutil.rmtree, tidy, True)
                (tidy / "config").mkdir()
                for name in (".env.example", "config/profile.example.json", "config/sources.json"):
                    shutil.copyfile(REPO / name, tidy / name)
                paths = setup.Paths(tidy)
                with mock.patch.object(setup, "cli_available", lambda binary: installed["claude" if "claude" in binary.lower() else "codex"]),                         mock.patch.dict("os.environ", {"PIPELINE_CLAUDE_BIN": "", "PIPELINE_CODEX_BIN": ""}):
                    report = setup.init(paths)
                self.assertEqual(report["agent_cli"], recorded)
                self.assertEqual(setup.read_env(paths.env).get("PIPELINE_OUTREACH_DISCOVERY_PROVIDER", "") or None, recorded)
                self.assertEqual(any("PIPELINE_OUTREACH_RESEARCH_ALLOW_CODEX" in warning for warning in report["warnings"]), warns)

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

    def test_set_key_drops_the_byte_order_mark_windows_powershell_pipes_in(self):
        # `echo 1 | python -m opportunity_app.setup set-key NAME` (SETUP.md) in Windows PowerShell 5.1 sends
        # the bytes EF BB BF 31 0D 0A. A default Windows Python decodes a pipe as cp1252, so the mark arrives
        # as "\u00ef\u00bb\u00bf"; with PYTHONIOENCODING=utf-8 it arrives as U+FEFF. Both must store "1".
        setup.init(self.paths, migrate=False)
        for encoding in ("cp1252", "utf-8"):
            for piped, stored in ((b"\xef\xbb\xbf1\r\n", "1"), (b"\xef\xbb\xbfcaf\xc3\xa9\r\n", "caf\u00e9")):
                stdin = io.TextIOWrapper(io.BytesIO(piped), encoding=encoding)
                with self.subTest(encoding=encoding, piped=piped), mock.patch("sys.stdin", stdin):
                    setup.set_key(self.paths, "PIPELINE_SKIP_SIGN_IN")
                    self.assertEqual(setup.read_env(self.paths.env)["PIPELINE_SKIP_SIGN_IN"], stored)
        # A pipe with no mark (PowerShell 7, cmd, a POSIX shell) and a stream with no byte layer still work.
        with mock.patch("sys.stdin", io.TextIOWrapper(io.BytesIO(b"plain\n"), encoding="cp1252")):
            setup.set_key(self.paths, "PIPELINE_SKIP_SIGN_IN")
        self.assertEqual(setup.read_env(self.paths.env)["PIPELINE_SKIP_SIGN_IN"], "plain")
        with mock.patch("sys.stdin", io.StringIO("\ufeff1\r\n")):
            setup.set_key(self.paths, "PIPELINE_SKIP_SIGN_IN")
        self.assertEqual(setup.read_env(self.paths.env)["PIPELINE_SKIP_SIGN_IN"], "1")
        with self.assertRaises(SystemExit):
            setup.set_key(self.paths, "PIPELINE_SKIP_SIGN_IN", "\ufeff ")

    def test_a_dot_env_saved_with_a_byte_order_mark_keeps_its_first_setting(self):
        # Windows PowerShell 5.1's `Set-Content -Encoding utf8` and Out-File write one; it must not hide the first key.
        self.paths.env.write_bytes(b"\xef\xbb\xbfPIPELINE_SKIP_SIGN_IN=1\nADZUNA_APP_KEY=adzuna\n")
        self.assertEqual(setup.read_env(self.paths.env), {"PIPELINE_SKIP_SIGN_IN": "1", "ADZUNA_APP_KEY": "adzuna"})
        setup.set_env_values(self.paths.env, {"PIPELINE_SKIP_SIGN_IN": "0"}, overwrite=True)
        self.assertEqual(self.paths.env.read_text(encoding="utf-8"), "PIPELINE_SKIP_SIGN_IN=0\nADZUNA_APP_KEY=adzuna\n")

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

    def test_a_home_outreach_cannot_place_is_flagged(self):
        atlanta = [{"name": "Atlanta", "state_markers": ["ga"], "places": ["atlanta", "marietta"]}]
        for home, flagged in (("Portland", True), ("Seattle, WA", False), ("Marietta, GA", False), ("Atlanta", False)):
            warnings = setup.validate_profile({"break_location": home, "regions": atlanta})["warnings"]
            self.assertEqual(any("break_location" in warning for warning in warnings), flagged, home)

    def test_a_line_break_in_a_setting_can_never_write_another_key(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        env = Path(folder.name) / ".env"
        env.write_text("# mine\nPIPELINE_WEB_TOKEN=keep\n", encoding="utf-8")
        before = env.read_text(encoding="utf-8")
        for updates in (
            {"PIPELINE_LINKEDIN_ACCOUNT": "jane\nPIPELINE_WEB_TOKEN=stolen"},
            {"PIPELINE_LINKEDIN_ACCOUNT": "jane\rPIPELINE_WEB_TOKEN=stolen"},
            {"PIPELINE_LINKEDIN_ACCOUNT\nEVIL": "x"},
        ):
            with self.subTest(updates), self.assertRaisesRegex(ValueError, "line break"):
                setup.set_env_values(env, updates, overwrite=True)
        self.assertEqual(env.read_text(encoding="utf-8"), before, "nothing was written")
        setup.set_env_values(env, {"PIPELINE_LINKEDIN_ACCOUNT": "jane-doe"}, overwrite=True)
        self.assertEqual(setup.read_env(env)["PIPELINE_LINKEDIN_ACCOUNT"], "jane-doe")


if __name__ == "__main__":
    unittest.main()
