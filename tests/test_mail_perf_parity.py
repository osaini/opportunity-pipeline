"""Phase 2 workstream M: the mail-path optimisations answer exactly what the code they replaced did.

Each class keeps a copy of the pre-optimisation function (marked REFERENCE) and compares it with the live one on the
same generated data (tests/helpers_mail_perf.py). The references are frozen on purpose: when the live code changes
on purpose, change the behaviour test beside it, not the reference.
"""

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from opportunity_app import application_inbox, mail_trust, outreach_inbox
from opportunity_app.mail_message import host_of
from opportunity_app import outreach_gmail_sends as sends
from opportunity_app.outreach import DRAFT_KINDS, UNSENT_STATUSES, get_target
from opportunity_app.outreach_gmail import DRAFT_EVENT, _already_sent, last_bounce
from opportunity_app.schema import connect_product

import helpers_platform
from helpers_mail_perf import USER, populate_outreach


def database(test, n, seed, now):
    tempdir = tempfile.TemporaryDirectory()
    test.addCleanup(tempdir.cleanup)
    _, path = helpers_platform.build_and_migrate(Path(tempdir.name))
    conn = connect_product(path)
    test.addCleanup(conn.close)
    ids = populate_outreach(conn, n, seed, now)
    return conn, ids


def reference_pending(conn, user_id, now):
    """REFERENCE: outreach_gmail_sends._pending as it was before gmail-4 (a bounce query and a full get_target per draft)."""
    cutoff = (now - sends.WATCH_DRAFTS_FOR).isoformat(timespec="microseconds")
    rows = conn.execute(
        """
        SELECT e.target_id, e.detail, e.created_at FROM outreach_events e
        JOIN outreach_targets t ON t.id=e.target_id AND t.user_id=e.user_id
        WHERE e.user_id=? AND e.event_type=? AND e.created_at>=? ORDER BY e.created_at
        """,
        (user_id, DRAFT_EVENT, cutoff),
    ).fetchall()
    newest = {}
    for row in rows:
        try:
            detail = json.loads(row["detail"])
        except (TypeError, ValueError):
            continue
        if not isinstance(detail, dict) or detail.get("kind") not in DRAFT_KINDS or not detail.get("draft_id"):
            continue
        made = datetime.fromisoformat(row["created_at"])
        bounce = last_bounce(conn, row["target_id"], user_id)
        if bounce is not None and bounce > made:
            continue
        newest[(row["target_id"], detail["kind"])] = {"target_id": row["target_id"], "made": made, "detail": detail}
    pending = []
    for (target_id, kind), item in newest.items():
        if _already_sent(conn, target_id, user_id, kind):
            continue
        target = get_target(conn, target_id, user_id=user_id)
        waiting = (
            not target["sent_at"] and target["status"] in UNSENT_STATUSES if kind == "initial" else target["status"] == "sent"
        )
        if waiting:
            pending.append({**item, "target": target})
    return sorted(pending, key=lambda item: item["made"])


