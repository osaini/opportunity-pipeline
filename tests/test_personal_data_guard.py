"""The hooks that refuse to commit or push personal data (scripts/check_personal_data.py).

Every value here belongs to an invented student. The check reads the gitignored
personal files of whichever copy it runs in, so these tests build their own.
"""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from opportunity_app import setup

REPO = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("check_personal_data", REPO / "scripts" / "check_personal_data.py")
guard = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = guard  # dataclasses look the module up while it loads
SPEC.loader.exec_module(guard)

RESUME = {
    "name": "Robin Quill",
    "contact": {
        "email": "robin.quill@student.example.edu",
        "phone": "(512) 555-0199",
        "location": "Austin, TX",
        "linkedin": "https://www.linkedin.com/in/robin-quill-example/",
        "github": "https://github.com/robinquill-example",
        "portfolio": "",
    },
}
ENV = "\n".join([
    "# comment",
    "PIPELINE_WEB_TOKEN=tok-1234567890abcdef",
    "USAJOBS_CONTACT_EMAIL=robin.personal@mail.example",
    "PIPELINE_SEC_USER_AGENT=Robin Quill robin.agent@mail.example",
    "PIPELINE_OUTREACH_COMPOSE=true",
    "TYPESAFE_MODEL=jev-small-model",
])


def write_personal_files(root: Path) -> None:
    (root / "config").mkdir(exist_ok=True)
    (root / "private").mkdir(exist_ok=True)
    (root / "config" / "resume.json").write_text(json.dumps(RESUME), encoding="utf-8")
    (root / "config" / "profile.json").write_text(json.dumps({"name": "Robin Quill"}), encoding="utf-8")
    (root / ".env").write_text(ENV, encoding="utf-8")
    (root / "private" / "blocked-terms.txt").write_text("# employers\nQuillworks Labs\n\nab\n", encoding="utf-8")


class NeedleTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        write_personal_files(self.root)
        self.needles = guard.build_needles(self.root, home=Path("C:/Users/rquill"))

    def tearDown(self):
        self.tempdir.cleanup()

    def kinds(self, text):
        return {hit.kind for hit in guard.scan_text(text, "x", self.needles)}

    def test_every_personal_value_is_caught(self):
        cases = {
            "by Robin  Quill": "resume name",
            "mail ROBIN.QUILL@student.example.edu": "resume email",
            "call 512.555.0199": "resume phone",
            "call +1 512 555 0199": "resume phone",
            "see linkedin.com/in/robin-quill-example": "resume linkedin",
            "http://github.com/robinquill-example/repo": "resume github",
            "token = tok-1234567890abcdef": ".env value (PIPELINE_WEB_TOKEN)",
            "robin.agent@mail.example": ".env email (PIPELINE_SEC_USER_AGENT)",
            "worked at quillworks labs": "blocked term",
            'path "C:\\\\Users\\\\rquill\\\\Desktop"': "home directory",
            "/Users/rquill/Downloads": "home directory",
        }
        for text, kind in cases.items():
            with self.subTest(text=text):
                self.assertIn(kind, self.kinds(text))

    def test_ordinary_text_is_not_flagged(self):
        for text in (
            "Austin, TX",  # a city is shared by a million people
            "compose = true",
            "model jev-small-model",  # a setting, not an identity
            "Robin Quillson",  # names match whole words only
            "github.com/robinquill-example-other",
            "call 512-555-01999",
            "tab and ab",  # blocked terms shorter than three characters are ignored
            "C:/Users/rquillx",
        ):
            with self.subTest(text=text):
                self.assertEqual(self.kinds(text), set())

    def test_reports_mask_the_value(self):
        hit = guard.scan_text("robin.quill@student.example.edu", "f:1", self.needles)[0]
        self.assertNotIn("robin.quill", str(hit))
        self.assertIn("f:1", str(hit))

    def test_a_fresh_copy_with_no_personal_files_still_runs(self):
        with tempfile.TemporaryDirectory() as empty:
            needles = guard.build_needles(Path(empty), home=Path("/"))
        self.assertEqual(needles, [])

    def test_personal_files_are_refused_by_path(self):
        for path in (".env", ".env.local", "config/resume.json", "config/profile.json", "config/sources.local.json",
                     "private/notes.md", "output/shortlist.md", "data/platform.db", "data/outreach-seed.json",
                     "docs/my-resume.pdf", "scripts\\resume.docx"):
            with self.subTest(path=path):
                self.assertTrue(guard.check_path(path))
        for path in (".env.example", "data/manual_jobs.csv", "config/sources.local.example.json",
                     "config/profile.example.json", "tests/fixtures/sample.pdf", "README.md"):
            with self.subTest(path=path):
                self.assertEqual(guard.check_path(path), [])

    def test_only_added_lines_count_with_their_new_line_numbers(self):
        diff = "\n".join([
            "diff --git a/notes.md b/notes.md",
            "--- a/notes.md",
            "+++ b/notes.md",
            "@@ -3,2 +10,3 @@",
            "-removed Robin Quill",
            " unchanged",
            "+added line",
            "+contact robin.quill@student.example.edu",
        ])
        hits = guard.scan_diff(diff, self.needles)
        self.assertEqual([(hit.where, hit.kind) for hit in hits], [("notes.md:12", "resume email")])


