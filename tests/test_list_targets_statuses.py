"""list_targets(statuses=...) and the two automation scans that use it.

recovery_due and draft_due keep only companies in not_started or drafted, and used to build a full record for every
other company first. They now ask list_targets for just those statuses; every Python check stays. The reference is each
scan with the new argument stripped, which is what ran before.
"""

import json
import random
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from opportunity_app import outreach_automation
from opportunity_app.outreach import list_targets
from opportunity_app.schema import connect_product, utc_now

from helpers_platform import build_and_migrate

USER = "local-user"
NOW = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
STATUSES = ["not_started", "drafted", "sent", "followed_up", "replied", "declined", "no_response", "paused", "offer", "call_scheduled"]


def without_statuses(real):
    def call(conn, **kwargs):
        kwargs.pop("statuses", None)
        return real(conn, **kwargs)

    return call


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        _, platform_path = build_and_migrate(Path(self.tmp.name))
        self.conn = connect_product(platform_path)
        self.addCleanup(self.conn.close)

    def seed(self, count, seed):
        rng = random.Random(seed)
        stamp = utc_now()
        with self.conn:
            for index in range(count):
                target_id = f"outreach-{index:03d}"
                status = rng.choice(STATUSES)
                email = f"hi{index}@example{index}.com" if rng.random() < 0.9 else ""
                bounced = json.dumps([email]) if email and rng.random() < 0.3 else "[]"
                self.conn.execute(
                    "INSERT INTO outreach_targets(id, user_id, company, status, contact_email, contact_cc, bounced_addresses_json, email_body, "
                    "sent_at, location, location_basis, not_interested_at, created_at, updated_at) VALUES(?, ?, ?, ?, ?, '', ?, ?, ?, ?, ?, ?, ?, ?)",
                    (target_id, USER, f"Company {index}", status, email, bounced, "body" if rng.random() < 0.3 else "",
                     stamp if rng.random() < 0.2 else None, "Austin, TX", rng.choice(["manual", ""]),
                     stamp if rng.random() < 0.1 else None, stamp, stamp),
                )
                if bounced != "[]":
                    self.conn.execute(
                        "INSERT INTO outreach_events(id, user_id, target_id, event_type, created_at) VALUES(?, ?, ?, 'bounced', ?)",
                        (f"bounce-{index}", USER, target_id, stamp),
                    )
                    if rng.random() < 0.3:
                        self.conn.execute(
                            "INSERT INTO outreach_events(id, user_id, target_id, event_type, created_at) VALUES(?, ?, ?, 'contact_recovery', ?)",
                            (f"tried-{index}", USER, target_id, "9999-01-01T00:00:00+00:00"),
                        )
                if rng.random() < 0.1:
                    self.conn.execute(
                        "INSERT INTO outreach_events(id, user_id, target_id, event_type, created_at) VALUES(?, ?, ?, 'reply_logged', ?)",
                        (f"reply-{index}", USER, target_id, stamp),
                    )


class ListTargetsStatusesTests(Case):
    def test_the_filter_keeps_those_statuses_in_the_same_order_with_the_same_records(self):
        self.seed(80, seed=1)
        everything = list_targets(self.conn, user_id=USER, interested_only=True)
        for wanted in (("not_started", "drafted"), ("sent",), ("declined", "offer", "paused"), ("not_a_status",)):
            with self.subTest(wanted=wanted):
                got = list_targets(self.conn, user_id=USER, interested_only=True, statuses=wanted)
                self.assertEqual(got, [item for item in everything if item["status"] in wanted])
        self.assertTrue(list_targets(self.conn, user_id=USER, interested_only=True, statuses=("not_started", "drafted")))

    def test_no_statuses_means_every_target(self):
        self.seed(30, seed=2)
        self.assertEqual(list_targets(self.conn, user_id=USER), list_targets(self.conn, user_id=USER, statuses=()))

    def test_it_combines_with_the_other_filters(self):
        self.seed(60, seed=3)
        got = list_targets(self.conn, user_id=USER, status="sent", statuses=("sent", "drafted"))
        self.assertEqual(got, list_targets(self.conn, user_id=USER, status="sent"))
        self.assertEqual(list_targets(self.conn, user_id=USER, status="sent", statuses=("drafted",)), [])


class AutomationScanTests(Case):
    def test_recovery_and_draft_scans_find_what_they_found_before(self):
        real = outreach_automation.list_targets
        found = {"recovery": 0, "drafts": 0}
        for seed in range(4):
            with self.subTest(seed=seed):
                self.setUp()
                self.seed(120, seed=seed)
                recovery = outreach_automation.recovery_due(self.conn, user_id=USER)
                drafts = outreach_automation.draft_due(self.conn, user_id=USER, now=NOW)
                with mock.patch.object(outreach_automation, "list_targets", without_statuses(real)):
                    self.assertEqual(recovery, outreach_automation.recovery_due(self.conn, user_id=USER))
                    self.assertEqual(drafts, outreach_automation.draft_due(self.conn, user_id=USER, now=NOW))
                found["recovery"] += len(recovery)
                found["drafts"] += len(drafts)
        self.assertGreater(found["recovery"], 0, "the data has bounced companies to recover")
        self.assertGreater(found["drafts"], 0, "the data has companies ready for a draft")

    def test_both_scans_ask_for_only_the_two_statuses(self):
        self.seed(20, seed=1)
        real = outreach_automation.list_targets
        with mock.patch.object(outreach_automation, "list_targets", wraps=real) as spy:
            outreach_automation.recovery_due(self.conn, user_id=USER)
            outreach_automation.draft_due(self.conn, user_id=USER, now=NOW)
        self.assertEqual([call.kwargs["statuses"] for call in spy.call_args_list], [("not_started", "drafted")] * 2)


if __name__ == "__main__":
    unittest.main()