class PendingDraftsParityTests(unittest.TestCase):
    def test_pending_with_targets_equals_the_reference_on_generated_histories(self):
        for seed in (1, 2, 3):
            with self.subTest(seed=seed):
                now = datetime.now(timezone.utc)
                conn, _ids = database(self, 70, seed, now)
                expected = reference_pending(conn, USER, now)
                actual = sends._with_targets(conn, USER, sends._pending(conn, USER, now))
                kinds = {item["detail"]["kind"] for item in expected}
                self.assertEqual(kinds, {"initial", "follow_up"}, "the data must exercise both kinds or the comparison proves little")
                self.assertGreater(len(expected), 10)
                self.assertEqual(actual, expected)

    def test_a_later_clock_drops_the_same_old_drafts(self):
        now = datetime.now(timezone.utc)
        conn, _ids = database(self, 60, 9, now)
        for days in (0, 10, 29, 31, 60):
            later = now + timedelta(days=days)
            self.assertEqual(
                sends._with_targets(conn, USER, sends._pending(conn, USER, later)), reference_pending(conn, USER, later), days,
            )

    def test_the_same_after_status_changes_and_a_new_bounce(self):
        now = datetime.now(timezone.utc)
        conn, ids = database(self, 60, 5, now)
        pending = sends._pending(conn, USER, now)
        self.assertTrue(pending)
        with conn:
            # One waiting target is marked sent, one is bounced after its draft, one is deleted.
            conn.execute("UPDATE outreach_targets SET status='replied', sent_at='2026-09-02' WHERE id=?", (pending[0]["target_id"],))
            conn.execute(
                "INSERT INTO outreach_events(id, target_id, user_id, event_type, detail, detail_json, created_at) VALUES(?,?,?,?,?,?,?)",
                ("ev-late-bounce", pending[1]["target_id"], USER, "bounced", "{}", "{}",
                 (now + timedelta(minutes=1)).isoformat(timespec="microseconds")),
            )
        self.assertEqual(sends._with_targets(conn, USER, sends._pending(conn, USER, now)), reference_pending(conn, USER, now))
        self.assertNotIn(pending[0]["target_id"], [item["target_id"] for item in sends._pending(conn, USER, now)])
        self.assertNotIn(pending[1]["target_id"], [item["target_id"] for item in sends._pending(conn, USER, now)])

    def test_a_failed_full_target_read_forgets_the_drafts_it_stamped(self):
        now = datetime.now(timezone.utc)
        conn, _ids = database(self, 20, 5, now)
        sends._LAST_LOOK.clear()
        self.addCleanup(sends._LAST_LOOK.clear)
        self.assertTrue(sends._pending(conn, USER, now))
        with mock.patch.object(sends, "connector_row", return_value={"status": "connected"}),                 mock.patch.object(sends, "_with_targets", side_effect=ValueError("bad date")):
            with self.assertRaises(ValueError):
                sends.capture_gmail_sends(conn, user_id=USER, client_factory=lambda: None, now=now)
        self.assertEqual(sends._LAST_LOOK, {}, "a look that fails is forgotten")

    def test_pending_reads_no_full_target(self):
        now = datetime.now(timezone.utc)
        conn, _ids = database(self, 40, 4, now)
        with mock.patch.object(sends, "get_target", side_effect=AssertionError("a full target was read before the look-spacing gate")):
            self.assertTrue(sends._pending(conn, USER, now))
            # Nothing due (every draft was just looked at): no target is read at all.
            sends._LAST_LOOK.clear()
            sends._LOOKS.take_due(USER, sends._pending(conn, USER, now), now)
            self.assertEqual(sends.capture_gmail_sends(conn, user_id=USER, client_factory=lambda: None, now=now),
                             {"state": "ok", "sent": [], "scheduled": []})

    def test_only_due_drafts_are_read_in_full_and_a_deleted_target_is_forgotten(self):
        now = datetime.now(timezone.utc)
        conn, _ids = database(self, 40, 6, now)
        sends._LAST_LOOK.clear()
        self.addCleanup(sends._LAST_LOOK.clear)
        pending = sends._pending(conn, USER, now)
        self.assertGreater(len(pending), 3)
        due = sends._LOOKS.take_due(USER, pending, now)
        self.assertEqual(len(due), len(pending))
        gone = due[0]
        with conn:
            conn.execute("PRAGMA foreign_keys=OFF")
            conn.execute("DELETE FROM outreach_targets WHERE id=?", (gone["target_id"],))
        full = sends._with_targets(conn, USER, due)
        self.assertEqual([item["target_id"] for item in full], [item["target_id"] for item in due[1:]])
        self.assertNotIn((USER, str(gone["detail"]["draft_id"])), sends._LAST_LOOK, "the dropped draft is looked at again next check")
        self.assertIn((USER, str(due[1]["detail"]["draft_id"])), sends._LAST_LOOK)


