"""`opportunity_app.api` builds no app at import; `api.app` is built on first access; a server start builds exactly one app.

Before this, importing the module ran `create_app()` (about 0.8 s, wired to the real data/platform.db settings, and it loaded the
checkout's .env into os.environ for every importer, tests included), and `main()` / `launch.serve()` then built a second one.
`load_env_file` runs once per `create_app`, so a spy on it counts app builds without paying for them.
"""

import contextlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

try:
    import realdata_guard
except ImportError:  # imported as tests.<module>, with tests/ not on sys.path
    from tests import realdata_guard
# Guards this module's own run against opening data/*.db (AGENTS.md hard rule 1); see tests/realdata_guard.py.
realdata_guard.install()

ROOT = Path(__file__).resolve().parent.parent

# Runs in a fresh interpreter so the import really happens: counts load_env_file calls, snapshots os.environ around the import,
# then touches api.app and reports what happened.
PROBE = r"""
import json, os, sys
sys.path.insert(0, ".")
import pipeline
calls = []
real = pipeline.load_env_file
pipeline.load_env_file = lambda *a, **k: (calls.append(1), real(*a, **k))[1]
before = dict(os.environ)
import opportunity_app.api as api
after_import = dict(os.environ)
report = {
    "builds_at_import": len(calls),
    "app_attribute_at_import": "app" in vars(api),
    "environ_changed_by_import": before != after_import,
    "uvicorn_imported_by_api": "uvicorn" in vars(api),
}
first = api.app
report["builds_after_first_access"] = len(calls)
report["same_object_on_second_access"] = api.app is first
report["builds_after_second_access"] = len(calls)
report["type"] = type(first).__name__
report["routes"] = len(first.routes)
from fastapi import FastAPI
report["is_fastapi"] = isinstance(first, FastAPI)
try:
    api.nothing_here
except AttributeError as error:
    report["missing_attribute_error"] = str(error)
print("REPORT" + json.dumps(report))
"""


class ImportBuildsNothingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        env = dict(os.environ)
        env.pop("DATABASE_URL", None)
        done = subprocess.run([sys.executable, "-c", PROBE], cwd=ROOT, env=env, capture_output=True, text=True, timeout=300)
        lines = [line for line in done.stdout.splitlines() if line.startswith("REPORT")]
        if not lines:
            raise AssertionError(f"probe failed:\n{done.stdout}\n{done.stderr}")
        cls.report = json.loads(lines[-1][len("REPORT"):])

    def test_importing_the_module_builds_no_app_and_reads_no_env_file(self):
        self.assertEqual(self.report["builds_at_import"], 0)
        self.assertFalse(self.report["app_attribute_at_import"])
        self.assertFalse(self.report["environ_changed_by_import"])

    def test_uvicorn_is_only_imported_where_the_server_starts(self):
        self.assertFalse(self.report["uvicorn_imported_by_api"])

    def test_api_app_still_resolves_to_a_working_app_built_once(self):
        # `uvicorn opportunity_app.api:app` (the Dockerfile) is a getattr on the module.
        self.assertTrue(self.report["is_fastapi"])
        self.assertGreater(self.report["routes"], 100)
        self.assertEqual(self.report["builds_after_first_access"], 1)
        self.assertTrue(self.report["same_object_on_second_access"])
        self.assertEqual(self.report["builds_after_second_access"], 1)

    def test_other_missing_attributes_still_raise_attribute_error(self):
        self.assertIn("nothing_here", self.report["missing_attribute_error"])


class ServerStartBuildsOnceTests(unittest.TestCase):
    """Pins that each start path calls create_app once and hands that app to uvicorn.

    The old double build came from the module body building a second app at import, before these patches apply, so that
    half is pinned by ImportBuildsNothingTests (builds_at_import == 0), not here.
    """

    def counting_create_app(self, api):
        built = []

        def create_app(**kwargs):
            app = mock.MagicMock()
            app.state.access_token = app.state.employer_token = app.state.admin_token = "x"
            built.append(app)
            return app

        return built, mock.patch.object(api, "create_app", create_app)

    def test_main_builds_one_app_and_hands_it_to_uvicorn(self):
        from opportunity_app import api

        import uvicorn

        built, patch_create = self.counting_create_app(api)
        with patch_create, mock.patch.object(uvicorn, "run") as run, mock.patch.object(sys, "argv", ["api"]), \
                contextlib.redirect_stdout(open(os.devnull, "w")):
            self.assertEqual(api.main(), 0)
        self.assertEqual(len(built), 1)
        self.assertEqual(run.call_count, 1)
        self.assertIs(run.call_args.args[0], built[0])

    def test_launch_serve_builds_one_app_and_hands_it_to_uvicorn(self):
        from opportunity_app import api, launch

        import uvicorn

        built, patch_create = self.counting_create_app(api)
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            saved = sys.stdout, sys.stderr
            try:
                with patch_create, mock.patch.object(uvicorn, "run") as run, mock.patch.object(launch, "DATA_DIR", data), \
                        mock.patch.object(launch, "WEB_LOG", data / "web.log"), mock.patch.object(launch, "PID_PATH", data / "web.pid"):
                    self.assertEqual(launch.serve(8799), 0)
            finally:
                sys.stdout, sys.stderr = saved
        self.assertEqual(len(built), 1)
        self.assertEqual(run.call_count, 1)
        self.assertIs(run.call_args.args[0], built[0])


if __name__ == "__main__":
    unittest.main()
