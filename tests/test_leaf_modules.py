"""The shared leaf modules stay leaves, and the helpers that were collapsed into them keep their behavior.

A leaf is a small module many others import, so it may import only what its entry in the table below allows
(standard library, plus other leaves where stated). If it grew a dependency on schema, automation, outreach or the
API, every importer would drag that in and the import cycles the leaves exist to avoid would come back.

Each workstream of the Phase 3 refactor keeps its own section (a class named after it) so the sections merge cleanly.
"""

from __future__ import annotations

import ast
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from typing import get_args
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    import realdata_guard
except ImportError:  # imported as tests.<module>, with tests/ not on sys.path
    from tests import realdata_guard
# Guards this module's own run against opening data/*.db (AGENTS.md hard rule 1); see tests/realdata_guard.py.
realdata_guard.install()

ROOT = Path(__file__).resolve().parents[1]

# Leaf module path -> first-party modules it may import (everything else must be standard library).
# Dotted names are absolute; "." names are relative imports within the leaf's own package.
LEAVES: dict[str, set[str]] = {
    # Workstream I: identity and the legacy boundary
    "pipeline_core/identity.py": set(),
    "pipeline_core/regions.py": {".identity"},
    "pipeline_core/env.py": set(),
    "opportunity_app/storage_paths.py": set(),
}


