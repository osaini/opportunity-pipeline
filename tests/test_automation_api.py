"""The automation API: switches and the pause, the ledger's decisions, notices, and health, over HTTP."""

import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi.testclient import TestClient

from opportunity_app import STATIC_DIR, automation
from opportunity_app.actions import update_application
from opportunity_app.api import create_app
from opportunity_app.automation import OFF_SHADOW_ON, Feature
from opportunity_app.database import connect_product
from opportunity_app.timestamps import utc_now

from helpers_platform import build_and_migrate

USER = "local-user"
AUTH = {"Authorization": "Bearer automation-api-owner"}
SWITCH = Feature("test_api_switch", "Test API switch", "Moves an application on its own", "applications", "internal")
SHADOWED = Feature("test_api_shadow", "Test API shadow", "Moves an application on its own, after a trial in shadow",
                   "applications", "internal", OFF_SHADOW_ON)


class AutomationApiCase(unittest.TestCase):
    """A throwaway database, the app over it, and a test-only feature of each kind."""

    def setUp(self):
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        _, self.platform_path = build_and_migrate(Path(tempdir.name))
        for feature in (SWITCH, SHADOWED):
            automation.register(feature)
            self.addCleanup(automation.FEATURES.pop, feature.key, None)
        app = create_app(db_path=self.platform_path, access_token="automation-api-owner", static_dir=STATIC_DIR)
        self.client = self.enterContext(TestClient(app))
        self.conn = connect_product(self.platform_path)
        self.addCleanup(self.conn.close)

    def get(self, path, **kwargs):
        return self.client.get(path, headers=AUTH, **kwargs)

    def put(self, path, body):
        return self.client.put(path, headers=AUTH, json=body)

    def post(self, path, body=None):
        return self.client.post(path, headers=AUTH, json=body)

    def mode(self, key):
        return automation.mode(self.conn, USER, key)

    def act(self, *, feature=SWITCH.key, after=None, auto=True, subject_id="app-job-b"):
        """One automatic stage change on the seeded Orbit application (stage 'applied')."""
        return automation.perform(
            self.conn, user_id=USER, feature=feature, action_type="application.stage", subject_kind="application",
            subject_id=subject_id, after=after or {"stage": "interview"}, evidence={"subject": "Interview invitation"},
            summary="Moved Orbit Systems to interview", basis="rule:test", confidence=0.9,
            idempotency_key=f"test:{uuid4().hex}", auto=auto,
        )

    def stage(self, application_id="app-job-b"):
        return self.conn.execute("SELECT stage FROM applications WHERE id=?", (application_id,)).fetchone()[0]

    def feature(self, payload, key):
        return next(item for item in payload["settings"]["features"] if item["key"] == key)