class OwnMessageReuseTests(unittest.TestCase):
    """_scheduled reuses the draft's own message that _find_sent read a moment before."""

    class StubGmail:
        def __init__(self, labels):
            self.labels, self.paths = labels, []

        def request(self, method, path, params=None):
            self.paths.append(path)

            class Response:
                status_code = 200

                def __init__(self, body):
                    self.body = body

                def json(self):
                    return self.body

            if path == "/messages":
                return Response({"messages": []})
            return Response({"id": "m-1", "labelIds": self.labels, "internalDate": "0", "payload": {"headers": []}})

    def item(self):
        target = {"id": "t", "email_subject": "Hello there friend", "contact_email": "a@b.example"}
        return {"target_id": "t", "made": datetime.now(timezone.utc), "detail": {"kind": "initial", "draft_id": "r", "message_id": "m-1"}, "target": target}

    def test_one_read_of_the_own_message_serves_both_questions(self):
        gmail = self.StubGmail(["SCHEDULED"])
        item, seen = self.item(), {}
        self.assertIsNone(sends._find_sent(gmail, item, seen))
        self.assertTrue(sends._scheduled(gmail, item, seen))
        self.assertEqual([path for path in gmail.paths if path.startswith("/messages/")], ["/messages/m-1"])

    def test_without_what_was_seen_it_is_read_again_as_before(self):
        gmail = self.StubGmail(["SCHEDULED"])
        item = self.item()
        self.assertIsNone(sends._find_sent(gmail, item))
        self.assertTrue(sends._scheduled(gmail, item))
        self.assertEqual([path for path in gmail.paths if path.startswith("/messages/")], ["/messages/m-1", "/messages/m-1"])

    def test_a_draft_with_no_message_id_is_not_scheduled_either_way(self):
        gmail = self.StubGmail(["SCHEDULED"])
        item = self.item()
        item["detail"].pop("message_id")
        seen = {}
        self.assertIsNone(sends._find_sent(gmail, item, seen))
        self.assertFalse(sends._scheduled(gmail, item, seen))
        self.assertFalse(sends._scheduled(gmail, item))


def reference_refresh_suggestions(conn, user_id):
    """REFERENCE: mail_trust.refresh_suggestions as it was before gmail-6 (a posting scan and a suggest for every application)."""
    mt = mail_trust
    before = conn.execute("SELECT COUNT(*) FROM employer_domains WHERE user_id=?", (user_id,)).fetchone()[0]
    applications = conn.execute(
        """
        SELECT DISTINCT o.company, o.url FROM applications a JOIN opportunities o ON o.id=a.opportunity_id
        WHERE a.user_id=?
        """,
        (user_id,),
    ).fetchall()
    keys = {mt.company_key(row["company"]): row["company"] for row in applications}
    keys.pop("", None)
    with conn:
        for row in applications:
            host = host_of(row["url"])
            domain = mt.registrable_domain(host)
            key = mt.company_key(row["company"])
            if not domain or not key or mt.not_an_employer(domain) or mt._url_hosts_elsewhere(conn, domain, key):
                continue
            mt.suggest(conn, user_id, company=row["company"], host=domain, source="job_url",
                       evidence=f"The posting you applied to at {row['company']} is on {domain}.")
        for row in conn.execute(
            "SELECT company, website FROM outreach_targets WHERE user_id=? AND website IS NOT NULL AND website<>''", (user_id,),
        ).fetchall():
            key = mt.company_key(row["company"])
            if key not in keys:
                continue
            host = host_of(row["website"] if "//" in str(row["website"]) else f"https://{row['website']}")
            mt.suggest(conn, user_id, company=keys[key], host=host, source="outreach",
                       evidence=f"Your outreach record for {row['company']} lists the website {mt.registrable_domain(host) or host}.")
    after = conn.execute("SELECT COUNT(*) FROM employer_domains WHERE user_id=?", (user_id,)).fetchone()[0]
    return int(after) - int(before)


