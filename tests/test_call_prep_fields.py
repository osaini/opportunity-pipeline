"""The call prep fields around the notes: who the call is with, whether research can run, and when it goes stale."""

import json
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi.testclient import TestClient

from opportunity_app import STATIC_DIR, outreach_research
from opportunity_app.api import create_app
from opportunity_app.outreach import create_target, get_target, update_target
from opportunity_app.outreach_call_prep import CallPrepWorker
from opportunity_app.outreach_research import research_due, web_researcher
from opportunity_app.core.schema import ensure_product_schema
from opportunity_app.core.database import connect_product

from helpers_platform import build_and_migrate
from helpers_source import static_script_text
from helpers_outreach import BRIEF, DRAFTING_AUTH as AUTH, USER, store_brief
from helpers_outreach import DraftingScriptedProvider as ScriptedProvider

LINK = "https://www.linkedin.com/in/riley-park/"


class ApiCase(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        _, self.platform_path = build_and_migrate(self.root)

    def client(self, **options):
        app = create_app(
            db_path=self.platform_path, access_token="drafting-owner", static_dir=STATIC_DIR,
            resume_storage=self.root / "resumes", capture_storage=self.root / "captures",
            interview_storage=self.root / "interviews", agent_provider_factory=lambda *_: ScriptedProvider([]),
            outreach_draft_provider="anthropic", start_call_prep_worker=False, **options,
        )
        client = TestClient(app)
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        return client

    def create(self, client, company="Patchco", **fields):
        response = client.post("/api/v1/outreach", headers=AUTH, json={"company": company, **fields})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()


class InterviewerFieldTests(ApiCase):
    def test_the_name_and_link_typed_in_the_pane_are_saved(self):
        # The pane sends both fields, and the request model used to drop them: 200 with both blank.
        client = self.client()
        created = self.create(client)
        saved = client.patch(f"/api/v1/outreach/{created['id']}", headers=AUTH, json={
            "interviewer_name": "Riley Park", "interviewer_linkedin": LINK,
        })
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual((saved.json()["interviewer_name"], saved.json()["interviewer_linkedin"]), ("Riley Park", LINK))
        again = client.get(f"/api/v1/outreach/{created['id']}", headers=AUTH).json()
        self.assertEqual((again["interviewer_name"], again["interviewer_linkedin"]), ("Riley Park", LINK))
        cleared = client.patch(f"/api/v1/outreach/{created['id']}", headers=AUTH, json={"interviewer_name": "", "interviewer_linkedin": ""}).json()
        self.assertEqual((cleared["interviewer_name"], cleared["interviewer_linkedin"]), ("", ""))

    def test_a_link_without_a_scheme_is_kept_as_typed(self):
        client = self.client()
        created = self.create(client)
        saved = client.patch(f"/api/v1/outreach/{created['id']}", headers=AUTH, json={"interviewer_linkedin": "linkedin.com/in/riley-park"})
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(saved.json()["interviewer_linkedin"], "linkedin.com/in/riley-park")

    def test_a_link_that_is_not_a_linkedin_profile_is_refused(self):
        client = self.client()
        created = self.create(client)
        for bad in (
            "https://evil.example/in/riley-park", "https://linkedin.com.evil.example/in/riley-park",
            "https://www.linkedin.com/company/chargebot", "https://www.linkedin.com/in/", "https://www.linkedin.com/",
            "riley-park", "https://user:pw@www.linkedin.com/in/riley-park", "http://localhost/in/riley-park",
        ):
            refused = client.patch(f"/api/v1/outreach/{created['id']}", headers=AUTH, json={"interviewer_linkedin": bad})
            self.assertEqual(refused.status_code, 422, f"{bad}: {refused.text}")
            self.assertIn("interviewer_linkedin", json.dumps(refused.json()))
        self.assertEqual(client.get(f"/api/v1/outreach/{created['id']}", headers=AUTH).json()["interviewer_linkedin"], "")

    def test_a_link_can_be_given_when_the_target_is_created(self):
        client = self.client()
        created = self.create(client, company="Linkco", interviewer_name="Riley Park", interviewer_linkedin=LINK)
        self.assertEqual((created["interviewer_name"], created["interviewer_linkedin"]), ("Riley Park", LINK))


class ResearchAvailabilityTests(ApiCase):
    def researcher_client(self, researcher):
        worker = CallPrepWorker(
            self.platform_path, provider_factory=lambda *_: ScriptedProvider([]), provider="anthropic", researcher=researcher,
        )
        return self.client(call_prep_worker=worker), worker

    def test_research_is_not_offered_when_no_agent_is_installed(self):
        client, worker = self.researcher_client(web_researcher(lambda: None))
        created = self.create(client, website="https://patchco.example")
        with mock.patch.object(outreach_research, "cli_available", return_value=False):
            listing = client.get("/api/v1/outreach", headers=AUTH).json()["company_research"]
            self.assertFalse(listing["available"])
            self.assertIn("No research agent is set up", listing["reason"])
            refused = client.post(f"/api/v1/outreach/{created['id']}/research", headers=AUTH)
            self.assertEqual(refused.status_code, 409, refused.text)
            self.assertIn("No research agent is set up", refused.json()["detail"])
        self.assertIsNone(client.get(f"/api/v1/outreach/{created['id']}", headers=AUTH).json()["tech_brief_job"], "no job that cannot run")
        self.assertEqual(worker.run_pending(), 0)

    def test_research_is_offered_when_an_agent_is_installed(self):
        client, worker = self.researcher_client(web_researcher(lambda: None))
        created = self.create(client, website="https://patchco.example")
        with mock.patch.object(outreach_research, "cli_available", return_value=True):
            self.assertEqual(client.get("/api/v1/outreach", headers=AUTH).json()["company_research"], {"available": True})
            queued = client.post(f"/api/v1/outreach/{created['id']}/research", headers=AUTH)
            self.assertEqual(queued.status_code, 202, queued.text)

    def test_a_queued_job_with_no_agent_ends_at_once_and_says_why(self):
        client, worker = self.researcher_client(web_researcher(lambda: None))
        created = self.create(client, website="https://patchco.example")
        with mock.patch.object(outreach_research, "cli_available", return_value=True):
            self.assertEqual(client.post(f"/api/v1/outreach/{created['id']}/research", headers=AUTH).status_code, 202)
        # The CLI went away after the job was queued.
        with mock.patch.object(outreach_research, "cli_available", return_value=False):
            worker.run_pending()
        target = client.get(f"/api/v1/outreach/{created['id']}", headers=AUTH).json()
        self.assertEqual(target["tech_brief_job"]["state"], "succeeded", "not retried until it dies")
        self.assertIn("No research agent is set up", target["tech_brief_error"])

    def test_a_stand_in_researcher_needs_no_agent(self):
        client, worker = self.researcher_client(lambda conn, target_id, user_id: None)
        created = self.create(client)
        with mock.patch.object(outreach_research, "cli_available", return_value=False):
            self.assertEqual(client.get("/api/v1/outreach", headers=AUTH).json()["company_research"], {"available": True})
            self.assertEqual(client.post(f"/api/v1/outreach/{created['id']}/research", headers=AUTH).status_code, 202)

    def test_the_pane_says_why_research_is_off(self):
        self.assertIn("context.research?.reason", static_script_text())


class StaleResearchTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        _, self.platform_path = build_and_migrate(Path(self.tempdir.name))
        self.conn = connect_product(self.platform_path)
        self.addCleanup(self.conn.close)
        ensure_product_schema(self.conn)
        self.target = create_target(self.conn, {"company": "Chargebot", "website": "https://chargebot.example"}, user_id=USER)
        self.id = self.target["id"]
        update_target(self.conn, self.id, {"status": "replied"}, user_id=USER)
        self.store()

    def store(self):
        store_brief(self.conn, self.id, at="2099-01-01T00:00:00+00:00")
        self.conn.execute(
            "UPDATE outreach_targets SET interviewer_json=?, interviewer_at=?, interviewer_error='old', interviewer_tried_at=?, "
            "tech_brief_tried_at=? WHERE id=?",
            (json.dumps({"name": "Dana Ortiz", "key": "dana ortiz|", "notes": [{"text": "Led controls"}]}),
             "2099-01-01T00:00:00+00:00", "2099-01-01T00:00:00+00:00", "2099-01-01T00:00:00+00:00", self.id),
        )
        self.conn.commit()
        stored = get_target(self.conn, self.id, user_id=USER)
        self.assertEqual(len(stored["tech_brief"]["facts"]), len(BRIEF["facts"]))
        self.assertFalse(research_due(stored), "a fresh brief is not researched again")

    def events(self):
        return [event for event in get_target(self.conn, self.id, user_id=USER, include_events=True)["events"] if event["event_type"] == "research_cleared"]

    def assert_cleared(self, target):
        self.assertEqual(target["tech_brief"], {})
        self.assertEqual(target["interviewer"], {})
        self.assertIsNone(target["tech_brief_at"])
        self.assertIsNone(target["interviewer_at"])
        self.assertEqual((target["tech_brief_by"], target["tech_brief_error"], target["interviewer_error"]), ("", "", ""))
        self.assertIsNone(target["tech_brief_tried_at"], "a new company may be researched at once")
        self.assertIsNone(target["interviewer_tried_at"])
        self.assertTrue(research_due(target), "the new company gets its own research")

    def test_a_new_company_name_drops_the_old_company_research(self):
        # Facts checked against Chargebot's pages must not print as checked facts about Voltara Foods.
        after = update_target(self.conn, self.id, {"company": "Voltara Foods"}, user_id=USER)
        self.assert_cleared(after)
        events = self.events()
        self.assertEqual(len(events), 1)
        self.assertIn("Chargebot", events[0]["detail"])
        self.assertIn("Voltara Foods", events[0]["detail"])

    def test_a_new_website_drops_the_old_research(self):
        after = update_target(self.conn, self.id, {"website": "https://voltara.example"}, user_id=USER)
        self.assert_cleared(after)
        self.assertIn("chargebot.example", self.events()[0]["detail"])

    def test_the_same_company_written_another_way_keeps_its_research(self):
        for change in ({"company": "The CHARGEBOT, Inc."}, {"website": "http://www.chargebot.example/about"}, {"summary": "Robots"}):
            after = update_target(self.conn, self.id, change, user_id=USER)
            self.assertEqual(len(after["tech_brief"]["facts"]), len(BRIEF["facts"]), change)
            self.assertEqual(after["interviewer"]["name"], "Dana Ortiz", change)
        self.assertEqual(self.events(), [])

    def test_a_first_website_for_the_same_company_keeps_its_research(self):
        self.conn.execute("UPDATE outreach_targets SET website='' WHERE id=?", (self.id,))
        self.conn.commit()
        after = update_target(self.conn, self.id, {"website": "https://chargebot.example"}, user_id=USER)
        self.assertEqual(len(after["tech_brief"]["facts"]), len(BRIEF["facts"]), "research on the company by name still describes it")
        self.assertEqual(self.events(), [])

    def test_the_students_own_interviewer_entry_survives(self):
        update_target(self.conn, self.id, {"interviewer_name": "Riley Park", "interviewer_linkedin": LINK}, user_id=USER)
        after = update_target(self.conn, self.id, {"company": "Voltara Foods"}, user_id=USER)
        self.assertEqual((after["interviewer_name"], after["interviewer_linkedin"]), ("Riley Park", LINK))

    def test_a_company_with_no_research_logs_nothing(self):
        other = create_target(self.conn, {"company": "Plainco"}, user_id=USER)
        update_target(self.conn, other["id"], {"company": "Plainco Two"}, user_id=USER)
        events = get_target(self.conn, other["id"], user_id=USER, include_events=True)["events"]
        self.assertNotIn("research_cleared", [event["event_type"] for event in events])


class ReadingClaimLabelTests(unittest.TestCase):
    def test_the_claims_list_does_not_say_the_reading_ignores_the_research(self):
        script = static_script_text()
        claims = script[script.index("What these notes are based on"):]
        claims = claims[:claims.index("call_prep_generated_by) {")]
        # The reading is built only on checked research facts, so it is labelled as a read of the
        # research, still an inference and not a fact, and links the pages it cites.
        self.assertIn('claim.section === "reading"', claims)
        self.assertIn("my read of the research (inference, not a checked fact)", claims)
        self.assertIn("claim.sources", claims)
        reading = claims[claims.index('claim.section === "reading"'):]
        reading = reading[:reading.index("return;")]
        self.assertNotIn("not from your profile or research", reading)
        self.assertEqual(reading.count("safeExternalUrl(url)"), 1)


if __name__ == "__main__":
    unittest.main()
