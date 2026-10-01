"""Urgent's "possible reply" rows come from the targets already loaded, in the same order and with the same content.

_outreach_rows used to run one SELECT per waiting company after loading the open targets. It now reuses the rows it
loaded and reads only the closed ones the first query left out, in one query per 500. The reference here is that old
per-company lookup, written out independently.
"""

import json
import random
import re
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from opportunity_app import urgent
from opportunity_app.schema import connect_product, utc_now

from helpers_platform import build_and_migrate

USER = "local-user"
OTHER = "someone-else"
NOW = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
STATUSES = ["not_started", "drafted", "sent", "followed_up", "replied", "declined", "no_response", "paused", "offer", "call_scheduled"]


def old_possible_reply_rows(conn, user_id):
    """The rows the old tail of _outreach_rows produced: one lookup per waiting company, in waiting order."""
    waiting = {}
    for message in conn.execute(
        "SELECT target_id, candidates_json, received_at FROM outreach_inbox_messages WHERE user_id=? AND kind='possible' "
        "ORDER BY received_at", (user_id,),
    ).fetchall():
        try:
            others = [str(other) for other in json.loads(message["candidates_json"] or "[]")]
        except (TypeError, ValueError):
            others = []
        for owner in [str(message["target_id"]), *others]:
            waiting.setdefault(owner, str(message["received_at"] or ""))
    rows = []
    for target_id, received in waiting.items():
        target = conn.execute(
            "SELECT id, company, status FROM outreach_targets WHERE id=? AND user_id=? AND not_interested_at IS NULL", (target_id, user_id),
        ).fetchone()
        if target is not None:
            rows.append({
                "record_id": str(target["id"]), "date_only": False, "title": target["company"], "company": target["company"],
                "outreach_target_id": str(target["id"]), "stage": target["status"],
                "kind": "outreach_possible_reply", "raw_date": received,
            })
    return rows


class UrgentPossibleReplyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        _, platform_path = build_and_migrate(Path(self.tmp.name))
        self.conn = connect_product(platform_path)
        self.addCleanup(self.conn.close)

    def message(self, gmail_id, target_id, candidates, *, user=USER, kind="possible", received="2026-09-20T00:00:00+00:00"):
        self.conn.execute(
            "INSERT INTO outreach_inbox_messages(user_id, gmail_id, target_id, kind, sender, received_at, recorded_at, candidates_json) "
            "VALUES(?, ?, ?, ?, 'x', ?, ?, ?)",
            (user, gmail_id, target_id, kind, received, utc_now(), candidates if isinstance(candidates, str) else json.dumps(candidates)),
        )

    def target(self, target_id, status="sent", *, user=USER, aside=False, follow_up=None, deadline=None):
        stamp = utc_now()
        self.conn.execute(
            "INSERT INTO outreach_targets(id, user_id, company, status, follow_up_at, deadline_date, created_at, updated_at, not_interested_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (target_id, user, f"Company {target_id}", status, follow_up, deadline, stamp, stamp, stamp if aside else None),
        )

    def seed(self, targets, messages, seed):
        rng = random.Random(seed)
        stamp = utc_now()
        with self.conn:
            self.conn.execute("INSERT OR IGNORE INTO users(id, created_at, updated_at) VALUES(?, ?, ?)", (OTHER, stamp, stamp))
            ids = [f"outreach-{index:03d}" for index in range(targets)]
            for target_id in ids:
                self.target(target_id, rng.choice(STATUSES), user=OTHER if rng.random() < 0.1 else USER, aside=rng.random() < 0.1,
                            follow_up="2026-09-01" if rng.random() < 0.5 else None, deadline="2026-10-05" if rng.random() < 0.2 else None)
            for index in range(messages):
                candidates = rng.sample(ids, rng.randint(0, 3)) + (["ghost"] if rng.random() < 0.1 else [])
                raw = candidates if rng.random() < 0.95 else rng.choice(["not json", "", "null", '{"a": 1}'])
                self.message(f"m{index}", rng.choice(ids), raw, user=USER if rng.random() < 0.9 else OTHER,
                             kind="possible" if rng.random() < 0.85 else "reply",
                             received=(NOW - timedelta(hours=rng.randint(0, 900))).isoformat())

    def possible_rows(self):
        return [row for row in urgent._outreach_rows(self.conn, USER) if row["kind"] == "outreach_possible_reply"]

    def test_the_rows_are_what_a_lookup_per_company_gave(self):
        for seed in range(6):
            with self.subTest(seed=seed):
                self.setUp()
                self.seed(targets=60, messages=40, seed=seed)
                got = self.possible_rows()
                self.assertTrue(got, "the data has waiting companies")
                self.assertEqual(got, old_possible_reply_rows(self.conn, USER))

    def test_a_closed_company_with_an_email_waiting_still_shows(self):
        with self.conn:
            for target_id, status in (("t-open", "sent"), ("t-closed", "declined"), ("t-offer", "offer")):
                self.target(target_id, status)
            self.message("m1", "t-closed", ["t-open", "t-offer"])
        rows = self.possible_rows()
        self.assertEqual([row["outreach_target_id"] for row in rows], ["t-closed", "t-open", "t-offer"])
        self.assertEqual(rows, old_possible_reply_rows(self.conn, USER))

    def test_a_company_set_aside_or_another_students_never_shows(self):
        with self.conn:
            self.conn.execute("INSERT OR IGNORE INTO users(id, created_at, updated_at) VALUES(?, ?, ?)", (OTHER, utc_now(), utc_now()))
            self.target("t-aside", "sent", aside=True)
            self.target("t-theirs", "declined", user=OTHER)
            self.message("m1", "t-aside", ["t-theirs"])
        self.assertEqual(self.possible_rows(), [])

    def test_a_long_list_of_closed_companies_is_read_in_chunks(self):
        ids = [f"closed-{index:04d}" for index in range(1_300)]
        with self.conn:
            for target_id in ids:
                self.target(target_id, "declined")
            self.message("m1", ids[0], ids[1:])
        self.assertEqual([row["outreach_target_id"] for row in self.possible_rows()], ids)

    def test_it_runs_no_lookup_per_company(self):
        self.seed(targets=60, messages=40, seed=1)
        seen = []
        self.conn.set_trace_callback(seen.append)
        try:
            urgent._outreach_rows(self.conn, USER)
        finally:
            self.conn.set_trace_callback(None)
        self.assertEqual([text for text in seen if re.search(r"FROM outreach_targets WHERE id=", text)], [])
        self.assertLessEqual(len([text for text in seen if "FROM outreach_targets" in text]), 2)


if __name__ == "__main__":
    unittest.main()