@unittest.skipUnless(shutil.which("git"), "git is not installed")
class HookTests(unittest.TestCase):
    """The hooks as git runs them, in a throwaway repository."""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        shutil.copytree(REPO / ".githooks", self.root / ".githooks")
        (self.root / "scripts").mkdir()
        shutil.copyfile(REPO / "scripts" / "check_personal_data.py", self.root / "scripts" / "check_personal_data.py")
        (self.root / ".gitignore").write_text((REPO / ".gitignore").read_text(encoding="utf-8"), encoding="utf-8")
        write_personal_files(self.root)
        self.git("init", "-q", "-b", "main")
        self.assertEqual(setup.enable_personal_data_hooks(self.root), "enabled")
        self.assertEqual(setup.enable_personal_data_hooks(self.root), "enabled", "idempotent")
        self.git("add", ".")
        self.git("commit", "-q", "-m", "tooling")

    def tearDown(self):
        self.tempdir.cleanup()

    def git(self, *args, check=True):
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
        result = subprocess.run(["git", "-C", str(self.root), *args], capture_output=True, text=True, env=env)
        if check:
            self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def test_the_personal_files_themselves_stay_untracked(self):
        tracked = self.git("ls-files").stdout.split()
        self.assertNotIn(".env", tracked)
        self.assertFalse([name for name in tracked if name.startswith(("config/", "private/"))], tracked)

    def test_commit_with_personal_data_is_refused(self):
        (self.root / "README.md").write_text("Contact robin.quill@student.example.edu\n", encoding="utf-8")
        self.git("add", "README.md")
        result = self.git("commit", "-m", "readme", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("README.md:1: resume email", result.stderr)
        self.assertNotIn("robin.quill@", result.stderr, "the refusal does not repeat the value")

    def test_forced_personal_file_is_refused(self):
        self.git("add", "-f", "config/resume.json")
        result = self.git("commit", "-m", "resume", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("config/resume.json: personal file", result.stderr)

    def test_clean_commit_passes(self):
        (self.root / "README.md").write_text("A pipeline for students.\n", encoding="utf-8")
        self.git("add", "README.md")
        self.git("commit", "-q", "-m", "readme")

    def test_push_catches_a_commit_that_skipped_the_commit_hook(self):
        remote = self.root / "remote.git"
        subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
        self.git("remote", "add", "origin", str(remote))
        self.git("push", "-q", "origin", "main")
        (self.root / "README.md").write_text("fine\n", encoding="utf-8")
        self.git("add", "README.md")
        self.git("commit", "-q", "-m", "Thanks, Robin Quill", "--no-verify")
        result = self.git("push", "origin", "main", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("commit message: resume name", result.stderr)


if __name__ == "__main__":
    unittest.main()
