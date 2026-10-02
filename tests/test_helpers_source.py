"""The source-scanning helpers must find code wherever it moves to, or the guards built on them go quiet."""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers_source import apply_modules, is_apply_module

try:
    import realdata_guard
except ImportError:  # imported as tests.test_helpers_source, with tests/ not on sys.path
    from tests import realdata_guard
realdata_guard.install()


class ApplyModuleSelectionTests(unittest.TestCase):
    def test_apply_modules_are_found_at_any_depth(self):
        for relative in (
            "apply_policy.py", "apply/policy.py", "apply/deep/policy.py", "routers/apply_policy.py", "routers/apply/policy.py",
            "routers/apply/deep/x.py", "apply_sensitive.py", "apply/sensitive.py", "routers/apply_runs/x.py",
        ):
            with self.subTest(relative=relative):
                self.assertTrue(is_apply_module(relative))

    def test_other_modules_are_not(self):
        for relative in ("applications/extension.py", "api.py", "routers/outreach.py", "schema.py", "preparation/answers.py"):
            with self.subTest(relative=relative):
                self.assertFalse(is_apply_module(relative))

    def test_the_store_is_excluded_wherever_it_lives_when_asked(self):
        for relative in ("apply_sensitive.py", "apply_sensitive/store.py", "apply/sensitive.py", "routers/apply_sensitive.py",
                         "routers/apply/sensitive.py", "apply/sensitive/store.py"):
            with self.subTest(relative=relative):
                self.assertTrue(is_apply_module(relative))
                self.assertFalse(is_apply_module(relative, exclude_store=True))
        for relative in ("apply_policy.py", "routers/apply/policy.py", "apply/sensitivity.py"):
            with self.subTest(relative=relative):
                self.assertTrue(is_apply_module(relative, exclude_store=True))

    def test_the_real_scan_finds_the_modules_it_should(self):
        found = apply_modules()
        self.assertIn("apply/policy.py", found)
        self.assertIn("apply/sensitive.py", found)
        self.assertNotIn("apply/sensitive.py", apply_modules(exclude_store=True))
        self.assertNotIn("applications/extension.py", found)
        # The exclusion above would hold trivially if the file were not there, so check that the file exists.
        self.assertTrue((ROOT / "opportunity_app" / "applications" / "extension.py").is_file())


if __name__ == "__main__":
    unittest.main()
