"""Files the shipped code finds relative to itself must keep resolving when a module moves to another package.

Phase 6 moved modules into subpackages. A module that builds a path from its own file (`Path(__file__).parents[1]`) silently
points somewhere else after a move: ingestion would look for pipeline.py inside opportunity_app/, and the mock-interview audio
folder would land inside opportunity_app/data/private, which .gitignore does not cover, so private audio could be committed.
The rule is: build paths from the package constants (opportunity_app.ROOT, STATIC_DIR), which come from the one __init__.py
that never moves. This file pins the values that matter and fails when any other module reaches for __file__ again.
"""

import ast
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    import realdata_guard
except ImportError:  # imported as tests.<module>, with tests/ not on sys.path
    from tests import realdata_guard
# Guards this module's own run against opening data/*.db (AGENTS.md hard rule 1); see tests/realdata_guard.py.
realdata_guard.install()

# The only modules that may locate files through their own __file__: the package root (its constants are the anchor) and the
# sender allowlist, which ships a data file beside itself. Paths from the repo root.
FILE_RELATIVE_ALLOWED = ("opportunity_app/__init__.py", "opportunity_app/mail_trust.py")


class ResolvedPathTests(unittest.TestCase):
    def test_ingestion_paths_point_at_the_repository(self):
        from opportunity_app import ingestion

        self.assertEqual(ingestion.PIPELINE_CLI, ROOT / "pipeline.py")
        self.assertTrue(ingestion.PIPELINE_CLI.is_file(), "ingestion runs this file as a subprocess")
        self.assertEqual(ingestion.DEFAULT_DISCOVERED_PATH, ROOT / "data" / "discovered_jobs.json")
        self.assertEqual(ingestion.SOURCES_CONFIG, ROOT / "config" / "sources.json")

    def test_mock_interview_audio_stays_under_the_ignored_data_folder(self):
        from opportunity_app import preparation

        self.assertEqual(preparation.DEFAULT_MOCK_AUDIO_STORAGE, ROOT / "data" / "private" / "mock-interviews")

    def test_the_sender_allowlist_ships_beside_its_module_and_is_readable(self):
        from opportunity_app import mail_trust

        self.assertTrue(mail_trust.SENDERS_PATH.is_file(), f"{mail_trust.SENDERS_PATH} is missing: a moved module lost its data file")
        self.assertEqual(mail_trust.SENDERS_PATH.parent, Path(mail_trust.__file__).resolve().parent / "data")
        self.assertTrue(mail_trust.sender_lists(), "the allowlist file parses and lists senders")

    def test_the_package_anchors_resolve(self):
        import opportunity_app
        from opportunity_app import schema

        self.assertEqual(opportunity_app.ROOT, ROOT)
        self.assertTrue((opportunity_app.STATIC_DIR / "index.html").is_file())
        self.assertTrue(schema.MIGRATIONS_DIR.is_dir() and any(schema.MIGRATIONS_DIR.glob("0001_*.sql")))


class FileRelativeLookupTests(unittest.TestCase):
    def test_only_the_package_root_and_the_sender_allowlist_use___file__(self):
        offenders = []
        for path in sorted((ROOT / "opportunity_app").rglob("*.py")):
            relative = path.relative_to(ROOT).as_posix()
            if relative in FILE_RELATIVE_ALLOWED:
                continue
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8-sig"), filename=relative)):
                if isinstance(node, ast.Name) and node.id == "__file__":
                    offenders.append(f"{relative}:{node.lineno}")
        self.assertEqual(offenders, [], "build paths from opportunity_app.ROOT or STATIC_DIR, not from this module's own location")

    def test_the_allowed_modules_exist(self):
        for relative in FILE_RELATIVE_ALLOWED:
            self.assertTrue((ROOT / relative).is_file(), f"{relative} is allowed to use __file__ but does not exist: re-key the list")


if __name__ == "__main__":
    unittest.main()