class SettingsTests(AutomationApiCase):
    def test_the_overview_has_the_switches_the_health_and_unread_notices(self):
        automation.notice(self.conn, USER, event_key="test:1", level="warning", title="Gmail needs reconnecting")
        response = self.get("/api/v1/automation")
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        # application_mail: the job-email switch's own state (application_inbox.status), for its panel line.
        self.assertEqual(set(payload), {"settings", "health", "notices", "application_mail"})
        self.assertEqual(payload["settings"]["paused"], False)
        feature = self.feature(payload, "auto_drafts")
        self.assertEqual(set(feature), {"key", "label", "description", "group", "risk", "modes", "mode", "shadow_since",
                                        "can_turn_on", "can_turn_on_reason", "requirement"})
        self.assertEqual(feature["requirement"], "", "nothing beyond the switch is needed")
        self.assertEqual((feature["mode"], feature["modes"], feature["group"]), ("off", ["off", "on"], "outreach"))
        self.assertFalse(self.feature(payload, SHADOWED.key)["can_turn_on"])
        self.assertIn("shadow first", self.feature(payload, SHADOWED.key)["can_turn_on_reason"])
        self.assertEqual(payload["health"]["banner"], [])
        self.assertEqual(payload["health"]["unread_notices"], 1)
        self.assertEqual(payload["health"]["counts"], {"proposed": 0, "shadow_unreviewed": 0, "applied_last_24h": 0})
        self.assertEqual((payload["health"]["unconfirmed"], payload["health"]["breaker_off"]), ([], []))
        self.assertEqual([notice["title"] for notice in payload["notices"]], ["Gmail needs reconnecting"])

    def test_modes_are_switched(self):
        response = self.put("/api/v1/automation/settings", {"modes": {"auto_drafts": "on", SHADOWED.key: "shadow"}})
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(set(payload), {"settings", "health", "application_mail"}, "in_flight is only reported for a pause")
        self.assertEqual(self.feature(payload, "auto_drafts")["mode"], "on")
        self.assertEqual(self.feature(payload, SHADOWED.key)["mode"], "shadow")
        self.assertTrue(self.feature(payload, SHADOWED.key)["shadow_since"])
        self.assertEqual((self.mode("auto_drafts"), self.mode(SHADOWED.key)), ("on", "shadow"))
        self.assertEqual(self.put("/api/v1/automation/settings", {"modes": {"auto_drafts": "off"}}).status_code, 200)
        self.assertEqual(self.mode("auto_drafts"), "off")

    def test_an_unknown_feature_or_mode_is_refused_and_nothing_changes(self):
        unknown = self.put("/api/v1/automation/settings", {"modes": {"auto_drafts": "on", "no_such_feature": "on"}})
        self.assertEqual(unknown.status_code, 422, unknown.text)
        self.assertIn("no_such_feature", unknown.json()["detail"])
        self.assertEqual(self.mode("auto_drafts"), "off", "a refused request changes no switch")
        self.assertEqual(self.put("/api/v1/automation/settings", {"modes": {"auto_drafts": "sometimes"}}).status_code, 422)
        shadow = self.put("/api/v1/automation/settings", {"modes": {"auto_drafts": "shadow"}, "paused": True})
        self.assertEqual(shadow.status_code, 422, "a two-mode switch has no shadow")
        self.assertFalse(automation.paused(self.conn, USER), "nor does it pause")
        self.assertEqual(self.put("/api/v1/automation/settings", {"modes": {"auto_drafts": "on"}, "extra": 1}).status_code, 422)
        too_many = {"modes": {f"feature_{n}": "off" for n in range(51)}}
        self.assertEqual(self.put("/api/v1/automation/settings", too_many).status_code, 422)

    def test_a_shadow_feature_that_has_not_earned_on_is_a_conflict(self):
        response = self.put("/api/v1/automation/settings", {"modes": {SHADOWED.key: "on"}, "paused": True})
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn("shadow first", response.json()["detail"])
        self.assertEqual(self.mode(SHADOWED.key), "off")
        self.assertFalse(automation.paused(self.conn, USER), "the pause waits for every switch to be allowed")
        self.put("/api/v1/automation/settings", {"modes": {SHADOWED.key: "shadow"}})
        still = self.put("/api/v1/automation/settings", {"modes": {SHADOWED.key: "on"}})
        self.assertEqual(still.status_code, 409)
        self.assertIn("48 hours", still.json()["detail"])
        self.assertEqual(self.mode(SHADOWED.key), "shadow")

    def test_pausing_reports_what_is_already_on_its_way(self):
        now = utc_now()
        with self.conn:
            self.conn.execute(
                "INSERT INTO outreach_targets(id, user_id, company, created_at, updated_at) VALUES('t-1', ?, 'Bovi Robotics', ?, ?)",
                (USER, now, now),
            )
            self.conn.execute(
                "INSERT INTO outreach_scheduled_sends(target_id, user_id, kind, fingerprint, send_at, timezone, label, state, created_at, updated_at) "
                "VALUES('t-1', ?, 'initial', 'f', ?, 'UTC', 'Mon, Sep 28, 9:12 AM CDT', 'transmitting', ?, ?)",
                (USER, now, now, now),
            )
        response = self.put("/api/v1/automation/settings", {"paused": True})
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertTrue(payload["settings"]["paused"])
        [flight] = payload["in_flight"]
        self.assertEqual((flight["source"], flight["company"], flight["action"], flight["at"]), ("scheduled_send", "Bovi Robotics", "send", now))
        self.assertEqual([item["key"] for item in payload["health"]["banner"]], ["paused"])
        self.assertTrue(automation.paused(self.conn, USER))
        resumed = self.put("/api/v1/automation/settings", {"paused": False}).json()
        self.assertFalse(resumed["settings"]["paused"])
        self.assertIn("in_flight", resumed)
        self.assertEqual(resumed["health"]["banner"], [])

    def test_the_legacy_outreach_route_still_reads_and_writes_the_same_switches(self):
        legacy = self.client.put("/api/v1/outreach/automation", headers=AUTH, json={"auto_drafts": True})
        self.assertEqual(legacy.status_code, 200, legacy.text)
        self.assertEqual(legacy.json(), {"auto_drafts": True, "bounce_recovery": False, "bounce_auto_resend": False, "scheduled_sending": False,
                                         "follow_up_review": False, "form_submission": False})
        self.assertEqual(self.feature(self.get("/api/v1/automation").json(), "auto_drafts")["mode"], "on")
        self.put("/api/v1/automation/settings", {"modes": {"auto_drafts": "off", "bounce_recovery": "on"}})
        self.assertEqual(self.get("/api/v1/outreach/automation").json(), {
            "auto_drafts": False, "bounce_recovery": True, "bounce_auto_resend": False, "scheduled_sending": False, "follow_up_review": False, "form_submission": False,
        })

    def test_every_route_needs_a_signed_in_student(self):
        for method, path, body in (
            ("GET", "/api/v1/automation", None),
            ("PUT", "/api/v1/automation/settings", {"paused": True}),
            ("GET", "/api/v1/automation/actions", None),
            ("GET", "/api/v1/automation/actions/auto-x", None),
            ("POST", "/api/v1/automation/actions/auto-x/undo", None),
            ("POST", "/api/v1/automation/actions/auto-x/approve", None),
            ("POST", "/api/v1/automation/actions/auto-x/reject", None),
            ("POST", "/api/v1/automation/actions/auto-x/review", {"verdict": "right"}),
            ("POST", "/api/v1/automation/notices/read", {"ids": []}),
        ):
            with self.subTest(method=method, path=path):
                self.assertEqual(self.client.request(method, path, json=body).status_code, 401)
        self.assertFalse(automation.paused(self.conn, USER))


