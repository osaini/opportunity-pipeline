"""Automation the student switches on: drafts written for them, and a new contact found after a bounce."""

import sys
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx
from fastapi.testclient import TestClient

from opportunity_app import STATIC_DIR, automation, outreach_drafting
from opportunity_app.api import create_app
from opportunity_app.outreach import create_target, get_target, update_target
from opportunity_app.outreach_automation import (
    AUTO_DRAFT_FAILED,
    WORKER_COMPONENT,
    AutomationWorker,
    auto_draft,
    draft_due,
    recover_contact,
    recovery_due,
    settings,
    update_settings,
)
from opportunity_app.outreach_delivery import record_bounce
from opportunity_app.database import connect_product

from helpers_platform import build_and_migrate
from helpers_outreach import safe_fetcher, site_transport

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

    def bounced(self, **values):
        target = self.target(email_subject="Internship question", email_body="Hi Bovi team,\n\nA short note.\n\nSam", status="sent", **values)
        return record_bounce(self.conn, target["id"], user_id=USER, reason="Address not found", source="gmail")

    def fetch(self):
        transport, requested = site_transport(SITE, mx=False)
        return httpx.Client(transport=transport), requested

    def test_switches_are_off_until_turned_on(self):
        self.assertEqual(settings(self.conn, user_id=USER), {"auto_drafts": False, "bounce_recovery": False, "bounce_auto_resend": False, "scheduled_sending": False, "follow_up_review": False, "form_submission": False})
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
        self.assertEqual(worker.run_once(), {"sent": [], "recovered": [], "drafted": [], "forms": []})
        self.assertEqual(requested, [], "off by default: nothing is fetched")
        update_settings(self.conn, {"bounce_recovery": True}, user_id=USER)
        report = worker.run_once()
        self.assertEqual([item["to"] for item in report["recovered"]], ["dana.ruiz@bovi.test"])
        self.assertEqual(get_target(self.conn, target["id"], user_id=USER)["contact_email"], "dana.ruiz@bovi.test")

    def test_a_paused_student_gets_no_recovery_and_no_draft(self):
        bounced = self.bounced()
        ready = self.target(company="Kiva", website="https://kiva.test", contact_email="hi@kiva.test")
        update_settings(self.conn, {"bounce_recovery": True, "auto_drafts": True}, user_id=USER)
        self.assertEqual((recovery_due(self.conn, user_id=USER), draft_due(self.conn, user_id=USER)), ([bounced["id"]], [ready["id"]]))
        fetchers = []

        def fetcher_factory():
            fetchers.append(True)
            transport, _ = site_transport(SITE, mx=False)
            return safe_fetcher(httpx.Client(transport=transport))

        worker = AutomationWorker(self.platform_path, fetcher_factory=fetcher_factory, provider_factory=legacy,
                                  draft_provider="legacy", contact_delay=0)
        automation.set_paused(self.conn, USER, True)
        self.assertEqual(worker.run_once(), {"sent": [], "recovered": [], "drafted": [], "forms": []})
        self.assertEqual(fetchers, [], "no site is searched while paused")
        self.assertEqual(get_target(self.conn, bounced["id"], user_id=USER)["contact_email"], "info@bovi.test")
        self.assertEqual(get_target(self.conn, ready["id"], user_id=USER)["email_body"], "", "no draft is written while paused")
        automation.set_paused(self.conn, USER, False)
        report = worker.run_once()
        self.assertEqual(([item["to"] for item in report["recovered"]], [item["target_id"] for item in report["drafted"]]),
                         (["dana.ruiz@bovi.test"], [ready["id"]]), "both run once resumed")

    def test_a_pause_during_a_recovery_pass_changes_no_contact(self):
        first = self.bounced()
        second = self.bounced(company="Bovi Two")
        update_settings(self.conn, {"bounce_recovery": True}, user_id=USER)
        seen = {}

        def fetcher_factory():
            transport, _ = site_transport(SITE, mx=False)

            def handler(request):
                # The student presses Pause while the first company's site is being read.
                if "pause" not in seen:
                    with closing(connect_product(self.platform_path)) as other:
                        seen["pause"] = automation.set_paused(other, USER, True)
                return transport.handle_request(request)

            return safe_fetcher(httpx.Client(transport=httpx.MockTransport(handler)))

        report = AutomationWorker(self.platform_path, fetcher_factory=fetcher_factory, contact_delay=0).run_once()
        self.assertEqual(seen["pause"], {"paused": True, "in_flight": []}, "the instrument: the pause landed mid-pass")
        self.assertEqual(report["recovered"], [])
        for target in (first, second):
            after = get_target(self.conn, target["id"], user_id=USER)
            self.assertEqual((after["contact_email"], after["contact_bounced"]), ("info@bovi.test", True), after["company"])
        self.assertEqual(sorted(recovery_due(self.conn, user_id=USER)), sorted([first["id"], second["id"]]),
                         "neither counts as searched, so both are searched again on resume")

    def test_a_pause_during_a_recovery_pass_stops_it_before_the_next_company(self):
        anvil = self.bounced(company="Anvil", website="https://anvil.test", contact_email="info@anvil.test")
        bovi = self.bounced()
        update_settings(self.conn, {"bounce_recovery": True}, user_id=USER)
        self.assertEqual(recovery_due(self.conn, user_id=USER), [anvil["id"], bovi["id"]], "the instrument: Anvil is searched first")
        # Anvil's site names nobody else, so its search ends without a contact to apply.
        sites = {**SITE, "anvil.test": {"/robots.txt": "", "/": '<a href="mailto:info@anvil.test">info@anvil.test</a>'}}
        hosts = []

        def fetcher_factory():
            transport, _ = site_transport(sites, mx=False)

            def handler(request):
                hosts.append(request.url.host)
                if len(hosts) == 1:
                    with closing(connect_product(self.platform_path)) as other:
                        automation.set_paused(other, USER, True)
                return transport.handle_request(request)

            return safe_fetcher(httpx.Client(transport=httpx.MockTransport(handler)))

        report = AutomationWorker(self.platform_path, fetcher_factory=fetcher_factory, contact_delay=0).run_once()
        self.assertEqual({host for host in hosts if host.endswith(".test")}, {"anvil.test"}, "Bovi's site is never read")
        self.assertEqual([item["company"] for item in report["recovered"]], ["Anvil"], "the search under way finishes and is recorded")
        self.assertEqual(get_target(self.conn, bovi["id"], user_id=USER)["contact_email"], "info@bovi.test")
        self.assertEqual(recovery_due(self.conn, user_id=USER), [bovi["id"]], "Bovi is searched once resumed")

    def test_a_pause_while_a_draft_is_being_written_saves_nothing(self):
        ready = self.target()
        update_settings(self.conn, {"auto_drafts": True}, user_id=USER)
        real = outreach_drafting.template_draft

        def pause_then_write(*args):
            # Stands in for a model call the student pauses during.
            with closing(connect_product(self.platform_path)) as other:
                automation.set_paused(other, USER, True)
            return real(*args)

        with mock.patch.object(outreach_drafting, "template_draft", pause_then_write):
            result = auto_draft(self.conn, ready["id"], user_id=USER, provider_factory=legacy, draft_provider="legacy", automatic=True)
        self.assertEqual((result["drafted"], result["paused"]), (False, True))
        after = get_target(self.conn, ready["id"], user_id=USER, include_events=True)
        self.assertEqual((after["email_body"], after["status"]), ("", "not_started"))
        self.assertNotIn(AUTO_DRAFT_FAILED, [event["event_type"] for event in after["events"]], "a pause is not a failure")
        automation.set_paused(self.conn, USER, False)
        self.assertEqual(draft_due(self.conn, user_id=USER), [ready["id"]], "so it is written once resumed, not six hours later")