# (company, url) of what the student applied to.
APPLIED = [
    ("Acme Robotics", "https://careers.acme-robotics.com/jobs/1"),
    ("Acme Robotics", "https://jobs.acme-robotics.com/jobs/2"),  # a second posting on the same company's domain
    ("Orbit Systems", "https://orbit-systems.io/careers/3"),
    ("Orbit Systems", "https://boards.greenhouse.io/orbitsystems/jobs/4"),  # a job system: never an employer's own
    ("Bluefin Robotics", "https://www.bluefin.com/jobs/5"),
    ("Shared Host Inc", "https://careers.sharedhost.com/jobs/6"),  # another company posts there too
    ("Dismissed Dynamics", "https://dynamics.com/jobs/7"),
    ("Trusted Tech", "https://trustedtech.com/jobs/8"),
    ("Known Labs", "https://knownlabs.com/jobs/9"),
    ("Cased Corp", "https://CASED.com/jobs/10"),
    ("", "https://nameless.com/jobs/11"),
    ("No Url Co", ""),
    ("Odd Url Co", "not a url"),
    ("Outreach Only Ltd", "https://boards.lever.co/outreachonly/12"),
]
OTHER_POSTINGS = [
    ("Somebody Else", "https://sharedhost.com/jobs/99"),
    ("Somebody Else", "https://www.sharedhost.com/jobs/100"),
]
WEBSITES = {
    "Acme Robotics": "https://www.acme-robotics.com",
    "Bluefin Robotics": "bluefin.com",
    "Outreach Only Ltd": "https://outreachonly.com/about",
    "Known Labs": "knownlabs.com",
    "Trusted Tech": "https://trustedtech.com",
    "Never Applied Co": "https://neverapplied.com",
    "Cased Corp": "cased.com",
}
EXISTING = [  # (company, domain, status)
    ("Dismissed Dynamics", "dynamics.com", "dismissed"),
    ("Trusted Tech", "trustedtech.com", "trusted"),
    ("Known Labs", "knownlabs.com", "suggested"),
]


def trust_world(test):
    tempdir = tempfile.TemporaryDirectory()
    test.addCleanup(tempdir.cleanup)
    _, path = helpers_platform.build_and_migrate(Path(tempdir.name))
    conn = connect_product(path)
    test.addCleanup(conn.close)
    stamp = "2026-09-01T12:00:00+00:00"
    with conn:
        for number, (company, url) in enumerate(APPLIED + OTHER_POSTINGS):
            conn.execute(
                "INSERT INTO opportunities(id, company, title, url, first_seen_at, last_seen_at, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (f"o{number}", company, f"Intern {number}", url, stamp, stamp, stamp, stamp),
            )
            if number < len(APPLIED):
                conn.execute(
                    "INSERT INTO applications(id, opportunity_id, user_id, stage, created_at, updated_at) VALUES(?,?,?,?,?,?)",
                    (f"a{number}", f"o{number}", USER, "applied", stamp, stamp),
                )
        for number, (company, website) in enumerate(WEBSITES.items()):
            conn.execute(
                "INSERT INTO outreach_targets(id, user_id, company, website, status, created_at, updated_at) VALUES(?,?,?,?,?,?,?)",
                (f"t{number}", USER, company, website, "sent", stamp, stamp),
            )
        for number, (company, domain, status) in enumerate(EXISTING):
            conn.execute(
                "INSERT INTO employer_domains(id, user_id, company_key, company, domain, status, source, evidence, created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (f"d{number}", USER, mail_trust.company_key(company), company, domain, status, "manual", "kept", stamp),
            )
    return conn


def employer_domain_rows(conn):
    rows = conn.execute(
        "SELECT company_key, company, domain, status, source, evidence, confirmed_at FROM employer_domains ORDER BY company_key, domain"
    ).fetchall()
    return [tuple(row) for row in rows]


