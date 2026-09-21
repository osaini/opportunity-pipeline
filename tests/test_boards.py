"""Tracking a company's job board from the web app, under the CLI's identity rule."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi.testclient import TestClient

import pipeline
from opportunity_app import STATIC_DIR
from opportunity_app.api import create_app
from opportunity_app.boards import LOOKUP_TTL_SECONDS, BoardTracker

from helpers_platform import build_and_migrate

CATALOG = {
    "ats_sources": [{"kind": "greenhouse", "company": "Known Co", "token": "knownco"}],
    "discovery_title_terms": ["intern"],
}


def greenhouse(slug):
    boards = {
        "acme": {"board_name": "Acme", "titles": ["Robotics Intern", "Staff Engineer"], "field": "token"},
        # Token guessing finds impostors; the board's own name gives it away.
        "archer": {"board_name": "Archer Veterinary Clinic", "titles": ["Veterinary Intern"], "field": "token"},
    }
    return boards.get(slug)


def ashby(slug):
    if slug == "sierra":
        return {"board_name": None, "titles": ["Software Engineer", "AI Intern"], "field": "board"}
    return None


def lever(_slug):
    return None


class TrackerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.sources = self.root / "sources.json"
        self.sources.write_text(json.dumps(CATALOG), encoding="utf-8")
        self.local = self.root / "sources.local.json"
        probes = mock.patch.dict(pipeline.DISCOVERY_VENDORS, {"greenhouse": greenhouse, "ashby": ashby, "lever": lever})
        probes.start()
        self.addCleanup(probes.stop)
        self.now = [0.0]
        self.tracker = BoardTracker(sources_path=self.sources, local_path=self.local, clock=lambda: self.now[0])

    def tearDown(self):
        self.tmp.cleanup()

    def local_entries(self):
        return json.loads(self.local.read_text(encoding="utf-8"))["ats_sources"] if self.local.exists() else []


class BoardTrackerTests(TrackerCase):
    def test_a_board_that_names_the_company_is_added_on_one_click(self):
        found = self.tracker.look_up("Acme Inc.")
        self.assertEqual((found["status"], found["identity"], found["kind"], found["slug"]), ("resolved", "confirmed", "greenhouse", "acme"))
        self.assertEqual((found["total"], found["matching"]), (2, 1))
        self.assertEqual(found["sample_titles"][0], "Robotics Intern")
        self.assertEqual(self.local_entries(), [])

        added = self.tracker.add(found["lookup_id"])
        self.assertTrue(added["added"])
        self.assertEqual(self.local_entries(), [{"kind": "greenhouse", "company": "Acme", "token": "acme", "enabled": True}])
        # The shared catalog is never touched.
        self.assertEqual(json.loads(self.sources.read_text(encoding="utf-8")), CATALOG)

    def test_a_board_under_another_name_or_no_name_needs_the_student_to_confirm(self):
        impostor = self.tracker.look_up("Archer")
        self.assertEqual((impostor["identity"], impostor["board_name"]), ("review", "Archer Veterinary Clinic"))
        with self.assertRaises(ValueError):
            self.tracker.add(impostor["lookup_id"])
        unnamed = self.tracker.look_up("Sierra")
        self.assertEqual((unnamed["identity"], unnamed["kind"]), ("unverified", "ashby"))
        self.assertEqual(self.tracker.add(unnamed["lookup_id"], student_confirmed=True)["entry"],
                         {"kind": "ashby", "company": "Sierra", "board": "sierra", "enabled": True})
        self.assertEqual(len(self.local_entries()), 1)

    def test_known_unknown_and_expired_lookups(self):
        self.assertEqual(self.tracker.look_up("Known Co")["status"], "already-configured")
        missing = self.tracker.look_up("Nowhere Labs")
        self.assertEqual(missing["status"], "unresolved")
        self.assertNotIn("lookup_id", missing)
        found = self.tracker.look_up("Acme")
        self.now[0] = LOOKUP_TTL_SECONDS + 1
        with self.assertRaises(LookupError):
            self.tracker.add(found["lookup_id"])
        with self.assertRaises(ValueError):
            self.tracker.look_up("   ")

    def test_adding_the_same_board_twice_writes_it_once(self):
        first = self.tracker.look_up("Acme")
        second = self.tracker.look_up("Acme")
        self.tracker.add(first["lookup_id"])
        again = self.tracker.add(second["lookup_id"])
        self.assertEqual((again["added"], again["already_tracked"]), (False, True))
        self.assertEqual(len(self.local_entries()), 1)


class BoardApiTests(TrackerCase):
    def client(self, tracker):
        _, platform_path = build_and_migrate(self.root)
        app = create_app(
            db_path=platform_path, access_token="boards-owner", static_dir=STATIC_DIR,
            resume_storage=self.root / "resumes", capture_storage=self.root / "captures",
            interview_storage=self.root / "interviews", board_tracker=tracker,
        )
        return TestClient(app)

    def test_the_owner_looks_up_and_adds_a_board(self):
        headers = {"Authorization": "Bearer boards-owner"}
        with self.client(self.tracker) as client:
            found = client.post("/api/v1/sources/lookup", headers=headers, json={"company": "Sierra"}).json()
            refused = client.post("/api/v1/sources", headers=headers, json={"lookup_id": found["lookup_id"]})
            self.assertEqual(refused.status_code, 422)
            added = client.post("/api/v1/sources", headers=headers, json={"lookup_id": found["lookup_id"], "student_confirmed": True})
            self.assertEqual(added.status_code, 201, added.text)
            self.assertEqual(client.post("/api/v1/sources", headers=headers, json={"lookup_id": "gone"}).status_code, 404)
            self.assertEqual(client.post("/api/v1/sources/lookup", json={"company": "Acme"}).status_code, 401)
        self.assertEqual(self.local_entries()[0]["board"], "sierra")

    def test_a_scratch_database_cannot_add_boards(self):
        with self.client(None) as client:
            response = client.post("/api/v1/sources/lookup", headers={"Authorization": "Bearer boards-owner"}, json={"company": "Acme"})
        self.assertEqual(response.status_code, 409)


if __name__ == "__main__":
    unittest.main()