class WorkerHealthTests(unittest.TestCase):
    """Each worker pass records automation.worker, so a step that keeps failing is visible, not silent."""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        _, self.platform_path = build_and_migrate(Path(self.tempdir.name))
        self.conn = connect_product(self.platform_path)

    def tearDown(self):
        self.conn.close()
        self.tempdir.cleanup()

    def worker_health(self):
        return {row["user_id"]: dict(row) for row in self.conn.execute(
            "SELECT * FROM automation_health WHERE component=?", (WORKER_COMPONENT,),
        ).fetchall()}

    def test_a_pass_records_ok_only_for_students_with_something_on(self):
        worker = AutomationWorker(self.platform_path, fetcher_factory=lambda: None)
        worker.run_once()
        self.assertEqual(self.worker_health(), {}, "nothing on, nothing due: no row")
        update_settings(self.conn, {"bounce_recovery": True}, user_id=USER)
        worker.run_once()
        row = self.worker_health()[USER]
        self.assertIsNotNone(row["last_ok_at"])
        self.assertEqual((row["last_error"], row["last_error_at"]), ("", None))
        # Paused, the worker still ran for them (and held everything back), so it is still ok.
        automation.set_paused(self.conn, USER, True)
        worker.run_once()
        self.assertEqual(self.worker_health()[USER]["last_error"], "")

    def test_a_step_that_raises_is_recorded_without_addresses_and_the_pass_goes_on(self):
        update_settings(self.conn, {"bounce_recovery": True}, user_id=USER)
        worker = AutomationWorker(self.platform_path, fetcher_factory=lambda: None)
        with mock.patch("opportunity_app.outreach_automation.recovery_due",
                        side_effect=RuntimeError("could not search again for dana@bovi.test")), \
                self.assertLogs("opportunity_app.outreach_automation", "ERROR"):
            self.assertEqual(worker.run_once(), {"sent": [], "recovered": [], "drafted": [], "forms": []}, "the pass returns")
        row = self.worker_health()[USER]
        self.assertEqual(row["last_error"], "RuntimeError: could not search again for [address]")
        self.assertIsNone(row["last_ok_at"])
        worker.run_once()
        self.assertIsNotNone(self.worker_health()[USER]["last_ok_at"], "the next good pass says so")

    def test_a_student_with_an_email_due_is_recorded_when_the_send_step_fails(self):
        target = create_target(self.conn, {"company": "Bovi", "contact_email": "greg@bovi.test"}, user_id=USER)
        stamp = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(timespec="seconds")
        with self.conn:
            self.conn.execute(
                """INSERT INTO outreach_scheduled_sends(target_id, user_id, kind, fingerprint, send_at, timezone, label, state, created_at, updated_at)
                   VALUES(?, ?, 'initial', 'f', ?, 'UTC', 'now', 'scheduled', ?, ?)""",
                (target["id"], USER, stamp, stamp, stamp),
            )
        worker = AutomationWorker(self.platform_path, fetcher_factory=lambda: None, gmail_client_factory=lambda: None)
        with mock.patch("opportunity_app.outreach_schedule.run_due_sends",
                        side_effect=RuntimeError("the send path broke for greg@bovi.test")), \
                self.assertLogs("opportunity_app.outreach_automation", "ERROR"):
            worker.run_once()
        self.assertEqual(self.worker_health()[USER]["last_error"], "RuntimeError: the send path broke for [address]",
                         "every switch is off, but an email was due, so the failure shows")


class AutomationApiTests(unittest.TestCase):
    def test_settings_round_trip(self):
        with tempfile.TemporaryDirectory() as root:
            _, platform_path = build_and_migrate(Path(root))
            app = create_app(db_path=platform_path, access_token="automation-owner", static_dir=STATIC_DIR)
            with TestClient(app) as client:
                self.assertEqual(client.get("/api/v1/outreach/automation", headers=AUTH).json(),
                                 {"auto_drafts": False, "bounce_recovery": False, "bounce_auto_resend": False, "scheduled_sending": False, "follow_up_review": False, "form_submission": False})
                saved = client.put("/api/v1/outreach/automation", headers=AUTH, json={"auto_drafts": True})
                self.assertEqual(saved.status_code, 200, saved.text)
                self.assertEqual(saved.json(), {"auto_drafts": True, "bounce_recovery": False, "bounce_auto_resend": False, "scheduled_sending": False, "follow_up_review": False, "form_submission": False})
                self.assertEqual(client.get("/api/v1/outreach/automation").status_code, 401)


if __name__ == "__main__":
    unittest.main()