class ActionTests(AutomationApiCase):
    def setUp(self):
        super().setUp()
        automation.set_mode(self.conn, USER, SWITCH.key, "on")

    def test_the_list_filters_by_status_and_feature_and_counts_every_match(self):
        applied = self.act()
        proposed = self.act(after={"stage": "offer"}, auto=False)
        automation.set_mode(self.conn, USER, SHADOWED.key, "shadow")
        shadow = self.act(feature=SHADOWED.key, after={"stage": "rejected"})
        everything = self.get("/api/v1/automation/actions").json()
        self.assertEqual([item["id"] for item in everything["items"]], [shadow["id"], proposed["id"], applied["id"]])
        self.assertEqual(everything["total"], 3)
        self.assertEqual(everything["items"][0]["evidence"], {"subject": "Interview invitation"})
        waiting = self.get("/api/v1/automation/actions", params={"status": "proposed"}).json()
        self.assertEqual(([item["id"] for item in waiting["items"]], waiting["total"]), ([proposed["id"]], 1))
        by_feature = self.get("/api/v1/automation/actions", params={"feature": SHADOWED.key}).json()
        self.assertEqual([item["status"] for item in by_feature["items"]], ["shadow"])
        first = self.get("/api/v1/automation/actions", params={"limit": 1}).json()
        self.assertEqual((len(first["items"]), first["total"]), (1, 3))
        several = self.get("/api/v1/automation/actions", params={"status": "applied,shadow,undone"}).json()
        self.assertEqual(([item["id"] for item in several["items"]], several["total"]), ([shadow["id"], applied["id"]], 2),
                         "several statuses, comma-separated, each list fetched on its own")
        one_of_two = self.get("/api/v1/automation/actions", params={"status": "applied,shadow", "limit": 1}).json()
        self.assertEqual((len(one_of_two["items"]), one_of_two["total"]), (1, 2), "the total counts past the limit")
        for params in ({"status": "done"}, {"status": "applied,done"}, {"status": ","}, {"status": ""},
                       {"limit": 0}, {"limit": 201}, {"feature": "x" * 65}):
            with self.subTest(params=params):
                self.assertEqual(self.get("/api/v1/automation/actions", params=params).status_code, 422)

    def test_one_action_is_read_by_id(self):
        row = self.act()
        response = self.get(f"/api/v1/automation/actions/{row['id']}")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual((response.json()["id"], response.json()["status"]), (row["id"], "applied"))
        self.assertEqual(self.get("/api/v1/automation/actions/auto-missing").status_code, 404)

    def test_undo(self):
        row = self.act()
        self.assertEqual(self.stage(), "interview")
        response = self.post(f"/api/v1/automation/actions/{row['id']}/undo")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual((response.json()["status"], response.json()["feature_paused"]), ("undone", False))
        self.assertEqual(self.stage(), "applied")
        again = self.post(f"/api/v1/automation/actions/{row['id']}/undo")
        self.assertEqual(again.status_code, 409)
        self.assertIn("this one is undone", again.json()["detail"])
        self.assertEqual(self.post("/api/v1/automation/actions/auto-missing/undo").status_code, 404)

    def test_an_undo_after_the_student_changed_the_stage_is_superseded(self):
        row = self.act()
        update_application(self.conn, "app-job-b", stage="offer", user_id=USER)
        response = self.post(f"/api/v1/automation/actions/{row['id']}/undo")
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn("The stage changed since", response.json()["detail"])
        self.assertEqual(self.stage(), "offer")
        self.assertEqual(self.get(f"/api/v1/automation/actions/{row['id']}").json()["status"], "superseded")

    def test_two_undos_turn_the_feature_off_and_say_so(self):
        rows = [self.act(after={"stage": stage}) for stage in ("interview", "offer")]
        self.post(f"/api/v1/automation/actions/{rows[1]['id']}/undo")
        second = self.post(f"/api/v1/automation/actions/{rows[0]['id']}/undo").json()
        self.assertTrue(second["feature_paused"])
        self.assertEqual(self.mode(SWITCH.key), "off")
        overview = self.get("/api/v1/automation").json()
        self.assertEqual(self.feature(overview, SWITCH.key)["mode"], "off")
        self.assertIn("Turned off Test API switch", overview["notices"][0]["title"])
        self.assertEqual(second["breaker_notice"], {"title": overview["notices"][0]["title"], "body": overview["notices"][0]["body"]})
        self.assertEqual([item["feature"] for item in overview["health"]["breaker_off"]], [SWITCH.key])

    def test_approve(self):
        row = self.act(auto=False)
        self.assertEqual(self.stage(), "applied", "a proposal changes nothing")
        self.assertEqual(self.get("/api/v1/automation").json()["health"]["counts"]["proposed"], 1)
        response = self.post(f"/api/v1/automation/actions/{row['id']}/approve")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual((response.json()["status"], response.json()["decided_by"]), ("applied", "student"))
        self.assertEqual(self.stage(), "interview")
        again = self.post(f"/api/v1/automation/actions/{row['id']}/approve")
        self.assertEqual(again.status_code, 409)
        self.assertIn("this one is applied", again.json()["detail"])
        self.assertEqual(self.post("/api/v1/automation/actions/auto-missing/approve").status_code, 404)

    def test_an_approval_after_the_stage_changed_is_superseded(self):
        row = self.act(auto=False)
        update_application(self.conn, "app-job-b", stage="offer", user_id=USER)
        response = self.post(f"/api/v1/automation/actions/{row['id']}/approve")
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn("after this was proposed", response.json()["detail"])
        self.assertEqual(self.stage(), "offer")
        self.assertEqual(self.get(f"/api/v1/automation/actions/{row['id']}").json()["status"], "superseded")

    def test_reject(self):
        row = self.act(auto=False)
        response = self.post(f"/api/v1/automation/actions/{row['id']}/reject")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual((response.json()["status"], response.json()["feature_paused"]), ("rejected", False))
        self.assertEqual(self.stage(), "applied")
        again = self.post(f"/api/v1/automation/actions/{row['id']}/reject")
        self.assertEqual(again.status_code, 409)
        self.assertIn("this one is rejected", again.json()["detail"])
        self.assertEqual(self.post("/api/v1/automation/actions/auto-missing/reject").status_code, 404)

    def test_a_superseded_proposal_can_no_longer_be_rejected(self):
        # Rejecting never checks the fields, so it is never superseded itself; a proposal
        # an approval found superseded is refused like any other decided action.
        row = self.act(auto=False)
        update_application(self.conn, "app-job-b", stage="offer", user_id=USER)
        self.assertEqual(self.post(f"/api/v1/automation/actions/{row['id']}/approve").status_code, 409)
        response = self.post(f"/api/v1/automation/actions/{row['id']}/reject")
        self.assertEqual(response.status_code, 409)
        self.assertIn("this one is superseded", response.json()["detail"])

    def test_two_rejects_turn_the_feature_off(self):
        rows = [self.act(after={"stage": stage}, auto=False) for stage in ("interview", "offer")]
        first = self.post(f"/api/v1/automation/actions/{rows[0]['id']}/reject").json()
        self.assertEqual((first["feature_paused"], first["breaker_notice"]), (False, None))
        second = self.post(f"/api/v1/automation/actions/{rows[1]['id']}/reject").json()
        self.assertTrue(second["feature_paused"])
        self.assertEqual(second["breaker_notice"]["title"], "Turned off Test API switch: you undid or rejected 2 of its last 2 actions")
        self.assertEqual(self.mode(SWITCH.key), "off")

    def test_review(self):
        automation.set_mode(self.conn, USER, SHADOWED.key, "shadow")
        row = self.act(feature=SHADOWED.key)
        self.assertEqual(self.stage(), "applied", "shadow changes nothing")
        self.assertEqual(self.get("/api/v1/automation").json()["health"]["counts"]["shadow_unreviewed"], 1)
        response = self.post(f"/api/v1/automation/actions/{row['id']}/review", {"verdict": "wrong"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual((response.json()["status"], response.json()["review"]), ("shadow", "wrong"))
        self.assertEqual(self.get("/api/v1/automation").json()["health"]["counts"]["shadow_unreviewed"], 0)
        self.assertEqual(self.post(f"/api/v1/automation/actions/{row['id']}/review", {"verdict": "maybe"}).status_code, 422)
        applied = self.act()
        not_shadow = self.post(f"/api/v1/automation/actions/{applied['id']}/review", {"verdict": "right"})
        self.assertEqual(not_shadow.status_code, 409)
        self.assertIn("Only a shadow action", not_shadow.json()["detail"])
        self.assertEqual(self.post("/api/v1/automation/actions/auto-missing/review", {"verdict": "right"}).status_code, 404)


class NoticeTests(AutomationApiCase):
    def test_notices_are_marked_read(self):
        for n in range(2):
            automation.notice(self.conn, USER, event_key=f"test:{n}", level="info", title=f"Notice {n}")
        notices = self.get("/api/v1/automation").json()["notices"]
        self.assertEqual(len(notices), 2)
        response = self.post("/api/v1/automation/notices/read", {"ids": [notices[0]["id"], "notice-missing"]})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"marked": 1})
        overview = self.get("/api/v1/automation").json()
        self.assertEqual([notice["id"] for notice in overview["notices"]], [notices[1]["id"]], "only unread notices are listed")
        self.assertEqual(overview["health"]["unread_notices"], 1)
        self.assertEqual(self.post("/api/v1/automation/notices/read", {"ids": [f"n-{n}" for n in range(101)]}).status_code, 422)
        self.assertEqual(self.post("/api/v1/automation/notices/read", {}).status_code, 422)
        with closing(connect_product(self.platform_path)) as other:
            self.assertEqual(other.execute("SELECT COUNT(*) FROM automation_notices WHERE read_at IS NULL").fetchone()[0], 1)

    def test_mark_all_read_marks_every_unread_notice_not_only_the_page(self):
        for n in range(25):
            automation.notice(self.conn, USER, event_key=f"many:{n}", level="info", title=f"Notice {n}")
        overview = self.get("/api/v1/automation").json()
        self.assertEqual((len(overview["notices"]), overview["health"]["unread_notices"]), (20, 25))
        for body in ({"all": False}, {"ids": [overview["notices"][0]["id"]], "all": True}, {"all": "maybe"}):
            with self.subTest(body=body):
                self.assertEqual(self.post("/api/v1/automation/notices/read", body).status_code, 422)
        response = self.post("/api/v1/automation/notices/read", {"all": True})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"marked": 25})
        after = self.get("/api/v1/automation").json()
        self.assertEqual((after["notices"], after["health"]["unread_notices"]), ([], 0))


if __name__ == "__main__":
    unittest.main()