class RefreshSuggestionsParityTests(unittest.TestCase):
    def test_the_rows_and_the_count_are_the_references_on_a_mixed_history(self):
        old, new = trust_world(self), trust_world(self)
        expected_new, actual_new = reference_refresh_suggestions(old, USER), mail_trust.refresh_suggestions(new, USER)
        self.assertEqual(actual_new, expected_new)
        self.assertEqual(employer_domain_rows(new), employer_domain_rows(old))
        self.assertGreaterEqual(expected_new, 4, "the world must suggest something or the comparison proves little")
        domains = {row[2] for row in employer_domain_rows(new)}
        self.assertIn("acme-robotics.com", domains)
        self.assertNotIn("sharedhost.com", domains, "another company's postings live there too")
        self.assertNotIn("greenhouse.io", domains)
        # What was there stays exactly as it was, a dismissal included.
        kept = ("dismissed dynamics", "Dismissed Dynamics", "dynamics.com", "dismissed", "manual", "kept", None)
        self.assertIn(kept, employer_domain_rows(new))

    def test_a_second_refresh_is_the_references_second_refresh_and_scans_no_postings(self):
        old, new = trust_world(self), trust_world(self)
        reference_refresh_suggestions(old, USER)
        mail_trust.refresh_suggestions(new, USER)
        self.assertEqual(reference_refresh_suggestions(old, USER), 0)
        real, asked = mail_trust._url_hosts_elsewhere, []

        def counted(conn_, domain, key):
            asked.append(domain)
            return real(conn_, domain, key)

        with mock.patch.object(mail_trust, "_url_hosts_elsewhere", side_effect=counted):
            self.assertEqual(mail_trust.refresh_suggestions(new, USER), 0)
        # Only the pair that was skipped because another company posts there has no row, so it is asked about again.
        self.assertEqual(asked, ["sharedhost.com"])
        self.assertEqual(employer_domain_rows(new), employer_domain_rows(old))

    def test_a_pair_seen_twice_is_scanned_once(self):
        conn = trust_world(self)
        real = mail_trust._url_hosts_elsewhere
        asked = []

        def counted(conn_, domain, key):
            asked.append((domain, key))
            return real(conn_, domain, key)

        with mock.patch.object(mail_trust, "_url_hosts_elsewhere", side_effect=counted):
            mail_trust.refresh_suggestions(conn, USER)
        self.assertEqual(len(asked), len(set(asked)))
        self.assertEqual(len([pair for pair in asked if pair[0] == "acme-robotics.com"]), 1)

    def test_a_pair_that_exists_under_any_status_is_never_changed(self):
        for status in ("suggested", "trusted", "dismissed"):
            with self.subTest(status=status):
                conn = trust_world(self)
                with conn:
                    conn.execute("UPDATE employer_domains SET status=? WHERE domain='knownlabs.com'", (status,))
                before = [row for row in employer_domain_rows(conn) if row[2] == "knownlabs.com"]
                mail_trust.refresh_suggestions(conn, USER)
                self.assertEqual([row for row in employer_domain_rows(conn) if row[2] == "knownlabs.com"], before)


class SeenRowParityTests(unittest.TestCase):
    """inbox-7: one read of a message's stored row answers both 'read already?' and 'judged by older rules?'."""

    def reference(self, conn, gmail_id):
        """REFERENCE: _seen, and the judged_before query read() ran after the message was fetched, as they were before inbox-7."""
        row = conn.execute("SELECT kind, rules FROM outreach_inbox_messages WHERE user_id=? AND gmail_id=?", (USER, gmail_id)).fetchone()
        seen = row is not None and not (row[0] in outreach_inbox.REJUDGED and int(row[1] or 0) < outreach_inbox.RULES)
        judged_before = conn.execute(
            "SELECT 1 FROM outreach_inbox_messages WHERE user_id=? AND gmail_id=? AND rules < ?", (USER, gmail_id, outreach_inbox.RULES),
        ).fetchone() is not None
        return seen, judged_before

    def test_every_kind_and_rules_version_answers_as_the_two_queries_did(self):
        conn, _ids = database(self, 1, 1, datetime.now(timezone.utc))
        kinds = ("ignored", "automatic", "possible", "reply", "confirmed", "rejected")
        stamp = "2026-09-20T10:00:00+00:00"
        with conn:
            for kind in kinds:
                for rules in range(0, outreach_inbox.RULES + 2):
                    conn.execute(
                        "INSERT INTO outreach_inbox_messages(user_id, gmail_id, kind, recorded_at, rules) VALUES(?,?,?,?,?)",
                        (USER, f"{kind}-{rules}", kind, stamp, rules),
                    )
        cases = [f"{kind}-{rules}" for kind in kinds for rules in range(0, outreach_inbox.RULES + 2)] + ["never-read"]
        for gmail_id in cases:
            with self.subTest(gmail_id=gmail_id):
                seen, judged_before = self.reference(conn, gmail_id)
                stored = outreach_inbox._stored(conn, USER, gmail_id)
                self.assertEqual(outreach_inbox._settled(stored), seen)
                self.assertEqual(outreach_inbox._seen(conn, USER, gmail_id), seen)
                if not seen:  # read() goes on to judge only a message that is not settled
                    self.assertEqual(stored is not None and int(stored[1] or 0) < outreach_inbox.RULES, judged_before)
        self.assertTrue(any(self.reference(conn, case) == (False, True) for case in cases), "older rules' set-aside rows are covered")
        self.assertTrue(any(self.reference(conn, case) == (False, False) for case in cases), "a message never read is covered")


