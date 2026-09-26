"""Automation the student switches on: drafts written for them, and a new contact found after a bounce."""

import sys
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx
from fastapi.testclient import TestClient

from opportunity_app import STATIC_DIR
from opportunity_app.api import create_app
from opportunity_app.outreach import create_target, get_target, update_target
from opportunity_app.outreach_automation import (
    AUTO_DRAFT_FAILED,
    AutomationWorker,
    auto_draft,
    draft_due,
    recover_contact,
    recovery_due,
    settings,
    update_settings,
)
from opportunity_app.outreach_delivery import record_bounce
from opportunity_app.schema import connect_product

from helpers_platform import build_and_migrate
from test_outreach_discovery import safe_fetcher, site_transport

USER = "local-user"
AUTH = {"Authorization": "Bearer automation-owner"}
SITE = {
    "bovi.test": {
        "/robots.txt": "",
        "/": '<nav><a href="/team">Team</a></nav><p>Write to <a href="mailto:careers@bovi.test">careers@bovi.test</a> '
             'or <a href="mailto:info@bovi.test">info@bovi.test</a></p>',
        "/team": '<div><h3><a href="mailto:dana.ruiz@bovi.test">Dana Ruiz</a></h3><p>Co-Founder &amp; CTO</p></div>',
    },
}


def legacy(_provider, _model):
    raise AssertionError("the template drafter calls no model")


class AutomationTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        _, self.platform_path = build_and_migrate(Path(self.tempdir.name))
        self.conn = connect_product(self.platform_path)

    def tearDown(self):
        self.conn.close()
        self.tempdir.cleanup()

    def target(self, **values):
        return create_target(self.conn, {
            "company": "Bovi", "website": "https://bovi.test", "location": "Austin, TX",
            "contact_email": "info@bovi.test", **values,
        }, user_id=USER)

    def bounced(self):
        target = self.target(email_subject="Internship question", email_body="Hi Bovi team,\n\nA short note.\n\nSam", status="sent")
        return record_bounce(self.conn, target["id"], user_id=USER, reason="Address not found", source="gmail")

    def fetch(self):
        transport, requested = site_transport(SITE, mx=False)
        return httpx.Client(transport=transport), requested

    def test_switches_are_off_until_turned_on(self):
        self.assertEqual(settings(self.conn, user_id=USER), {"auto_drafts": False, "bounce_recovery": False, "scheduled_sending": False})
        self.assertEqual(update_settings(self.conn, {"bounce_recovery": True}, user_id=USER)["bounce_recovery"], True)
        with self.assertRaises(ValueError):
            update_settings(self.conn, {"autopilot": True}, user_id=USER)

    def test_after_a_bounce_the_best_other_contact_is_applied_and_greeted(self):
        target = self.bounced()
        self.assertEqual(recovery_due(self.conn, user_id=USER), [target["id"]])
        client, _ = self.fetch()
        with client:
            result = recover_contact(self.conn, target["id"], user_id=USER, fetcher=safe_fetcher(client), contact_delay=0)
        self.assertEqual(result["to"], "dana.ruiz@bovi.test", "a confirmed person beats a shared inbox")
        after = get_target(self.conn, target["id"], user_id=USER, include_events=True)
        self.assertEqual((after["contact_email"], after["contact_name"]), ("dana.ruiz@bovi.test", "Dana Ruiz"))
        self.assertEqual(after["email_body"].splitlines()[0], "Hi Dana,")
        self.assertEqual(after["draft_status"], "generated", "the student reviews it before it goes again")
        self.assertFalse(after["contact_bounced"])
        self.assertEqual(recovery_due(self.conn, user_id=USER), [], "each bounce is searched once")
        self.assertIn("Chose dana.ruiz@bovi.test", after["events"][0]["detail"])

    def test_an_address_that_bounced_is_never_chosen_again(self):
        target = self.bounced()
        # The only people on the site: the address that bounced, and a shared inbox.
        site = {"bovi.test": {"/robots.txt": "", "/": '<a href="mailto:info@bovi.test">info@bovi.test</a> '
                                                      '<a href="mailto:hello@bovi.test">hello@bovi.test</a>'}}
        transport, _ = site_transport(site, mx=False)
        with httpx.Client(transport=transport) as client:
            result = recover_contact(self.conn, target["id"], user_id=USER, fetcher=safe_fetcher(client), contact_delay=0)
        self.assertEqual(result["to"], "hello@bovi.test")

    def test_when_nothing_else_is_found_the_history_says_so(self):
        target = self.bounced()
        site = {"bovi.test": {"/robots.txt": "", "/": '<a href="mailto:info@bovi.test">info@bovi.test</a>'}}
        transport, _ = site_transport(site, mx=False)
        with httpx.Client(transport=transport) as client:
            result = recover_contact(self.conn, target["id"], user_id=USER, fetcher=safe_fetcher(client), contact_delay=0)
        self.assertIsNone(result["to"])
        after = get_target(self.conn, target["id"], user_id=USER, include_events=True)
        self.assertTrue(after["contact_bounced"])
        self.assertIn("Found no other address", after["events"][0]["detail"])

    def test_a_contact_the_student_picked_meanwhile_stands(self):
        target = self.bounced()
        update_target(self.conn, target["id"], {"contact_email": "sam@bovi.test", "contact_name": "Sam Lee"}, user_id=USER)
        client, _ = self.fetch()
        with client:
            result = recover_contact(self.conn, target["id"], user_id=USER, fetcher=safe_fetcher(client), contact_delay=0)
        self.assertIsNone(result["to"])
        self.assertEqual(get_target(self.conn, target["id"], user_id=USER)["contact_email"], "sam@bovi.test")

    def test_draft_due_needs_a_contact_a_location_and_no_draft(self):
        ready = self.target()
        self.target(company="NoLocation", location="", website="https://noloc.test")
        self.target(company="NoContact", contact_email="", website="https://nocontact.test")
        self.target(company="HasDraft", email_body="Hi,\n\nA note.", website="https://hasdraft.test")
        self.target(company="Sent", status="sent", website="https://sent.test")
        self.assertEqual(draft_due(self.conn, user_id=USER), [ready["id"]])

    def test_a_failed_draft_waits_before_it_is_tried_again(self):
        ready = self.target()
        with self.conn:
            self.conn.execute(
                "INSERT INTO outreach_events(id, target_id, user_id, event_type, detail, created_at) VALUES('e1', ?, ?, ?, 'x', ?)",
                (ready["id"], USER, AUTO_DRAFT_FAILED, datetime.now(timezone.utc).isoformat(timespec="microseconds")),
            )
        self.assertEqual(draft_due(self.conn, user_id=USER), [])
        later = datetime.now(timezone.utc) + timedelta(hours=7)
        self.assertEqual(draft_due(self.conn, user_id=USER, now=later), [ready["id"]])

    def test_auto_draft_writes_a_draft_that_waits_for_approval(self):
        ready = self.target()
        self.assertTrue(auto_draft(self.conn, ready["id"], user_id=USER, provider_factory=legacy, draft_provider="legacy")["drafted"])
        after = get_target(self.conn, ready["id"], user_id=USER)
        self.assertEqual((after["draft_status"], after["status"]), ("generated", "drafted"))
        self.assertTrue(after["email_body"].startswith("Hi Bovi team,"))
        self.assertEqual(draft_due(self.conn, user_id=USER), [])

    def test_a_draft_the_model_could_not_write_is_recorded_and_not_retried_at_once(self):
        ready = self.target()

        def down(_provider, _model):
            raise RuntimeError("The model is unreachable")

        result = auto_draft(self.conn, ready["id"], user_id=USER, provider_factory=down, draft_provider="anthropic")
        self.assertFalse(result["drafted"])
        after = get_target(self.conn, ready["id"], user_id=USER, include_events=True)
        self.assertEqual((after["events"][0]["event_type"], after["events"][0]["detail"]), (AUTO_DRAFT_FAILED, "The model is unreachable"))
        self.assertEqual(draft_due(self.conn, user_id=USER), [])

    def test_the_worker_does_only_what_is_switched_on(self):
        target = self.bounced()
        requested = []

        def fetcher_factory():
            transport, seen = site_transport(SITE, mx=False)
            requested.append(seen)
            return safe_fetcher(httpx.Client(transport=transport))

        worker = AutomationWorker(self.platform_path, fetcher_factory=fetcher_factory, contact_delay=0)
        self.assertEqual(worker.run_once(), {"sent": [], "recovered": [], "drafted": []})
        self.assertEqual(requested, [], "off by default: nothing is fetched")
        update_settings(self.conn, {"bounce_recovery": True}, user_id=USER)
        report = worker.run_once()
        self.assertEqual([item["to"] for item in report["recovered"]], ["dana.ruiz@bovi.test"])
        self.assertEqual(get_target(self.conn, target["id"], user_id=USER)["contact_email"], "dana.ruiz@bovi.test")


class AutomationApiTests(unittest.TestCase):
    def test_settings_round_trip(self):
        with tempfile.TemporaryDirectory() as root:
            _, platform_path = build_and_migrate(Path(root))
            app = create_app(db_path=platform_path, access_token="automation-owner", static_dir=STATIC_DIR)
            with TestClient(app) as client:
                self.assertEqual(client.get("/api/v1/outreach/automation", headers=AUTH).json(),
                                 {"auto_drafts": False, "bounce_recovery": False, "scheduled_sending": False})
                saved = client.put("/api/v1/outreach/automation", headers=AUTH, json={"auto_drafts": True})
                self.assertEqual(saved.status_code, 200, saved.text)
                self.assertEqual(saved.json(), {"auto_drafts": True, "bounce_recovery": False, "scheduled_sending": False})
                self.assertEqual(client.get("/api/v1/outreach/automation").status_code, 401)


if __name__ == "__main__":
    unittest.main()