def first_party_imports(path: Path) -> tuple[set[str], set[str]]:
    """(standard-library-or-other top-level modules imported, relative modules imported) for every import in a file."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    absolute: set[str] = set()
    relative: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            absolute.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                relative.add("." * node.level + (node.module or ""))
            elif node.module:
                absolute.add(node.module)
    return absolute, relative


def imports_outside_the_stdlib(path: Path) -> set[str]:
    absolute, relative = first_party_imports(path)
    return {name for name in absolute if name.split(".")[0] not in sys.stdlib_module_names and name != "__future__"} | relative


class LeafModulesStayLeavesTests(unittest.TestCase):
    def test_each_leaf_imports_only_the_standard_library_and_the_leaves_it_names(self):
        for relative_path, allowed in LEAVES.items():
            with self.subTest(leaf=relative_path):
                path = ROOT / relative_path
                self.assertTrue(path.is_file(), f"{relative_path} is listed as a leaf but does not exist")
                self.assertEqual(imports_outside_the_stdlib(path) - allowed, set(), f"{relative_path} imports outside its allowlist")

    def test_every_leaf_import_is_at_module_level_so_nothing_loads_late(self):
        for relative_path in LEAVES:
            with self.subTest(leaf=relative_path):
                tree = ast.parse((ROOT / relative_path).read_text(encoding="utf-8"))
                nested = [
                    node for node in ast.walk(tree)
                    if isinstance(node, (ast.Import, ast.ImportFrom)) and node not in tree.body
                ]
                self.assertEqual(nested, [], f"{relative_path} has a function-level import")


class IdentityAndLegacyWorkstreamTests(unittest.TestCase):
    """Workstream I: pipeline_core/identity.py, opportunity_app/legacy.py and the constants collapsed into one place."""

    def test_employer_key_output_is_pinned_because_apply_stores_persist_it(self):
        from pipeline_core.identity import employer_key

        # A change to the identity rule orphans rows in apply_runs, apply_sensitive_answers and employer_domains.
        for name, expected in (
            ("Acme Robotics Inc.", "acme robotics"),
            ("ACME robotics", "acme robotics"),
            ("The Acme Group, LLC", "acme"),
            ("Robotics Acme", "acme robotics"),
            ("Bluefin Labs", "bluefin labs"),
            ("", ""),
        ):
            with self.subTest(name=name):
                self.assertEqual(employer_key(name), expected)

    def test_employer_key_still_refuses_none_because_an_empty_key_means_any_company(self):
        from pipeline_core.identity import employer_key

        with self.assertRaises(AttributeError):
            employer_key(None)  # type: ignore[arg-type]

    def test_mail_trust_company_key_still_reads_none_as_empty(self):
        from opportunity_app import mail_trust
        from pipeline_core.identity import employer_key

        self.assertEqual(mail_trust.company_key(None), "")  # type: ignore[arg-type]
        self.assertEqual(mail_trust.company_key("Acme Robotics Inc."), employer_key("Acme Robotics Inc."))

    def test_the_apply_stores_and_policy_use_the_one_employer_key(self):
        from opportunity_app import apply_policy, apply_runs, apply_sensitive

        for module in (apply_runs, apply_sensitive, apply_policy):
            self.assertFalse(hasattr(module, "company_key"), f"{module.__name__} defines its own company_key again")
        self.assertNotIn("company_key", apply_sensitive.__all__)

    def test_sort_key_is_the_casefold_the_stored_columns_use(self):
        from pipeline_core.identity import sort_key

        self.assertEqual(sort_key("Straße GmbH"), "strasse gmbh")
        self.assertEqual(sort_key(None), "")
        self.assertEqual(sort_key(""), "")
        self.assertEqual(sort_key("ACME"), "acme")

    def test_schema_read_model_and_tags_share_that_one_sort_key(self):
        from opportunity_app import company_tags, schema
        from pipeline_core import identity, read_model

        self.assertIs(schema.sort_key, identity.sort_key)
        self.assertIs(company_tags.sort_key, identity.sort_key)
        self.assertIs(read_model.sort_key, identity.sort_key)

    def test_outreach_company_key_is_a_different_rule_and_stays_separate(self):
        from opportunity_app import outreach
        from pipeline_core.identity import employer_key, sort_key

        # NFKC, "&" becomes "and", a leading "The" and trailing legal words dropped, word order kept.
        self.assertEqual(outreach.company_key("The Smith & Sons Holdings Group"), "smith and sons holdings group")
        self.assertNotEqual(outreach.company_key("Robotics Acme"), employer_key("Robotics Acme"))
        self.assertNotEqual(outreach.company_key("Acme Robotics Inc"), sort_key("Acme Robotics Inc"))

    def test_normalized_text_reads_none_as_empty_but_not_zero_or_false(self):
        from pipeline_core.identity import normalized, normalized_text

        self.assertEqual(normalized_text(None), "")
        self.assertEqual(normalized_text(0), "0")
        self.assertEqual(normalized_text(False), "false")
        self.assertEqual(normalized_text("Are you 18+?"), normalized("Are you 18+?"))

    def test_schema_no_longer_loads_the_legacy_pipeline_for_a_region_label(self):
        # A fresh interpreter, since every other test in the run has imported pipeline already.
        import subprocess

        code = (
            "import sys; sys.path.insert(0, %r); import opportunity_app.schema; "
            "print('pipeline' in sys.modules)" % str(ROOT)
        )
        done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=ROOT, check=True)
        self.assertEqual(done.stdout.strip(), "False")

    def test_only_the_legacy_adapter_imports_pipeline(self):
        offenders = []
        for path in sorted((ROOT / "opportunity_app").rglob("*.py")):
            if path.name == "legacy.py":
                continue
            absolute, _ = first_party_imports(path)
            if "pipeline" in absolute:
                offenders.append(path.name)
        self.assertEqual(offenders, [], "web modules must import pipeline names through opportunity_app.legacy")

    def test_the_regions_moved_to_pipeline_core_still_bucket_locations(self):
        from pipeline_core.regions import match_region, region_label

        profile = {"regions": [{"name": "Austin", "state_markers": ["TX"], "places": ["Austin"], "aliases": []}]}
        self.assertEqual(region_label("Austin, TX", profile), "Austin")
        self.assertEqual(region_label("3 Locations", profile), "Unknown")
        self.assertEqual(region_label("Remote - US", profile), "Remote")
        self.assertEqual(region_label("Seattle, WA", profile), "Other")
        self.assertIsNone(match_region("Austin, MN", profile["regions"]))

    def test_env_lines_share_one_rule_and_differ_only_in_how_the_caller_treats_repeats(self):
        import os

        import pipeline
        from opportunity_app import setup
        from pipeline_core.env import iter_env_pairs

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text(
                "# comment\n\nA=one\nB = \"two\" \nnot a pair\nA=again\n=orphan\nC='mixed\"\nD=\n", encoding="utf-8",
            )
            self.assertEqual(
                iter_env_pairs(path),
                [("A", "one"), ("B", "two"), ("A", "again"), ("", "orphan"), ("C", "'mixed\""), ("D", "")],
            )
            # setup.read_env keeps the last line of a repeated key and an empty key ...
            self.assertEqual(setup.read_env(path), {"A": "again", "B": "two", "": "orphan", "C": "'mixed\"", "D": ""})
            # ... pipeline.load_env_file keeps the first and drops an empty key, and never overrides a real variable.
            names = ("A", "B", "C", "D")
            saved = {name: os.environ.pop(name, None) for name in names}
            try:
                os.environ["B"] = "from the shell"
                pipeline.load_env_file(path)
                self.assertEqual([os.environ.get(name) for name in names], ["one", "from the shell", "'mixed\"", ""])
                self.assertNotIn("", os.environ)
            finally:
                for name, value in saved.items():
                    os.environ.pop(name, None)
                    if value is not None:
                        os.environ[name] = value
            self.assertEqual(iter_env_pairs(Path(tmp) / "absent.env"), [])

    def test_imported_posting_ids_keep_linkedin_ids_except_for_the_manual_csv(self):
        import hashlib

        import pipeline

        url = "https://www.linkedin.com/jobs/view/3912345678/?trackingId=abc"
        hashed = hashlib.sha256(pipeline.canonical_url(url).encode("utf-8")).hexdigest()[:20]
        self.assertEqual(pipeline.url_external_id(url, linkedin_ids=True), "3912345678")
        self.assertEqual(pipeline.url_external_id(url, linkedin_ids=False), hashed)
        other = "https://boards.example.test/jobs/1"
        self.assertEqual(
            pipeline.url_external_id(other, linkedin_ids=True), pipeline.url_external_id(other, linkedin_ids=False),
        )

    def test_source_key_keeps_its_keyerror_that_system_status_relies_on(self):
        import pipeline

        self.assertEqual(pipeline.source_key({"kind": "greenhouse", "token": "acme"}), "greenhouse:acme")
        self.assertEqual(pipeline.source_key({"kind": "workday", "tenant": "t", "site": "s"}), "workday:t:s")
        with self.assertRaises(KeyError):
            pipeline.source_key({"token": "acme"})
        # The merge key is the same string lowercased; its .get("kind", "") never helps, since source_identity needs kind too.
        self.assertEqual(pipeline._source_merge_key({"kind": "Greenhouse", "token": "ACME"}), "greenhouse:acme")
        with self.assertRaises(KeyError):
            pipeline._source_merge_key({"token": "acme"})

    def test_pipeline_connect_takes_a_path_and_defaults_to_the_module_path_read_at_call_time(self):
        import sqlite3

        import pipeline

        with tempfile.TemporaryDirectory() as tmp:
            explicit = Path(tmp) / "nested" / "explicit.db"
            pipeline.connect(explicit).close()
            self.assertTrue(explicit.is_file())
            default = Path(tmp) / "default.db"
            with mock.patch.object(pipeline, "DB_PATH", default):
                pipeline.connect().close()
            self.assertTrue(default.is_file())
            with closing(sqlite3.connect(explicit)) as conn:
                tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertIn("jobs", tables)

    def test_legacy_create_database_does_not_move_the_global_db_path(self):
        import pipeline
        from opportunity_app import legacy

        before = pipeline.DB_PATH
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "data" / "pipeline.db"
            legacy.create_database(target)
            self.assertTrue(target.is_file())
        self.assertEqual(pipeline.DB_PATH, before)

    def test_one_ruleset_version_constant_backs_every_fit_score_read_and_write(self):
        from opportunity_app import schema
        from pipeline_core.read_model import RULESET_VERSION

        self.assertEqual(RULESET_VERSION, "legacy-v1")  # the SQL views in migrations/0001, 0020 and 0021 bake this in
        self.assertFalse(hasattr(schema, "RULESET_VERSION") and schema.RULESET_VERSION is not RULESET_VERSION)
        # The migration_runs key is a different concept that happens to read the same today.
        self.assertEqual(schema.LEGACY_MIGRATION_KEY, "legacy-v1")
        for relative in ("actions.py", "extension_apply.py", "profile.py"):
            with self.subTest(module=relative):
                text = (ROOT / "opportunity_app" / relative).read_text(encoding="utf-8")
                self.assertNotIn("legacy-v1", text)

    def test_the_closed_application_stages_are_defined_once(self):
        from opportunity_app import actions, urgent

        self.assertIs(urgent.CLOSED_APPLICATION_STAGES, actions.CLOSED_APPLICATION_STAGES)
        self.assertEqual(tuple(actions.CLOSED_APPLICATION_STAGES), ("rejected", "withdrawn", "archived"))

    def test_the_job_types_the_api_accepts_are_the_job_types_the_worker_handles(self):
        from helpers_platform import build_and_migrate
        from opportunity_app import api, worker

        accepted = set(get_args(api.JobCreateRequest.model_fields["job_type"].annotation))
        seen: dict[str, object] = {}

        def capture(conn, handlers, **kwargs):
            seen.update(handlers)
            return None

        with tempfile.TemporaryDirectory() as tmp:
            _, platform_path = build_and_migrate(Path(tmp))
            with mock.patch.object(worker, "run_next_job", capture):
                worker.run_once(platform_path)
        self.assertEqual(accepted, set(seen))

    def test_confined_path_accepts_a_direct_child_and_refuses_everything_else(self):
        from opportunity_app.storage_paths import confined_path

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "store"
            root.mkdir()
            self.assertEqual(confined_path(root, "a.pdf"), (root / "a.pdf").resolve())
            for name in ("../a.pdf", "sub/a.pdf", "..", str(Path(tmp) / "elsewhere.pdf"), ""):
                with self.subTest(name=name):
                    self.assertIsNone(confined_path(root, name))

    def test_each_store_still_raises_its_own_error_for_a_name_outside_its_folder(self):
        from opportunity_app import captures, preparation, resumes

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(captures.CaptureValidationError):
                captures._safe_path(root, "../x")
            with self.assertRaises(resumes.ResumeValidationError):
                resumes._safe_storage_path(root, "../x")
            self.assertEqual(captures._safe_path(root, "x"), (root.resolve() / "x"))
            self.assertEqual(resumes._safe_storage_path(root, "x"), (root.resolve() / "x"))
            self.assertTrue(hasattr(preparation, "PreparationNotFoundError"))


if __name__ == "__main__":
    unittest.main()