def reference_settle_event(conn, user_id, gmail_id, event_id, *, application_id):
    """REFERENCE: application_inbox._settle_event as it was before inbox-8 (the ledger read twice)."""
    ai = application_inbox
    if not event_id or ai._message_actions(conn, user_id, gmail_id, "proposed"):
        return
    applied = any(action["status"] == "applied" for action in ai._message_actions(conn, user_id, gmail_id))
    with conn:
        conn.execute(
            """
            UPDATE monitored_events SET status=?, decided_at=?, decided_by='student', application_id=COALESCE(?, application_id)
            WHERE id=? AND user_id=? AND status='pending'
            """,
            ("confirmed" if applied else "ignored", "2026-09-30T00:00:00+00:00", application_id, event_id, user_id),
        )


class SettleEventParityTests(unittest.TestCase):
    STATUSES = ("proposed", "applied", "rejected", "superseded", "undone")

    def add_case(self, conn, name, statuses):
        stamp = "2026-09-30T10:00:00+00:00"
        insert = (
            "INSERT INTO automation_actions(id, user_id, feature, action_type, subject_kind, subject_id, status, idempotency_key, created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)"
        )
        with conn:
            conn.execute(
                "INSERT INTO monitored_events(id, user_id, external_id, event_type, confidence, payload_json, status, created_at) "
                "VALUES(?,?,?,?,?,?,?,?)", (f"ev-{name}", USER, f"gmail:{name}", "rejection", 0.9, "{}", "pending", stamp),
            )
            for number, status in enumerate(statuses):
                conn.execute(insert, (f"act-{name}-{number}", USER, application_inbox.FEATURE, "application.stage", "application",
                                      "app-1", status, f"gmail:{name}:{number}", stamp))
            # Another email's proposal, with a lookalike id, and another feature's action on this email: neither counts.
            conn.execute(insert, (f"act-{name}-other", USER, application_inbox.FEATURE, "application.stage", "application",
                                  "app-1", "proposed", f"gmail:{name}x:0", stamp))
            conn.execute(insert, (f"act-{name}-feature", USER, "another_feature", "x", "application", "app-1", "proposed",
                                  f"gmail:{name}:9", stamp))

    def card(self, conn, name):
        row = conn.execute("SELECT status, application_id FROM monitored_events WHERE id=?", (f"ev-{name}",)).fetchone()
        return tuple(row)

    def test_every_mix_of_ledger_statuses_settles_the_card_as_before(self):
        conn, _ids = database(self, 1, 1, datetime.now(timezone.utc))
        cases = [()] + [(a,) for a in self.STATUSES] + [(a, b) for a in self.STATUSES for b in self.STATUSES if a != b] \
            + [("applied", "rejected", "proposed"), ("applied", "rejected", "superseded")]
        outcomes = set()
        for number, statuses in enumerate(cases):
            old, new = f"old{number}", f"new{number}"
            self.add_case(conn, old, statuses)
            self.add_case(conn, new, statuses)
            reference_settle_event(conn, USER, old, f"ev-{old}", application_id=None)
            application_inbox._settle_event(conn, USER, new, f"ev-{new}", application_id=None)
            with self.subTest(statuses=statuses):
                self.assertEqual(self.card(conn, new)[0], self.card(conn, old)[0])
            outcomes.add(self.card(conn, new)[0])
        self.assertEqual(outcomes, {"pending", "confirmed", "ignored"}, "the cases must reach every outcome")

    def test_the_ledger_is_read_once_and_nothing_is_read_without_an_event(self):
        conn, _ids = database(self, 1, 2, datetime.now(timezone.utc))
        self.add_case(conn, "once", ("rejected", "superseded"))
        with mock.patch.object(application_inbox, "_message_actions", wraps=application_inbox._message_actions) as read:
            application_inbox._settle_event(conn, USER, "once", "ev-once", application_id=None)
            self.assertEqual(read.call_count, 1)
            read.reset_mock()
            application_inbox._settle_event(conn, USER, "once", "", application_id=None)
            self.assertEqual(read.call_count, 0)
        self.assertEqual(self.card(conn, "once"), ("ignored", None))


