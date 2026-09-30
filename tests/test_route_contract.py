"""The web app's public surface, pinned: the ordered route table and the OpenAPI document.

api.py holds every route in one create_app. A refactor that splits it into routers, moves the page routes, or re-registers the
/assets mount in a different place changes what the app answers without failing any single-route test: route order decides which
of two overlapping paths wins, and the /assets mount sits between /api/v1/stats and the page routes on purpose. These two
snapshots are the contract such a change must keep byte-identical:

* tests/fixtures/route_table.json  every route and mount in registration order, with path, methods, endpoint name, operation id,
  response model, status code and whether it is in the schema, plus the middleware stack and the exception handlers.
* tests/fixtures/openapi.json      app.openapi(), normalized (sorted keys, two-space indent).

A deliberate change (a new route, a renamed endpoint) updates them on purpose:

    UPDATE_SNAPSHOTS=1 py -3 -m unittest tests.test_route_contract        (PowerShell: $env:UPDATE_SNAPSHOTS = "1")

Review the snapshot diff like any other code change, and do not regenerate in parallel (-n auto) or alongside other suites. A pure
refactor must never need to regenerate them.
"""

import difflib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi.routing import APIRoute
from starlette.routing import Mount

from opportunity_app import STATIC_DIR
from opportunity_app.api import create_app

FIXTURES = Path(__file__).resolve().parent / "fixtures"
ROUTE_TABLE = FIXTURES / "route_table.json"
OPENAPI = FIXTURES / "openapi.json"
UPDATE = os.environ.get("UPDATE_SNAPSHOTS") == "1"
DIFF_LINES = 80


def _model_name(model):
    if model is None:
        return None
    if isinstance(model, type) and not getattr(model, "__args__", None):
        return model.__name__
    return str(model)  # a typing form such as dict[str, int]: its text is the contract


def _route_entry(route):
    if isinstance(route, APIRoute):
        return {
            "kind": "route",
            "path": route.path,
            "methods": sorted(route.methods or ()),
            "endpoint": route.endpoint.__name__,
            "operation_id": route.operation_id or route.unique_id,
            "response_model": _model_name(route.response_model),
            "status_code": route.status_code,
            "include_in_schema": route.include_in_schema,
        }
    if isinstance(route, Mount):
        return {"kind": "mount", "path": route.path, "name": route.name, "app": type(route.app).__name__}
    return {
        "kind": type(route).__name__,
        "path": getattr(route, "path", None),
        "name": getattr(route, "name", None),
        "methods": sorted(getattr(route, "methods", None) or ()),
        "include_in_schema": getattr(route, "include_in_schema", None),
    }


def _middleware_name(item):
    options = getattr(item, "kwargs", None) or {}
    dispatch = options.get("dispatch")
    cls = getattr(item, "cls", None)
    name = getattr(cls, "__name__", str(cls))
    return f"{name}({dispatch.__name__})" if dispatch is not None else name


def build_snapshots():
    """(route table text, openapi text) for a freshly built app over a throwaway database path."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        app = create_app(
            db_path=root / "platform.db",
            access_token="route-contract-owner",
            employer_token="route-contract-employer",
            admin_token="route-contract-admin",
            static_dir=STATIC_DIR,
            resume_storage=root / "resumes",
            capture_storage=root / "captures",
            interview_storage=root / "audio",
            apply_storage=root / "apply",
            start_call_prep_worker=False,
            start_inbox_watcher=False,
            start_automation_worker=False,
        )
        table = {
            "routes": [_route_entry(route) for route in app.router.routes],
            "middleware": [_middleware_name(item) for item in app.user_middleware],
            "exception_handlers": sorted(getattr(key, "__name__", str(key)) for key in app.exception_handlers),
        }
        openapi = app.openapi()
    return _dump(table), _dump(openapi)


def _dump(value):
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _read(path):
    return path.read_text(encoding="utf-8") if path.exists() else None


def _diff(name, expected, actual):
    lines = list(difflib.unified_diff(expected.splitlines(), actual.splitlines(), f"{name} (committed)", f"{name} (built now)", lineterm="", n=2))
    shown = lines[:DIFF_LINES]
    more = f"\n... {len(lines) - DIFF_LINES} more diff lines" if len(lines) > DIFF_LINES else ""
    return "\n".join(shown) + more


class RouteContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.table_text, cls.openapi_text = build_snapshots()

    def check(self, path, actual):
        if UPDATE:
            path.write_text(actual, encoding="utf-8", newline="\n")
            return
        expected = _read(path)
        self.assertIsNotNone(expected, f"{path.name} is missing: generate it with UPDATE_SNAPSHOTS=1 (see this file's docstring)")
        if expected != actual:
            self.fail(
                f"{path.name} no longer matches the app. A refactor must keep it identical; a deliberate API change regenerates it "
                f"with UPDATE_SNAPSHOTS=1.\n{_diff(path.name, expected, actual)}"
            )

    def test_the_route_table_matches_the_committed_snapshot(self):
        self.check(ROUTE_TABLE, self.table_text)

    def test_the_openapi_document_matches_the_committed_snapshot(self):
        self.check(OPENAPI, self.openapi_text)

    def test_the_assets_mount_sits_between_stats_and_the_page_routes(self):
        routes = json.loads(self.table_text)["routes"]
        paths = [(entry["kind"], entry["path"]) for entry in routes]
        mount = paths.index(("mount", "/assets"))
        self.assertLess(paths.index(("route", "/api/v1/stats")), mount)
        self.assertLess(mount, paths.index(("route", "/employer")))
        self.assertLess(mount, paths.index(("route", "/")))

    def test_the_snapshot_is_built_the_same_way_twice(self):
        # A snapshot that changes between two builds of the same code would fail every later phase for no reason.
        again_table, again_openapi = build_snapshots()
        self.assertEqual(again_table, self.table_text)
        self.assertEqual(again_openapi, self.openapi_text)

    def test_the_diff_message_names_what_changed(self):
        shown = _diff("x.json", '{\n  "a": 1\n}\n', '{\n  "a": 2\n}\n')
        self.assertIn('-  "a": 1', shown)
        self.assertIn('+  "a": 2', shown)


if __name__ == "__main__":
    unittest.main()