MIGRATION_0048 = Path(__file__).resolve().parent.parent / "migrations" / "0048_mail_hot_path_indexes.sql"


class MailIndexMigrationTests(unittest.TestCase):
    """0048 adds three indexes that queries use, and changes no row or result."""

    def plan(self, conn, sql, params):
        return " | ".join(row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + sql, params).fetchall())

    def test_the_indexes_exist_and_the_hot_queries_use_them(self):
        conn, _ids = database(self, 30, 1, datetime.now(timezone.utc))
        self.assertIn("0048_mail_hot_path_indexes.sql", {row[0] for row in conn.execute("SELECT name FROM schema_migrations")})
        indexes = {row[0]: row[1] for row in conn.execute("SELECT name, sql FROM sqlite_master WHERE type='index'")}
        self.assertIn("(user_id, event_type, created_at)", " ".join(indexes["idx_outreach_events_user_type"].split()))
        self.assertIn("(company_sort_key)", indexes["idx_opportunities_company_sort_key"])
        reply_events = self.plan(
            conn, "SELECT target_id, detail, created_at FROM outreach_events WHERE user_id=? AND event_type=?", (USER, "gmail_sent"))
        self.assertIn("idx_outreach_events_user_type (user_id=? AND event_type=?)", reply_events)
        draft_events = self.plan(
            conn,
            "SELECT e.target_id, e.detail, e.created_at FROM outreach_events e JOIN outreach_targets t ON t.id=e.target_id AND t.user_id=e.user_id "
            "WHERE e.user_id=? AND e.event_type=? AND e.created_at>=? ORDER BY e.created_at", (USER, "gmail_draft_created", "2026-01-01"))
        self.assertIn("idx_outreach_events_user_type (user_id=? AND event_type=? AND created_at>?)", draft_events)
        self.assertIn("idx_opportunities_company_sort_key (company_sort_key=?)",
                      self.plan(conn, "SELECT id FROM opportunities WHERE company_sort_key = ?", ("acme",)))

    def test_per_company_event_queries_lead_with_the_company(self):
        """Without ANALYZE SQLite picks the index with the most equality columns: these must not read a whole kind of event."""
        conn, _ids = database(self, 30, 1, datetime.now(timezone.utc))
        indexes = {row[0]: row[1] for row in conn.execute("SELECT name, sql FROM sqlite_master WHERE type='index'")}
        self.assertIn("(target_id, user_id, event_type, created_at DESC)", " ".join(indexes["idx_outreach_events_target_type"].split()))
        queries = {
            "one kind (_already_sent, _followed_up)":
                ("SELECT detail, created_at FROM outreach_events WHERE target_id=? AND user_id=? AND event_type=?", ("t", USER, "gmail_sent")),
            "ordered (_draft_events)":
                ("SELECT id, detail, created_at FROM outreach_events WHERE target_id=? AND user_id=? AND event_type=? ORDER BY created_at DESC",
                 ("t", USER, "gmail_draft_created")),
            "several kinds (sent_since)":
                ("SELECT created_at FROM outreach_events WHERE target_id=? AND user_id=? AND event_type IN (?, ?)",
                 ("t", USER, "gmail_sent", "sent")),
        }
        for name, (sql, params) in queries.items():
            with self.subTest(name):
                plan = self.plan(conn, sql, params)
                self.assertIn("idx_outreach_events_target_type (target_id=? AND user_id=? AND event_type=?", plan)
                self.assertNotIn("idx_outreach_events_user_type", plan)

    def test_running_it_again_changes_nothing(self):
        conn, _ids = database(self, 30, 2, datetime.now(timezone.utc))
        counts = [conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in ("outreach_events", "outreach_targets", "opportunities")]
        before = conn.execute("SELECT name, sql FROM sqlite_master ORDER BY name").fetchall()
        conn.executescript(MIGRATION_0048.read_text(encoding="utf-8"))
        self.assertEqual(conn.execute("SELECT name, sql FROM sqlite_master ORDER BY name").fetchall(), before)
        self.assertEqual(counts, [conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in ("outreach_events", "outreach_targets", "opportunities")])

    def test_the_pending_drafts_are_the_same_with_and_without_the_indexes(self):
        now = datetime.now(timezone.utc)
        conn, _ids = database(self, 50, 3, now)
        with_indexes = sends._pending(conn, USER, now)
        conn.execute("DROP INDEX idx_outreach_events_user_type")
        conn.execute("DROP INDEX idx_outreach_events_target_type")
        conn.execute("DROP INDEX idx_opportunities_company_sort_key")
        self.assertEqual(sends._pending(conn, USER, now), with_indexes)

    def tied_events(self, now):
        """Companies whose drafts, and whose sends, share a created_at, as the coarse Windows clock once produced."""
        conn, _ids = database(self, 3, 11, now)
        drafting, sending = "outreach-tie-drafts", "outreach-tie-sends"
        stamp = (now - timedelta(hours=2)).isoformat(timespec="microseconds")
        for target in (drafting, sending):
            conn.execute(
                "INSERT INTO outreach_targets(id, user_id, company, contact_name, contact_email, contact_cc, location, email_subject, "
                "email_body, follow_up_subject, status, sent_at, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (target, USER, f"{target} Labs", "Pat", f"pat@{target}.example", "", "Austin, TX", "Question", "Hi Pat", "",
                 "drafted", None, stamp, stamp))
        order = iter(range(100))

        def event(target, kind, detail):
            conn.execute(
                "INSERT INTO outreach_events(id, target_id, user_id, event_type, detail, created_at) VALUES(?,?,?,?,?,?)",
                (f"tie-{next(order):03d}", target, USER, kind, json.dumps(detail), stamp))

        # Inserted first, second: the ids are in insertion order and not in sort order.
        for name in ("first", "second"):
            event(drafting, "gmail_draft_created", {"kind": "initial", "draft_id": name, "message_id": f"m-{name}", "fingerprint": "f"})
            event(sending, "gmail_sent", {"kind": "initial", "message_id": f"s-{name}", "thread_id": f"t-{name}"})
        conn.commit()
        return conn, drafting, sending

    def answers(self, conn, drafting, sending, now):
        from opportunity_app import outreach_delivery as delivery
        from opportunity_app.outreach_gmail import _draft_events, _previous_draft

        pending = [item["detail"]["draft_id"] for item in sends._pending(conn, USER, now) if item["target_id"] == drafting]
        watched = [item["detail"]["message_id"] for item in delivery._watched(conn, USER, now) if item["target_id"] == sending]
        previous = _previous_draft(conn, drafting, USER, "initial", "f", "", "")
        return {
            "pending": pending,
            "watched": watched,
            "previous": previous["draft_id"],
            "draft_events": [detail["draft_id"] for _id, detail in _draft_events(conn, drafting, USER, "initial")],
        }

    def test_tied_events_are_picked_the_same_with_and_without_the_indexes(self):
        """0048 changes the plan of these queries; which of two events with one created_at they keep must not move."""
        now = datetime.now(timezone.utc)
        conn, drafting, sending = self.tied_events(now)
        with_indexes = self.answers(conn, drafting, sending, now)
        for name in ("idx_outreach_events_user_type", "idx_outreach_events_target_type", "idx_opportunities_company_sort_key"):
            conn.execute(f"DROP INDEX {name}")
        self.assertEqual(self.answers(conn, drafting, sending, now), with_indexes)
        # Frozen: what the code answered before 0048 existed. The newest draft kept for the company is the first one
        # stored (the last row of a list that listed ties newest-stored first), the sends are watched second then
        # first, and the per-company queries read ties in insertion order.
        self.assertEqual(
            with_indexes,
            {"pending": ["first"], "watched": ["s-second", "s-first"], "previous": "first", "draft_events": ["first", "second"]},
        )


if __name__ == "__main__":
    unittest.main()
