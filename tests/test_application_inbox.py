"""Application mail: job-system emails read from Gmail, matched, then acted on or proposed (application_inbox.py).

Every company, address and message here is invented. Gmail is FakeGmail from
test_outreach_gmail, taught users.history.list and a historyId on the profile.
"""

import base64
import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from opportunity_app import STATIC_DIR, application_inbox, automation, internal_automation, mail_trust, outreach_gmail
from opportunity_app.actions import record_intent, update_application
from opportunity_app.api import create_app
from opportunity_app.application_inbox import match_application, parse_message
from opportunity_app.connections import classify_monitored_message, decide_monitored_event, monitored_event
from opportunity_app.operations import export_account
from opportunity_app.schema import connect_product, utc_now
from opportunity_app.urgent import urgent_queue

from helpers_platform import build_and_migrate
from test_outreach_gmail import ACCOUNT, FakeGmail, forget_gmail_backoff, rate_limited

USER = "local-user"
AUTH = {"Authorization": "Bearer mail-owner"}
FEATURE = "application_mail"
GREENHOUSE = "Acme Robotics Hiring Team <no-reply@us.greenhouse-mail.io>"


def now_utc():
    return datetime.now(timezone.utc).replace(microsecond=0)


def in_days(days):
    """A day this many days from now (UTC), so a stated date is always ahead of the email, whatever day the test runs."""
    return (now_utc() + timedelta(days=days)).date()


def spelled(day, *, year=True):
    """A date as an email writes it: "October 3, 2027", or "October 3" with no year."""
    return f"{day:%B} {day.day}, {day.year}" if year else f"{day:%B} {day.day}"


def arh(domain, *, kind="dmarc", server="mx.google.com", result="pass", signer=None):
    """An Authentication-Results header line as Gmail's inbound servers write it."""
    signer = signer or domain
    if kind == "dmarc":
        body = (f"dkim={result} header.i=@{signer} header.s=s1 header.b=abc; "
                f"spf=pass (google.com: domain of bounce@{domain} designates 192.0.2.1 as permitted sender) smtp.mailfrom=bounce@{domain}; "
                f"dmarc={result} (p=REJECT sp=REJECT dis=NONE) header.from={domain.split('.', 1)[-1] if domain.count('.') > 1 else domain}")
    else:
        body = f"dkim={result} header.i=@{signer} header.s=s1 header.b=abc; spf=neutral smtp.mailfrom=bounce@{domain}"
    return f"Authentication-Results: {server};\n       {body}\n"


def job_mail(*, sender=GREENHOUSE, subject="Thank you for applying to Acme Robotics", body="", headers=None, date=None, html=False):
    """A message as the raw text Gmail stores, with Gmail's own sender check on top unless ``headers`` says otherwise."""
    domain = sender.rsplit("@", 1)[1].rstrip(">").strip()
    top = arh(domain) if headers is None else headers
    date_line = f"Date: {format_datetime(date)}\n" if date else ""
    content_type = "text/html" if html else "text/plain"
    return (
        f"{top}From: {sender}\nTo: {ACCOUNT}\nSubject: {subject}\n{date_line}"
        f"MIME-Version: 1.0\nContent-Type: {content_type}; charset=UTF-8\n\n{body}\n"
    ).encode()


def mail_from(raw, *, received=None, gmail_id="m-1", thread_id="t-1"):
    received = received or now_utc()
    return parse_message({
        "id": gmail_id, "threadId": thread_id, "labelIds": ["INBOX"], "internalDate": str(int(received.timestamp() * 1000)),
        "raw": base64.urlsafe_b64encode(raw).decode(),
    })


class MailboxGmail(FakeGmail):
    """FakeGmail with a mailbox history, a historyId on the profile, and searches by kind."""

    def __init__(self):
        super().__init__()
        self.history_id = 100
        self.history = []
        self.history_expired = False
        self.history_page_size = None
        self.recovery_found = []
        self.backfill_found = []
        self.list_page_size = None
        self.threads = {}
        self.labels = {}
        self.message_gets = 0
        self.throttle_after = None

    def deliver(self, gmail_id, raw, received, *, thread_id=None, labels=("INBOX",)):
        self.raw[gmail_id] = (raw, int(received.timestamp() * 1000))
        self.threads[gmail_id] = thread_id or f"thread-{gmail_id}"
        self.labels[gmail_id] = list(labels)
        self.history_id += 1
        self.history.append({"id": str(self.history_id), "messagesAdded": [{"message": {"id": gmail_id, "labelIds": list(labels)}}]})

    def store(self, gmail_id, raw, received):
        """A message in the mailbox that no history record announces (found only by a search)."""
        self.raw[gmail_id] = (raw, int(received.timestamp() * 1000))
        self.threads[gmail_id] = f"thread-{gmail_id}"
        self.labels[gmail_id] = ["INBOX"]

    def _page(self, found, token, size):
        start = int(token or 0)
        page = found[start:start + size] if size else found[start:]
        body = {"messages": [{"id": item} for item in page]}
        if size and start + size < len(found):
            body["nextPageToken"] = str(start + size)
        return body

    def handler(self, request):
        path = request.url.path
        if request.url.host == "gmail.googleapis.com" and request.method == "GET":
            if request.headers.get("Authorization", "").removeprefix("Bearer ") not in self.expired_tokens and self.read_response is None:
                if path.endswith("/profile"):
                    self.requests.append(request)
                    return httpx.Response(200, json={"emailAddress": self.profile_email, "historyId": str(self.history_id)})
                if path.endswith("/history"):
                    self.requests.append(request)
                    if self.history_expired:
                        return httpx.Response(404, json={"error": {"code": 404, "message": "Requested entity was not found."}})
                    start = int(request.url.params.get("startHistoryId"))
                    records = [record for record in self.history if int(record["id"]) > start]
                    offset = int(request.url.params.get("pageToken") or 0)
                    size = self.history_page_size or len(records) or 1
                    page = records[offset:offset + size]
                    body = {"history": page, "historyId": str(self.history_id)}
                    if offset + size < len(records):
                        body["nextPageToken"] = str(offset + size)
                    return httpx.Response(200, json=body)
                if path.endswith("/messages"):
                    self.requests.append(request)
                    query = request.url.params.get("q", "")
                    self.searches.append(query)
                    found = self.recovery_found if query.startswith("in:inbox after:") else self.backfill_found if "from:(" in query else []
                    return httpx.Response(200, json=self._page(found, request.url.params.get("pageToken"), self.list_page_size))
                if "/messages/" in path and request.url.params.get("format") == "raw":
                    self.requests.append(request)
                    self.message_gets += 1
                    if self.throttle_after is not None and self.message_gets > self.throttle_after:
                        return rate_limited()
                    message_id = path.rsplit("/", 1)[1]
                    if message_id not in self.raw:
                        return httpx.Response(404)
                    raw, received = self.raw[message_id]
                    return httpx.Response(200, json={
                        "id": message_id, "threadId": self.threads.get(message_id, message_id), "labelIds": self.labels.get(message_id, ["INBOX"]),
                        "internalDate": str(received), "raw": base64.urlsafe_b64encode(raw).decode(),
                    })
        return super().handler(request)


class MailCase(unittest.TestCase):
    """A throwaway database with Gmail connected, the seeded Orbit application (applied), and Acme's (applying)."""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        root = Path(self.tempdir.name)
        _, self.platform_path = build_and_migrate(root)
        self.key = Fernet.generate_key().decode()
        env = mock.patch.dict("os.environ", {
            "GOOGLE_OAUTH_CLIENT_ID": "client-id", "GOOGLE_OAUTH_CLIENT_SECRET": "client-secret",
            "PIPELINE_CONNECTION_KEY": self.key, "PIPELINE_OUTREACH_ACCOUNT": ACCOUNT,
        })
        env.start()
        self.addCleanup(env.stop)
        forget_gmail_backoff(self)
        self.gmail = MailboxGmail()
        self.factory = lambda: httpx.Client(transport=httpx.MockTransport(self.gmail.handler))
        self.conn = connect_product(self.platform_path)
        self.addCleanup(self.conn.close)
        fernet = Fernet(self.key.encode())
        with self.conn:
            self.conn.execute(
                """INSERT INTO connector_accounts(id, user_id, provider, scopes_json, encrypted_access_token, encrypted_refresh_token, status, created_at, updated_at)
                   VALUES(?, ?, 'gmail_drafts', '[]', ?, ?, 'connected', ?, ?)""",
                (f"connector-gmail_drafts-{USER}", USER, fernet.encrypt(b"valid-token").decode(), fernet.encrypt(b"refresh").decode(), utc_now(), utc_now()),
            )
        self.acme = record_intent(self.conn, "job-a", "apply_opened", user_id=USER)["application_id"]
        self.orbit = "app-job-b"
        application_inbox._PASS_LOCKS.clear()

    # --- helpers

    def switch(self, mode, *, since=None):
        """Set the switch directly (the 48-hour shadow gate is automation's, tested there), as turned on at ``since``."""
        stamp = (since or now_utc() - timedelta(hours=1)).isoformat()
        with self.conn:
            self.conn.execute(
                "INSERT INTO user_settings(user_id, key, value, updated_at) VALUES(?, ?, ?, ?) "
                "ON CONFLICT(user_id, key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (USER, FEATURE, mode, stamp),
            )

    def pass_once(self, **kwargs):
        return application_inbox.run_pass(self.conn, user_id=USER, client_factory=self.factory, force=True, **kwargs)

    def started(self, mode="on"):
        """The switch on, and a first pass that took the cursor (and found no backfill)."""
        self.switch(mode)
        result = self.pass_once()
        self.assertEqual(result["state"], "ok", result)
        return result

    def deliver(self, gmail_id, raw, *, minutes_ago=5, **kwargs):
        received = now_utc() - timedelta(minutes=minutes_ago)
        self.gmail.deliver(gmail_id, raw, received, **kwargs)
        return received

    def actions(self, **where):
        clauses = " AND ".join(f"{key}=?" for key in where)
        rows = self.conn.execute(
            f"SELECT * FROM automation_actions WHERE user_id=?{' AND ' + clauses if clauses else ''} ORDER BY created_at, id",
            (USER, *where.values()),
        ).fetchall()
        return [automation._decode(row) for row in rows]

    def stage(self, application_id):
        row = self.conn.execute("SELECT stage, applied_at FROM applications WHERE id=?", (application_id,)).fetchone()
        return row["stage"], row["applied_at"]

    def message_row(self, gmail_id):
        return self.conn.execute("SELECT * FROM application_mail_messages WHERE user_id=? AND gmail_id=?", (USER, gmail_id)).fetchone()

    def sync(self):
        return dict(self.conn.execute("SELECT * FROM application_mail_sync WHERE user_id=?", (USER,)).fetchone())

    def add_application(self, opportunity_id, company, title, *, stage="applied", url=None):
        now = utc_now()
        with self.conn:
            self.conn.execute(
                "INSERT INTO opportunities(id, company, title, url, first_seen_at, last_seen_at, created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                (opportunity_id, company, title, url or f"https://jobs.example.org/{opportunity_id}", now, now, now, now),
            )
            self.conn.execute(
                "INSERT INTO applications(id, opportunity_id, user_id, stage, created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?)",
                (f"app-{opportunity_id}", opportunity_id, USER, stage, now, now),
            )
        return f"app-{opportunity_id}"


def acme_confirmation(**overrides):
    return job_mail(
        subject="Thank you for applying to Acme Robotics",
        body="Hi Sam,\n\nThank you for applying to the Mechanical Engineering Intern role at Acme Robotics. We have received your application.",
        **overrides,
    )


def orbit_mail(subject, body, **overrides):
    return job_mail(sender="Orbit Systems <no-reply@hire.lever.co>", subject=subject, body=body, **overrides)


# --- Registry and domains ------------------------------------------------------------------------


class RegistryTests(unittest.TestCase):
    def test_the_switch_is_registered_off_by_default_and_needs_shadow_first(self):
        feature = automation.FEATURES[FEATURE]
        self.assertEqual((feature.label, feature.group, feature.risk, feature.modes),
                         ("Update applications from job emails", "applications", "internal", ("off", "shadow", "on")))
        self.assertIn("Anything unclear waits for you", feature.description)
        self.assertEqual(application_inbox.HEALTH_COMPONENT, "inbox.applications")
        self.assertFalse(automation.undoable("application.capture_proposal"))
        self.assertTrue(automation.undoable("application.deadline"))


class PublicSuffixTests(unittest.TestCase):
    def test_registrable_domains_follow_the_public_suffix_list(self):
        self.assertTrue(mail_trust.psl_available(), "publicsuffixlist is in requirements-web")
        self.assertTrue(mail_trust.same_organization("careers.acme.com", "acme.com"))
        self.assertFalse(mail_trust.same_organization("acme.co.uk", "evil.co.uk"))
        self.assertFalse(mail_trust.same_organization("acme.github.io", "other.github.io"), "a private suffix: two tenants")
        self.assertFalse(mail_trust.same_organization("acme-careers.com", "acme.com"))
        self.assertFalse(mail_trust.same_organization("acme.com.evil.io", "acme.com"))
        self.assertEqual(mail_trust.registrable_domain("mail.us.greenhouse-mail.io"), "greenhouse-mail.io")

    def test_it_fails_closed(self):
        for host in ("192.0.2.10", "[2001:db8::1]", "acme.unknowntld", "com", "github.io", "", "a..b.com", "localhost"):
            with self.subTest(host=host):
                self.assertIsNone(mail_trust.registrable_domain(host))
        self.assertFalse(mail_trust.same_organization("acme.unknowntld", "acme.unknowntld"), "no suffix, no match, even with itself")

    def test_without_the_package_nothing_matches(self):
        with mock.patch.object(mail_trust, "_PSL", None), mock.patch.object(mail_trust, "_PSL_MISSING", True):
            self.assertIsNone(mail_trust.registrable_domain("careers.acme.com"))
            self.assertFalse(mail_trust.same_organization("careers.acme.com", "acme.com"))

    def test_the_shipped_list_and_what_it_reads(self):
        lists = mail_trust.sender_lists()
        for domain in ("greenhouse-mail.io", "greenhouse.io", "hire.lever.co", "ashbyhq.com", "myworkday.com", "smartrecruiters.com",
                       "icims.com", "workablemail.com"):
            self.assertIn(domain, lists["ats"])
        for domain in ("hackerrank.com", "codesignal.com", "codility.com", "hirevue.com"):
            self.assertIn(domain, lists["assessment"])
        for domain in ("calendly.com", "goodtime.io", "modernloop.io"):
            self.assertIn(domain, lists["scheduling"])
        self.assertEqual(mail_trust.listed("us.greenhouse-mail.io"), "ats")
        self.assertIsNone(mail_trust.listed("greenhouse-mail.io.evil.com"))
        self.assertIsNone(mail_trust.listed("calendly.com", mail_trust.AUTHORIZING_CATEGORIES), "a scheduling tool authorizes nothing")


class AuthenticationTests(unittest.TestCase):
    def check(self, raw):
        return mail_trust.authenticate(mail_from(raw).message)

    def test_gmails_dmarc_pass_counts(self):
        result = self.check(acme_confirmation())
        self.assertEqual((result.ok, result.method), (True, "dmarc"))

    def test_aligned_dkim_counts_without_dmarc(self):
        result = self.check(acme_confirmation(headers=arh("us.greenhouse-mail.io", kind="dkim")))
        self.assertEqual((result.ok, result.method), (True, "dkim"))

    def test_dkim_signed_by_another_domain_does_not(self):
        result = self.check(acme_confirmation(headers=arh("us.greenhouse-mail.io", kind="dkim", signer="bulk-sender.com")))
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "the email is signed by another domain")

    def test_a_missing_check_does_not(self):
        result = self.check(acme_confirmation(headers=""))
        self.assertEqual((result.ok, result.reason), (False, "Gmail's sender check is missing"))

    def test_a_forged_result_lower_down_is_ignored(self):
        # The topmost header is another server's; a forged Gmail pass sits below it.
        forged = arh("us.greenhouse-mail.io", server="mx.other-relay.net", result="fail") + arh("us.greenhouse-mail.io")
        result = self.check(acme_confirmation(headers=forged))
        self.assertEqual((result.ok, result.reason), (False, "the sender check was not added by Gmail"))

    def test_a_forged_gmail_result_below_the_real_one_is_ignored(self):
        forged = arh("us.greenhouse-mail.io", result="fail") + arh("us.greenhouse-mail.io")
        result = self.check(acme_confirmation(headers=forged))
        self.assertFalse(result.ok, "only the topmost mx.google.com result counts")

    def test_a_forged_from_without_dkim_or_dmarc_does_not(self):
        headers = "Authentication-Results: mx.google.com;\n       spf=softfail smtp.mailfrom=someone@elsewhere.example\n"
        result = self.check(acme_confirmation(headers=headers))
        self.assertEqual((result.ok, result.reason), (False, "the sender did not pass Gmail's check"))

    def test_conflicting_dmarc_results_are_ambiguous(self):
        headers = "Authentication-Results: mx.google.com;\n       dmarc=pass header.from=greenhouse-mail.io;\n       dmarc=fail header.from=greenhouse-mail.io\n"
        self.assertEqual(self.check(acme_confirmation(headers=headers)).reason, "Gmail's sender check is ambiguous")


# --- Matching --------------------------------------------------------------------------------


class MatchTests(MailCase):
    def match(self, raw):
        return match_application(self.conn, USER, mail_from(raw))

    def test_a_job_id_in_a_link_matches_first(self):
        with self.conn:
            self.conn.execute(
                "UPDATE opportunity_sources SET source_url='https://boards.greenhouse.io/acmerobotics/jobs/4455667', external_id='4455667' WHERE opportunity_id='job-a'",
            )
        raw = job_mail(sender="Careers <no-reply@us.greenhouse-mail.io>", subject="Your application",
                       body="Thanks! View the posting: https://boards.greenhouse.io/acmerobotics/jobs/4455667?gh_src=abc")
        found = self.match(raw)
        self.assertEqual((found.tier, found.application_id), ("job_id", self.acme))

    def test_company_and_role_match(self):
        found = self.match(acme_confirmation())
        self.assertEqual((found.tier, found.application_id), ("company_title", self.acme))

    def test_the_company_alone_matches_its_one_open_application(self):
        found = self.match(job_mail(subject="Update from Acme Robotics", body="Hi Sam, a quick update on your application."))
        self.assertEqual((found.tier, found.application_id), ("company_single", self.acme))

    def test_two_open_applications_at_one_company_are_ambiguous_and_ranked(self):
        second = self.add_application("acme-2", "Acme Robotics", "Firmware Intern")
        found = self.match(job_mail(subject="Update from Acme Robotics", body="Hi Sam, a quick update on your application."))
        self.assertEqual(found.tier, "ambiguous")
        self.assertEqual(set(found.candidates), {self.acme, second})
        titled = self.match(acme_confirmation())
        self.assertEqual((titled.tier, titled.application_id), ("company_title", self.acme), "the role tells them apart")
        self.assertEqual(titled.candidates[0], self.acme)

    def test_no_company_named_is_none(self):
        self.assertEqual(self.match(job_mail(sender="Careers <no-reply@us.greenhouse-mail.io>", subject="Your application",
                                             body="Thanks for applying.")).tier, "none")

    def test_a_shared_ats_sender_is_told_apart_by_the_company_it_names(self):
        orbit = self.match(orbit_mail("Your application to Orbit Systems", "Thanks for applying to Orbit Systems for the Controls Co-op role."))
        acme = self.match(acme_confirmation())
        self.assertEqual((orbit.application_id, acme.application_id), (self.orbit, self.acme))
        both_greenhouse = self.match(job_mail(sender="Orbit Systems <no-reply@us.greenhouse-mail.io>", subject="Update from Orbit Systems", body="Hi."))
        self.assertEqual(both_greenhouse.application_id, self.orbit, "the same no-reply address speaks for both")

    def test_a_namesake_is_not_the_same_company(self):
        plain_acme = self.add_application("acme-plain", "Acme", "Test Technician", stage="applied")
        found = self.match(job_mail(subject="Update from Acme Robotics", body="Hi Sam, a quick update."))
        self.assertEqual(found.application_id, self.acme)
        self.assertNotIn(plain_acme, found.candidates, "Acme Robotics is not Acme")
        only_acme = self.match(job_mail(sender="Acme Hiring Team <no-reply@us.greenhouse-mail.io>", subject="Update from Acme", body="Hi."))
        self.assertEqual(only_acme.application_id, plain_acme)


# --- Deciding and acting ---------------------------------------------------------------------------


class LiveMailTests(MailCase):
    def test_nothing_runs_while_off(self):
        self.assertEqual(self.pass_once()["state"], "off")
        self.assertEqual(self.gmail.requests, [])

    def test_the_first_pass_takes_the_cursor_and_records_when_it_was_turned_on(self):
        since = now_utc() - timedelta(hours=2)
        self.switch("on", since=since)
        self.pass_once()
        sync = self.sync()
        self.assertEqual(sync["history_id"], "100")
        self.assertEqual(automation._parse(sync["enabled_at"]), since)
        self.assertIn(sync["backfill_state"], ("running", "done"))

    def test_a_confirmation_moves_applying_to_applied_with_gmails_received_time(self):
        self.started()
        received = self.deliver("m-1", acme_confirmation(date=now_utc() - timedelta(minutes=5)))
        result = self.pass_once()
        self.assertEqual(result["state"], "ok", result)
        stage, applied_at = self.stage(self.acme)
        self.assertEqual(stage, "applied")
        self.assertEqual(datetime.fromisoformat(applied_at), received, "applied_at is internalDate, not now")
        [action] = self.actions(action_type="application.stage")
        self.assertEqual((action["status"], action["decided_by"], action["basis"].split(";")[1]), ("applied", "system", "match:company_title"))
        self.assertEqual(action["evidence"]["why_proposal"], [])
        event = self.conn.execute("SELECT status, decided_by, application_id FROM monitored_events WHERE external_id='gmail:m-1'").fetchone()
        self.assertEqual(tuple(event), ("confirmed", "system", self.acme))
        self.assertEqual(self.message_row("m-1")["state"], "done")
        timeline = self.conn.execute("SELECT detail_json FROM application_events WHERE application_id=? AND event_type='stage_changed'", (self.acme,)).fetchall()
        self.assertEqual(json.loads(timeline[-1]["detail_json"])["source"], f"automation:{action['id']}")

    def test_one_email_makes_a_stage_change_and_a_task_each_exactly_once(self):
        self.started()
        invite = orbit_mail("Interview invitation: Orbit Systems",
                            "Hi Sam,\n\nWe'd like to invite you to interview for the Controls Co-op role at Orbit Systems.")
        self.deliver("m-2", invite)
        self.pass_once()
        self.assertEqual(self.stage(self.orbit)[0], "interview")
        tasks = self.conn.execute("SELECT title, origin, origin_ref FROM application_tasks WHERE application_id=?", (self.orbit,)).fetchall()
        self.assertEqual([tuple(row) for row in tasks], [("Schedule interview", "email", "m-2")])
        # Read again (queued twice, and decided again after a pause): nothing more.
        with self.conn:
            self.conn.execute("UPDATE application_mail_messages SET state='awaiting_resume' WHERE gmail_id='m-2'")
        self.pass_once()
        self.assertEqual(len(self.actions()), 2)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM application_tasks WHERE application_id=?", (self.orbit,)).fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM monitored_events WHERE external_id='gmail:m-2'").fetchone()[0], 1)

    def test_stages_only_move_forward(self):
        self.started()
        update_application(self.conn, self.orbit, stage="offer", user_id=USER)
        with self.conn:  # the student's change is older than the email, so manual wins does not apply
            self.conn.execute("UPDATE application_events SET created_at=? WHERE application_id=? AND event_type='stage_changed'",
                              ((now_utc() - timedelta(days=1)).isoformat(), self.orbit))
        self.deliver("m-3", orbit_mail("Update on your Orbit Systems application",
                                       "We regret to inform you that we will not be moving forward with your application for Controls Co-op."))
        self.pass_once()
        self.assertEqual(self.stage(self.orbit)[0], "offer", "a rejection never overwrites an offer")
        self.assertEqual(self.actions(action_type="application.stage"), [])

    def test_manual_wins(self):
        self.started()
        received = self.deliver("m-4", orbit_mail("Interview invitation: Orbit Systems",
                                                  "We'd like to invite you to interview for the Controls Co-op role at Orbit Systems."),
                                minutes_ago=30)
        update_application(self.conn, self.orbit, stage="applying", user_id=USER)  # the student, after the email arrived
        self.pass_once()
        [stage_action] = self.actions(action_type="application.stage")
        self.assertEqual(stage_action["status"], "proposed")
        self.assertIn("you changed this application's stage after the email arrived", stage_action["evidence"]["why_proposal"])
        self.assertEqual(self.stage(self.orbit)[0], "applying")
        self.assertLess(received, now_utc())

    def test_a_date_line_far_from_gmails_time_only_proposes(self):
        self.started()
        self.deliver("m-5", acme_confirmation(date=now_utc() - timedelta(days=5)))
        self.pass_once()
        [action] = self.actions()
        self.assertEqual(action["status"], "proposed")
        self.assertIn("its Date line and when Gmail received it are more than 48 hours apart", action["evidence"]["why_proposal"])
        self.assertEqual(self.stage(self.acme)[0], "applying")

    def test_an_offer_is_only_ever_proposed_with_a_notice_that_carries_no_email_text(self):
        self.started()
        self.deliver("m-6", orbit_mail("Offer letter - Controls Co-op",
                                       "Congratulations! We are pleased to offer you the Controls Co-op role. See https://offers.example.org/o/secret-token"))
        self.pass_once()
        [action] = self.actions()
        self.assertEqual((action["status"], action["after"]["stage"]), ("proposed", "offer"))
        self.assertIn("an offer is always yours to confirm", action["evidence"]["why_proposal"])
        [notice] = automation.list_notices(self.conn, USER)
        self.assertEqual((notice["title"], notice["body"]), ("Orbit Systems may have sent an offer", "Open Automation to review it."))
        self.assertEqual(self.stage(self.orbit)[0], "applied")

    def test_an_assessment_task_keeps_its_link_in_the_app_only(self):
        self.started()
        link = "https://www.hackerrank.com/test/abc123/login?token=very-secret-token"
        due = in_days(30)
        raw = job_mail(sender="HackerRank <support@hackerrankforwork.com>", subject="Acme Robotics has invited you to take a test",
                       body=f"Acme Robotics has invited you to take the Mechanical Engineering Intern Coding Test. Please complete the test by {spelled(due)}.\n\n{link}")
        self.deliver("m-7", raw)
        self.pass_once()
        [task] = self.conn.execute("SELECT title, link, due_at, origin FROM application_tasks WHERE application_id=?", (self.acme,)).fetchall()
        self.assertEqual((task["title"], task["link"], task["origin"]), ("Complete the HackerRank assessment", link, "email"))
        self.assertTrue(task["due_at"].startswith(due.isoformat()))
        # Nor in the ledger at all: the applied task's after keeps the link's host only.
        for row in self.conn.execute("SELECT * FROM automation_actions").fetchall():
            self.assertNotIn("very-secret-token", json.dumps(dict(row)))
            self.assertNotIn("abc123", json.dumps(dict(row)))
        [task_action] = self.actions(action_type="application.task")
        self.assertEqual(task_action["after"]["task"]["link_host"], "www.hackerrank.com")
        for action in self.actions():
            self.assertNotIn("very-secret-token", json.dumps(action["evidence"]))
            self.assertNotIn("abc123", json.dumps(action["evidence"]))
        self.assertIn("www.hackerrank.com", self.actions()[0]["evidence"]["excerpt"], "a link is cut to its host")
        for notice in automation.list_notices(self.conn, USER):
            self.assertNotIn("hackerrank.com/test", json.dumps(notice))
        health = json.dumps(automation.health_summary(self.conn, USER))
        self.assertNotIn("very-secret-token", health)
        event = self.conn.execute("SELECT payload_json FROM monitored_events WHERE external_id='gmail:m-7'").fetchone()
        self.assertNotIn("very-secret-token", event["payload_json"])

    def test_a_stated_deadline_becomes_an_urgent_row_from_an_email(self):
        self.started()
        due = in_days(30)
        self.deliver("m-8", job_mail(
            sender="Acme Robotics Hiring Team <no-reply@us.greenhouse-mail.io>", subject="Next step: online assessment",
            body=f"Thanks for applying to the Mechanical Engineering Intern role at Acme Robotics. Please complete our online assessment by {spelled(due)}.",
        ))
        self.pass_once()
        [deadline] = self.conn.execute("SELECT deadline_on, quote, sender_domain FROM email_deadlines WHERE application_id=?", (self.acme,)).fetchall()
        self.assertEqual((deadline["deadline_on"], deadline["sender_domain"]), (due.isoformat(), "greenhouse-mail.io"))
        self.assertIn(f"by {spelled(due, year=False)}", deadline["quote"])
        looking = datetime.combine(due - timedelta(days=9), datetime.min.time(), tzinfo=timezone.utc).replace(hour=12)
        queue = urgent_queue(self.conn, user_id=USER, days=60, now=looking)
        [item] = [item for item in queue["items"] if item["kind"] == "email_deadline"]
        self.assertEqual((item["date"], item["date_source"], item["application_id"]), (due.isoformat(), "From an email", self.acme))
        self.assertTrue(item["date_note"].startswith("From greenhouse-mail.io, received "))
        self.assertIn("'", item["date_note"])
        [task] = [item for item in queue["items"] if item["kind"] == "task"]
        self.assertEqual(task["origin_label"], "From an email", "the assessment task says where it came from")
        [action] = self.actions(action_type="application.deadline")
        automation.undo(self.conn, action["id"], USER)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM email_deadlines").fetchone()[0], 0, "undo removes the row")

    def test_a_plain_deadline_email_only_proposes(self):
        self.started()
        self.deliver("m-8b", job_mail(subject="Action needed: transcript",
                                      body=f"To complete your application for the Mechanical Engineering Intern role at Acme Robotics, please upload your transcript by {spelled(in_days(30))}."))
        self.pass_once()
        [action] = self.actions()
        self.assertEqual((action["action_type"], action["status"]), ("application.deadline", "proposed"))
        self.assertTrue(any("not sure enough" in reason for reason in action["evidence"]["why_proposal"]))

    def test_a_date_with_no_year_only_proposes(self):
        self.started()
        self.deliver("m-8c", job_mail(sender="HackerRank <support@hackerrankforwork.com>", subject="Acme Robotics has invited you to take a test",
                                      body=f"Acme Robotics invited you to the Mechanical Engineering Intern Coding Test. Please complete the test by {spelled(in_days(21), year=False)}."))
        self.pass_once()
        [deadline] = self.actions(action_type="application.deadline")
        self.assertEqual(deadline["status"], "proposed")
        self.assertIn("the email did not state the year of the deadline", deadline["evidence"]["why_proposal"])

    def test_pause_then_resume_then_the_message_is_acted_on(self):
        self.started()
        automation.set_paused(self.conn, USER, True)
        self.deliver("m-9", acme_confirmation())
        result = self.pass_once()
        self.assertEqual(result["detail"].get("awaiting_resume"), 1)
        self.assertEqual(self.message_row("m-9")["state"], "awaiting_resume")
        self.assertEqual((self.actions(), self.stage(self.acme)[0]), ([], "applying"), "reading goes on; acting waits")
        automation.set_paused(self.conn, USER, False)
        self.pass_once()
        self.assertEqual(self.message_row("m-9")["state"], "done")
        self.assertEqual(self.stage(self.acme)[0], "applied")
        self.assertEqual(len(self.actions()), 1)

    def test_shadow_records_what_it_would_do(self):
        self.started(mode="shadow")
        self.deliver("m-10", acme_confirmation())
        self.pass_once()
        [action] = self.actions()
        self.assertEqual(action["status"], "shadow")
        self.assertEqual(self.stage(self.acme)[0], "applying")

    def test_a_message_outreach_owns_is_left_to_outreach(self):
        self.started()
        self.deliver("m-11", acme_confirmation())
        now = utc_now()
        with self.conn:
            self.conn.execute("INSERT INTO outreach_targets(id, user_id, company, created_at, updated_at) VALUES('t-1', ?, 'Acme Robotics', ?, ?)", (USER, now, now))
            self.conn.execute(
                "INSERT INTO outreach_inbox_messages(user_id, gmail_id, target_id, kind, sender, received_at, recorded_at) VALUES(?, 'm-11', 't-1', 'reply', 'x', ?, ?)",
                (USER, now, now),
            )
        self.pass_once()
        self.assertEqual(self.message_row("m-11")["state"], "outreach")
        self.assertEqual(self.actions(), [])
        self.assertEqual(self.gmail.message_gets, 0, "not even read")

    def test_a_message_that_is_not_from_a_job_system_is_skipped_and_nothing_kept(self):
        self.started()
        self.deliver("m-12", job_mail(sender="A Friend <friend@mail.example.net>", subject="Lunch?", body="Want to grab lunch?", headers=""))
        self.pass_once()
        row = self.message_row("m-12")
        self.assertEqual((row["state"], row["subject"], row["sender_domain"], row["thread_id"]), ("skipped", "", "", ""))
        self.assertEqual(self.actions(), [])

    def test_a_link_host_alone_or_a_forged_sender_only_proposes(self):
        self.started()
        self.deliver("m-13", job_mail(sender="Acme Robotics <news@acme-robotics-news.com>", headers="",
                                      subject="Thank you for applying to Acme Robotics",
                                      body="Thank you for applying to the Mechanical Engineering Intern role at Acme Robotics. https://boards.greenhouse.io/acme/jobs/1234567"))
        self.deliver("m-14", acme_confirmation(headers="Authentication-Results: mx.google.com;\n       spf=fail smtp.mailfrom=x@forged.example\n"))
        self.pass_once()
        statuses = {action["evidence"]["gmail_id"]: action for action in self.actions()}
        self.assertEqual(statuses["m-13"]["status"], "proposed")
        self.assertIn("the sender is not a job system or a company domain you trusted", statuses["m-13"]["evidence"]["why_proposal"])
        self.assertEqual(statuses["m-14"]["status"], "proposed")
        self.assertIn("the sender did not pass Gmail's check", statuses["m-14"]["evidence"]["why_proposal"])
        self.assertEqual(self.stage(self.acme)[0], "applying")
        # A From line Gmail did not vouch for is never named as the sender, even once approved.
        self.assertIn("from an email claiming to be from greenhouse-mail.io (sender not verified)", statuses["m-14"]["summary"])
        payload = json.loads(self.conn.execute("SELECT payload_json FROM monitored_events WHERE external_id='gmail:m-14'").fetchone()[0])
        self.assertIs(payload["sender_verified"], False)

    def test_a_trusted_company_domain_may_act_and_an_untrusted_one_proposes(self):
        # Acme's posting is on its own careers site, which suggests acme.com (never trusted on its own).
        with self.conn:
            self.conn.execute("UPDATE opportunities SET url='https://careers.acme.com/jobs/77' WHERE id='job-a'")
        self.started()
        body = "We have received your application for the Mechanical Engineering Intern role at Acme Robotics."
        self.deliver("m-15", job_mail(sender="Acme Robotics <careers@acme.com>", subject="Application received", body=body))
        self.pass_once()
        [first] = self.actions()
        self.assertEqual(first["status"], "proposed")
        self.assertIn("mail from acme.com is not trusted for Acme Robotics yet", first["evidence"]["why_proposal"])
        suggestion = self.conn.execute("SELECT id, status FROM employer_domains WHERE domain='acme.com'").fetchone()
        self.assertEqual(suggestion["status"], "suggested")
        mail_trust.decide(self.conn, USER, suggestion["id"], "trusted")
        automation.reject(self.conn, first["id"], USER)
        self.deliver("m-16", job_mail(sender="Acme Robotics <careers@acme.com>", subject="Application received", body=body), minutes_ago=1)
        self.pass_once()
        [second] = [action for action in self.actions() if action["evidence"]["gmail_id"] == "m-16"]
        self.assertEqual(second["status"], "applied")
        self.assertEqual(self.stage(self.acme)[0], "applied")

    def test_an_authenticated_email_from_an_unknown_company_domain_is_not_even_read(self):
        self.started()
        self.deliver("m-19", job_mail(sender="Orbit Recruiting <jobs@orbit.io>", subject="Update from Orbit Systems",
                                      body="Hi Sam, we would like to invite you to interview for the Controls Co-op role at Orbit Systems."))
        self.pass_once()
        self.assertEqual(self.message_row("m-19")["state"], "skipped", "only listed senders and known company domains are read")

    def test_an_untracked_role_proposes_a_capture_that_opens_a_draft(self):
        self.started()
        self.deliver("m-17", job_mail(sender="Nimbus Aero <notifications@ashbyhq.com>", subject="Thank you for applying to Nimbus Aero",
                                      body="Thanks for applying to Nimbus Aero for the Flight Software Intern position. https://jobs.ashbyhq.com/nimbus/2f1c3e4a-1111-4222-8333-944455556666?utm_source=x"))
        self.pass_once()
        [proposal] = self.actions()
        self.assertEqual((proposal["action_type"], proposal["status"], proposal["undoable"]), ("application.capture_proposal", "proposed", False))
        self.assertEqual(proposal["after"]["capture"]["company"], "Nimbus Aero")
        self.assertEqual(proposal["summary"], "Looks like you applied to Flight Software Intern at Nimbus Aero. Add it?")
        approved = automation.approve(self.conn, proposal["id"], USER)
        capture_id = approved["after"]["_result"]["capture_id"]
        draft = self.conn.execute("SELECT status, source_url, parsed_json FROM opportunity_captures WHERE id=?", (capture_id,)).fetchone()
        self.assertEqual(draft["status"], "draft", "the student confirms it in the capture form")
        self.assertEqual(draft["source_url"], "https://jobs.ashbyhq.com/nimbus/2f1c3e4a-1111-4222-8333-944455556666")
        with self.assertRaisesRegex(ValueError, "can't be undone"):
            automation.undo(self.conn, proposal["id"], USER)

    def test_a_hedged_rejection_only_proposes(self):
        self.started()
        self.deliver("m-18", orbit_mail("Orbit Systems - Controls Co-op",
                                        "We are not moving forward with your application for the Controls Co-op role at Orbit Systems, "
                                        "but would you be open to us considering you for another role?"))
        self.pass_once()
        [action] = self.actions()
        self.assertEqual((action["status"], action["after"]["stage"]), ("proposed", "rejected"))
        self.assertTrue(any("not sure enough" in reason for reason in action["evidence"]["why_proposal"]))


class CursorTests(MailCase):
    def test_a_crash_after_the_history_fetch_loses_no_ids(self):
        self.started()
        self.deliver("m-20", acme_confirmation())
        self.deliver("m-21", orbit_mail("Your application to Orbit Systems", "Thanks for applying to Orbit Systems."))
        with mock.patch.object(application_inbox, "_drain", side_effect=RuntimeError("the laptop slept")):
            with self.assertRaises(RuntimeError):
                self.pass_once()
        sync = self.sync()
        self.assertEqual(sync["history_id"], str(self.gmail.history_id), "the cursor moved")
        self.assertEqual(json.loads(sync["pending_ids_json"]), ["m-20", "m-21"], "because the ids it covers were stored with it")
        self.pass_once()
        self.assertEqual(json.loads(self.sync()["pending_ids_json"]), [])
        self.assertEqual(self.message_row("m-20")["state"], "done")
        self.assertEqual(self.stage(self.acme)[0], "applied")

    def test_a_throttled_pass_loses_no_ids(self):
        self.started()
        for number in range(3):
            self.deliver(f"m-3{number}", acme_confirmation(), minutes_ago=10 - number)
        self.gmail.throttle_after = 1
        result = self.pass_once()
        self.assertEqual(result["state"], "throttled")
        self.assertEqual(json.loads(self.sync()["pending_ids_json"]), ["m-31", "m-32"])
        # Gmail's hold is over.
        outreach_gmail._BACKOFF.clear()
        with self.conn:
            self.conn.execute("UPDATE connector_accounts SET backoff_until=NULL")
        self.gmail.throttle_after = None
        self.assertEqual(self.pass_once()["state"], "ok")
        self.assertEqual({row["gmail_id"] for row in self.conn.execute("SELECT gmail_id FROM application_mail_messages WHERE state='done'")},
                         {"m-30", "m-31", "m-32"})

    def test_an_expired_cursor_recovers_by_searching_since_the_last_good_pass(self):
        self.started()
        with self.conn:
            self.conn.execute("UPDATE application_mail_sync SET last_ok_at=?", ((now_utc() - timedelta(hours=3)).isoformat(),))
        received = now_utc() - timedelta(minutes=20)
        self.gmail.store("m-40", acme_confirmation(), received)
        self.gmail.recovery_found = ["m-40"]
        self.gmail.history_expired = True
        self.gmail.history_id = 555
        result = self.pass_once()
        self.assertEqual(result["state"], "ok", result)
        [query] = [search for search in self.gmail.searches if search.startswith("in:inbox after:")]
        after = int(query.rsplit(":", 1)[1])
        enabled = automation._parse(self.sync()["enabled_at"])
        self.assertEqual(after, int(enabled.timestamp()), "a day before the last good pass, but never before the switch was turned on")
        self.assertEqual(self.sync()["history_id"], "555", "live reading goes on from the cursor taken before the search")
        self.assertEqual(self.sync()["recovery_state"], "")
        self.assertEqual(self.message_row("m-40")["state"], "done")

    def test_at_most_fifty_messages_are_read_in_a_pass(self):
        self.started()
        for number in range(55):
            self.deliver(f"q-{number:02d}", job_mail(sender="Someone <someone@us.greenhouse-mail.io>", subject="Hello", body="Hi."))
        self.pass_once()
        self.assertEqual(self.gmail.message_gets, 50)
        self.assertEqual(len(json.loads(self.sync()["pending_ids_json"])), 5, "the rest wait for the next pass")
        self.pass_once()
        self.assertEqual(json.loads(self.sync()["pending_ids_json"]), [])

    def test_passes_are_ten_minutes_apart_unless_forced(self):
        self.started()
        requests = len(self.gmail.requests)
        result = application_inbox.run_pass(self.conn, user_id=USER, client_factory=self.factory)
        self.assertTrue(result["skipped"])
        self.assertEqual(len(self.gmail.requests), requests)

    def test_switching_off_forgets_the_cursor(self):
        self.started()
        application_inbox.note_off(self.conn, USER)
        sync = self.sync()
        self.assertEqual((sync["history_id"], sync["enabled_at"], sync["backfill_state"]), ("", None, ""))


class BackfillTests(MailCase):
    def test_the_backfill_query_has_a_term_for_every_live_criterion(self):
        enabled = now_utc()
        query = application_inbox.backfill_query(self.conn, USER, enabled, enabled)
        lists = mail_trust.sender_lists()
        for category in mail_trust.READ_CATEGORIES:
            for domain in lists[category]:
                with self.subTest(domain=domain):
                    self.assertIn(domain, query.split("from:(", 1)[1].split(")", 1)[0])
                    self.assertIn(f'"{domain}"', query)
        with self.conn:
            mail_trust.suggest(self.conn, USER, company="Acme Robotics", host="acme.com", source="test", evidence="test")
        self.assertIn("acme.com", application_inbox.backfill_query(self.conn, USER, enabled, enabled).split("from:(", 1)[1].split(")", 1)[0])
        self.assertIn(f"after:{int((enabled - timedelta(days=60)).timestamp())}", query)
        self.assertIn(f"before:{int(enabled.timestamp())}", query)

    def test_the_backfill_only_ever_proposes(self):
        received = now_utc() - timedelta(days=10)
        self.gmail.store("b-1", acme_confirmation(date=received), received)
        self.gmail.backfill_found = ["b-1"]
        self.switch("on")
        self.pass_once()
        [action] = self.actions()
        self.assertEqual(action["status"], "proposed")
        self.assertIn("it arrived before you turned this on", action["evidence"]["why_proposal"])
        self.assertIn(application_inbox.BACKFILL_BASIS, action["basis"])
        self.assertEqual(self.stage(self.acme)[0], "applying")
        self.assertEqual(self.message_row("b-1")["origin"], "backfill")
        self.assertEqual(application_inbox.status(self.conn, USER)["backfill_found"], 1)
        counts = application_inbox.approve_backfill(self.conn, USER)
        self.assertEqual(counts, {"approved": 1, "superseded": 0, "left": 0})
        self.assertEqual(self.stage(self.acme)[0], "applied")
        self.assertEqual(self.sync()["backfill_state"], "running")
        self.pass_once()
        self.assertEqual(self.sync()["backfill_state"], "done")

    def test_the_backfill_resumes_from_its_page_token(self):
        self.gmail.list_page_size = 2
        for number in range(5):
            received = now_utc() - timedelta(days=5, minutes=number)
            self.gmail.store(f"b-{number}", job_mail(sender="Someone <someone@us.greenhouse-mail.io>", subject="Hi", body="Hi."), received)
        self.gmail.backfill_found = [f"b-{number}" for number in range(5)]
        self.switch("on")
        self.pass_once()
        self.assertEqual(self.sync()["backfill_page_token"], "2")
        self.pass_once()
        self.pass_once()
        self.pass_once()
        self.assertEqual(self.sync()["backfill_state"], "done")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM application_mail_messages WHERE origin='backfill'").fetchone()[0], 5)


class DecisionTests(MailCase):
    def proposed_interview(self):
        self.started()
        self.deliver("m-50", orbit_mail("Update from Orbit Systems", "We'd like to invite you to interview. Reply with times."))
        self.pass_once()
        return self.actions(status="proposed")

    def test_confirming_the_email_card_approves_its_proposals_for_the_application_picked(self):
        proposals = self.proposed_interview()
        self.assertTrue(proposals)
        event = self.conn.execute("SELECT id FROM monitored_events WHERE external_id='gmail:m-50'").fetchone()
        from opportunity_app.connections import decide_monitored_event

        decided = decide_monitored_event(self.conn, event["id"], "confirm", self.acme, user_id=USER)
        self.assertEqual((decided["status"], decided["application_id"], decided["decided_by"]), ("confirmed", self.acme, "student"))
        self.assertEqual(self.stage(self.acme)[0], "interview", "the application the student picked, corrected from Orbit")
        self.assertEqual(self.stage(self.orbit)[0], "applied")
        self.assertEqual(self.actions(status="proposed"), [])
        corrected = [action for action in self.actions() if action["action_type"] == "application.stage"][0]
        self.assertEqual((corrected["subject_id"], corrected["evidence"]["corrected_from"]), (self.acme, self.orbit))
        self.assertEqual(self.message_row("m-50")["application_id"], self.acme)

    def test_ignoring_the_email_card_sets_its_proposals_aside_without_the_breaker(self):
        self.proposed_interview()
        event = self.conn.execute("SELECT id FROM monitored_events WHERE external_id='gmail:m-50'").fetchone()
        from opportunity_app.connections import decide_monitored_event

        decide_monitored_event(self.conn, event["id"], "ignore", None, user_id=USER)
        statuses = {action["action_type"]: action["status"] for action in self.actions()}
        # The task was allowed on its own (the company alone matched); the stage change waited and is set aside.
        self.assertEqual(statuses, {"application.stage": "expired", "application.task": "applied"})
        self.assertEqual(automation.mode(self.conn, USER, FEATURE), "on", "ignoring an email is not the feature being wrong")

    def test_deciding_every_proposal_in_the_queue_settles_the_email_card(self):
        proposals = self.proposed_interview()
        for action in proposals:
            result = automation.approve(self.conn, action["id"], USER)
            application_inbox.after_decision(self.conn, USER, result)
        event = self.conn.execute("SELECT status, decided_by FROM monitored_events WHERE external_id='gmail:m-50'").fetchone()
        self.assertEqual(tuple(event), ("confirmed", "student"))
        self.assertEqual(self.message_row("m-50")["matched_by"], "student", "the student approved it, so the link is no longer a guess")


class RetentionTests(MailCase):
    def test_old_excerpts_are_purged_and_the_hash_stays(self):
        self.started()
        self.deliver("m-60", acme_confirmation())
        self.pass_once()
        old = (now_utc() - timedelta(days=200)).isoformat()
        with self.conn:
            self.conn.execute("UPDATE automation_actions SET created_at=?", (old,))
            self.conn.execute("UPDATE monitored_events SET created_at=?", (old,))
        from opportunity_app.operations import run_retention

        counts = run_retention(self.conn)
        self.assertEqual((counts["mail_excerpts_removed"], counts["mail_previews_removed"]), (1, 1))
        [action] = self.actions()
        self.assertEqual(action["evidence"]["excerpt"], "")
        self.assertTrue(action["evidence"]["text_sha256"])
        payload = json.loads(self.conn.execute("SELECT payload_json FROM monitored_events").fetchone()[0])
        self.assertEqual(payload["body_preview"], "")


class WatcherTests(MailCase):
    def test_the_watcher_runs_it_as_a_fourth_step_only_when_it_is_not_off(self):
        from opportunity_app.outreach_inbox import InboxWatcher

        watcher = InboxWatcher(self.platform_path, client_factory=self.factory, decisions_for=lambda conn, user_id: None)
        with mock.patch.object(application_inbox, "run_pass", return_value={"state": "ok", "detail": {"read": 0}}) as step:
            watcher.run_once()
            step.assert_not_called()
            self.switch("shadow")
            watcher.run_once()
            step.assert_called_once()
        components = {row["component"] for row in self.conn.execute("SELECT component FROM automation_health WHERE user_id=?", (USER,))}
        self.assertIn("inbox.applications", components)


class MigrationTests(unittest.TestCase):
    def test_a_half_applied_0038_is_repaired_by_running_it_again(self):
        from opportunity_app import schema

        migrations = Path(__file__).resolve().parent.parent / "migrations"
        with tempfile.TemporaryDirectory() as directory:
            conn = connect_product(Path(directory) / "platform.db")
            try:
                conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)")
                for migration in sorted(migrations.glob("[0-9][0-9][0-9][0-9]_*.sql")):
                    if migration.name >= "0038":
                        break
                    sql = migration.read_text(encoding="utf-8")
                    step = schema._MIGRATION_STEPS.get(migration.name)
                    step(conn, sql) if step else conn.executescript(sql)
                    conn.execute("INSERT INTO schema_migrations(name, applied_at) VALUES(?, ?)", (migration.name, utc_now()))
                conn.commit()
                # The crash: one column added, the rest (and the marker) not.
                conn.execute("ALTER TABLE application_tasks ADD COLUMN link TEXT NOT NULL DEFAULT ''")
                conn.commit()
                schema.ensure_product_schema(conn)
                for table, column in (("application_tasks", "link"), ("monitored_events", "decided_by")):
                    self.assertTrue(schema._has_column(conn, table, column), f"{table}.{column}")
                for table in ("application_mail_sync", "application_mail_messages", "email_deadlines", "employer_domains", "automation_held"):
                    self.assertIsNotNone(conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone(), table)
                self.assertIsNotNone(conn.execute("SELECT 1 FROM schema_migrations WHERE name='0038_application_mail.sql'").fetchone())
            finally:
                conn.close()


class ApiTests(MailCase):
    def setUp(self):
        super().setUp()
        root = Path(self.tempdir.name)
        app = create_app(
            db_path=self.platform_path, access_token="mail-owner", static_dir=STATIC_DIR,
            resume_storage=root / "resumes", capture_storage=root / "captures", interview_storage=root / "interviews",
            outreach_gmail_client_factory=self.factory,
        )
        self.client = TestClient(app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def test_employer_domains_are_suggested_trusted_and_dismissed(self):
        with self.conn:
            self.conn.execute("UPDATE opportunities SET url='https://careers.acme-robotics.com/jobs/77' WHERE id='job-a'")
        listed = self.client.get("/api/v1/automation/employer-domains", headers=AUTH).json()
        [item] = listed["items"]
        self.assertEqual((item["domain"], item["status"], item["company"], item["source"]), ("acme-robotics.com", "suggested", "Acme Robotics", "job_url"))
        self.assertIn("acme-robotics.com", item["evidence"])
        trusted = self.client.post(f"/api/v1/automation/employer-domains/{item['id']}/trust", headers=AUTH)
        self.assertEqual(trusted.json()["status"], "trusted")
        self.assertEqual(mail_trust.trusted_for(self.conn, USER, mail_trust.company_key("Acme Robotics")), {"acme-robotics.com"})
        # Stop trusting: a suggestion again, so its mail is still read and only proposes.
        untrusted = self.client.post(f"/api/v1/automation/employer-domains/{item['id']}/untrust", headers=AUTH)
        self.assertEqual((untrusted.json()["status"], untrusted.json()["confirmed_at"]), ("suggested", None))
        self.assertEqual(mail_trust.trusted_for(self.conn, USER, mail_trust.company_key("Acme Robotics")), set())
        self.assertIn("acme-robotics.com", mail_trust.known_domains(self.conn, USER))
        self.assertEqual([row["status"] for row in self.client.get("/api/v1/automation/employer-domains", headers=AUTH).json()["items"]], ["suggested"])
        dismissed = self.client.post(f"/api/v1/automation/employer-domains/{item['id']}/dismiss", headers=AUTH)
        self.assertEqual(dismissed.json()["status"], "dismissed")
        self.assertEqual(self.client.get("/api/v1/automation/employer-domains", headers=AUTH).json()["items"], [], "a dismissal is not suggested again")
        self.assertEqual(self.client.post("/api/v1/automation/employer-domains/nope/trust", headers=AUTH).status_code, 404)

    def test_a_waiting_task_proposal_shows_its_link_host_only_and_the_export_leaves_the_link_out(self):
        self.started()
        link = "https://www.hackerrank.com/test/api555/login?token=api555-secret"
        self.deliver("m-158", job_mail(
            sender="HackerRank <support@hackerrankforwork.com>", subject="Acme Robotics has invited you to take a test", headers="",
            body=f"Acme Robotics invited you to the Mechanical Engineering Intern Coding Test. Please complete it by {spelled(in_days(30))}.\n\n{link}",
        ))
        self.pass_once()
        listed = self.client.get("/api/v1/automation/actions?status=proposed", headers=AUTH)
        [task] = [item for item in listed.json()["items"] if item["action_type"] == "application.task"]
        self.assertEqual(task["after"]["task"]["link_host"], "www.hackerrank.com")
        self.assertNotIn("api555", listed.text)
        exported = self.client.get("/api/v1/account/export", headers=AUTH)
        self.assertEqual(exported.status_code, 200)
        self.assertNotIn("api555", exported.text)
        approved = self.client.post(f"/api/v1/automation/actions/{task['id']}/approve", headers=AUTH)
        self.assertEqual(approved.status_code, 200, approved.text)
        self.assertNotIn("api555", approved.text)
        [row] = self.client.get(f"/api/v1/applications/{self.acme}", headers=AUTH).json()["tasks"]
        self.assertEqual(row["link"], link, "the task itself has the whole link")

    def test_a_shared_job_host_is_never_suggested(self):
        # Both seeded postings live on example.com, and reserved domains belong to no employer.
        self.assertEqual(self.client.get("/api/v1/automation/employer-domains", headers=AUTH).json()["items"], [])

    def test_approve_takes_a_corrected_application(self):
        self.switch("on")
        row = automation.perform(
            self.conn, user_id=USER, feature=FEATURE, action_type="application.stage", subject_kind="application", subject_id=self.orbit,
            after={"stage": "interview"}, evidence={"gmail_id": "m-70"}, summary="Orbit Systems to interview", basis="test", confidence=0.9,
            idempotency_key="gmail:m-70:app-job-b:application.stage", auto=False,
        )
        response = self.client.post(f"/api/v1/automation/actions/{row['id']}/approve", headers=AUTH, json={"subject_id": self.acme})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual((body["status"], body["subject_id"], body["evidence"]["corrected_from"]), ("applied", self.acme, self.orbit))
        self.assertEqual(body["before"], {"stage": "applying", "applied_at": None}, "the chosen application's own before")
        self.assertIn("different application", body["note"])
        self.assertEqual((self.stage(self.acme)[0], self.stage(self.orbit)[0]), ("interview", "applied"))
        automation.undo(self.conn, row["id"], USER)
        self.assertEqual(self.stage(self.acme)[0], "applying", "undo restores the corrected application")

    def test_approve_refuses_an_application_that_is_not_the_students(self):
        self.switch("on")
        row = automation.perform(
            self.conn, user_id=USER, feature=FEATURE, action_type="application.stage", subject_kind="application", subject_id=self.orbit,
            after={"stage": "interview"}, evidence={}, summary="x", basis="test", confidence=0.9, idempotency_key="k-71", auto=False,
        )
        response = self.client.post(f"/api/v1/automation/actions/{row['id']}/approve", headers=AUTH, json={"subject_id": "app-nobody"})
        self.assertEqual((response.status_code, response.json()["detail"]), (404, "Application not found"))
        self.assertEqual(self.actions()[0]["status"], "proposed", "nothing was decided")
        plain = self.client.post(f"/api/v1/automation/actions/{row['id']}/approve", headers=AUTH)
        self.assertEqual(plain.json()["subject_id"], self.orbit, "no body approves as proposed")

    def test_the_application_lists_its_emails_with_a_gmail_link(self):
        self.started()
        self.deliver("m-72", acme_confirmation(), thread_id="18c0ffee")
        self.pass_once()
        detail = self.client.get(f"/api/v1/applications/{self.acme}", headers=AUTH).json()
        [email_row] = detail["emails"]
        self.assertEqual(email_row["subject"], "Thank you for applying to Acme Robotics")
        self.assertEqual(email_row["sender_domain"], "greenhouse-mail.io")
        self.assertEqual(email_row["gmail_url"], "https://mail.google.com/mail/u/0/#all/18c0ffee")

    def test_the_overview_names_new_automatic_changes_for_the_page_to_announce(self):
        self.switch("on")
        common = dict(user_id=USER, feature=FEATURE, action_type="application.stage", subject_kind="application",
                      evidence={}, basis="test", confidence=0.9)
        automation.perform(self.conn, subject_id=self.orbit, after={"stage": "interview"}, summary="Orbit Systems to interview",
                           idempotency_key="k-auto", auto=True, **common)
        automation.perform(self.conn, subject_id=self.acme, after={"stage": "applied"}, summary="Acme Robotics to applied",
                           idempotency_key="k-proposed", auto=False, **common)
        health = self.client.get("/api/v1/automation", headers=AUTH).json()["health"]
        [item] = health["recent_applied"]
        self.assertEqual(health["recent_applied_total"], 1, "so the page can say 'at least' when the list was cut short")
        self.assertEqual((item["summary"], item["action_type"], item["undoable"]), ("Orbit Systems to interview", "application.stage", True))
        listed = self.client.get("/api/v1/automation/actions?status=proposed", headers=AUTH).json()["items"]
        self.assertEqual([action["undoable"] for action in listed], [True])

    def test_the_overview_reports_the_switch_and_a_check_runs_a_pass(self):
        self.switch("on")
        overview = self.client.get("/api/v1/automation", headers=AUTH).json()
        self.assertEqual(overview["application_mail"]["mode"], "on")
        checked = self.client.post("/api/v1/automation/application-mail/check", headers=AUTH).json()
        self.assertEqual(checked["state"], "ok")
        self.assertEqual(checked["status"]["enabled_at"] is not None, True)
        off = self.client.put("/api/v1/automation/settings", headers=AUTH, json={"modes": {FEATURE: "off"}})
        self.assertEqual(off.status_code, 200, off.text)
        self.assertEqual(self.sync()["history_id"], "", "switching off forgets the cursor")


# --- Review fixes: what an email says, and what it may do ---------------------------------------------


class ConfirmationWordingTests(unittest.TestCase):
    """What an ordinary confirmation says about the process is not news that it happened."""

    CONFIRMATIONS = (
        "We are reviewing your application along with other candidates and will be in touch soon.",
        "If we are not moving forward, we will let you know.",
        "If you are not selected for this role, we will keep your resume on file.",
        "Applications are reviewed on a rolling basis until the position has been filled.",
        "We may not be able to move forward with all applicants, but we read every application.",
        "If your background is a fit we will reach out to schedule an interview.",
        "We review every application and invite the strongest candidates to interview.",
        "We will invite selected candidates for an interview.",
        "If your background is a match, a recruiter will reach out to schedule a call.",
        # A request or an invitation that waits on being chosen, or is made conditional after it.
        "If you are selected for the next round, we would like to schedule an interview with you.",
        "If selected, please book a time using the link below.",
        "We will invite you to interview if your application is selected.",
        "Should you be selected, we will contact you to schedule an interview.",
        "Once we have reviewed your application, we will be in touch to schedule an interview.",
        "If there is no match, we will not be moving forward with your application.",
        "In the event that the position has been filled, we will let you know.",
    )

    def test_a_confirmation_that_mentions_the_process_stays_a_confirmation(self):
        subject = "Thank you for applying to Acme Robotics"
        for sentence in self.CONFIRMATIONS:
            with self.subTest(sentence=sentence):
                body = (f"Hi Sam,\n\nThank you for applying to the Mechanical Engineering Intern role at Acme Robotics. "
                        f"We have received your application. {sentence}")
                # The connector's email cards read the same rules, so this pins their label too.
                self.assertEqual(classify_monitored_message(subject, body), ("application_confirmation", 0.9))
                self.assertEqual(application_inbox.classify_rules(subject, body, "us.greenhouse-mail.io", [])[0], "application_confirmation")

    def test_a_definite_rejection_invitation_or_offer_still_counts(self):
        cases = {
            "We regret to inform you that we will not be moving forward with your application.": "rejected",
            "After careful review, we have decided to move forward with other candidates.": "rejected",
            "Unfortunately, the position has been filled.": "rejected",
            "You have not been selected for the Controls Co-op role.": "rejected",
            "We'd like to invite you to interview for the Controls Co-op role.": "interview",
            "We are happy to offer you an interview slot next week for the Controls Intern role.": "interview",
            "We are pleased to offer you the Controls Co-op role.": "offer",
            "On behalf of Quill Labs, I am happy to offer you a position as Hardware Intern.": "offer",
        }
        for body, label in cases.items():
            with self.subTest(body=body):
                self.assertEqual(classify_monitored_message("An update", body)[0], label)

    def test_offering_an_interview_is_never_an_offer(self):
        for body in ("Hi Sam, we are happy to offer you an interview slot next week for the Controls Intern role",
                     "We are pleased to offer you a phone screen for the role.",
                     "We are pleased to offer you the opportunity to interview for the Software Intern position."):
            with self.subTest(body=body):
                self.assertNotEqual(classify_monitored_message("", body)[0], "offer")
        self.assertEqual(classify_monitored_message("", "We are glad to offer you feedback on your application.")[0], "unknown")

    # Real news whose sentence also holds a word the first hedge read as a condition: a modal in another
    # clause, "once again", May the month, or a condition that sets up a request rather than a selection.
    # Each was read right before the hedge and unknown (no proposal, no card) with it.
    REAL_NEWS = {
        "Unfortunately, we have decided to proceed with other candidates for this role.": "rejected",
        "After careful consideration, we have decided to go with other candidates.": "rejected",
        "The team has chosen to move ahead with other candidates.": "rejected",
        "We have identified other candidates whose qualifications better match our needs.": "rejected",
        "We know this may be disappointing, but we will not be moving forward with your application.": "rejected",
        "Once again, we regret to inform you that the role has been filled.": "rejected",
        "Thank you for interviewing with us on May 5; we regret to inform you that we will not be moving forward.": "rejected",
        "Thank you for applying in May. We regret to inform you that we will not be moving forward.": "rejected",
        "If you're available, we would like to schedule an interview with you this week.": "interview",
        "If you\u2019re available, we\u2019d like to schedule an interview with you this week.": "interview",
        "Following your application in May, we would like to invite you to interview for the Controls Intern role.": "interview",
        "Congratulations! We will invite you to an onsite interview next week to meet the team.": "interview",
        "Of all the qualified applicants, we'd like to invite you to interview.": "interview",
        "If you are still interested, please let us know your availability for a call next week.": "scheduling",
        "If it works for you, please schedule a time with the team using the link below.": "scheduling",
        "If still interested, let us know your availability for a call next week.": "scheduling",
        "You may now book a time with the team using the link below.": "scheduling",
        "You have until October 5 to complete the online assessment.": "assessment",
        "We are pleased to offer you the opportunity to join Acme Robotics as a Software Engineering Intern this summer.": "offer",
        "We are pleased to offer the Software Intern position to you.": "offer",
    }

    def test_real_news_is_not_hedged_away(self):
        for body, label in self.REAL_NEWS.items():
            with self.subTest(body=body):
                self.assertEqual(classify_monitored_message("An update", body)[0], label)
                self.assertEqual(application_inbox.classify_rules("An update", body, "us.greenhouse-mail.io", [])[0], label)

    def test_an_offer_always_gets_its_card(self):
        # C2: an offer gets a notice and a card, so the wording the old rule caught still counts.
        for body in ("We are pleased to offer you the opportunity to join Acme Robotics as a Software Engineering Intern this summer.",
                     "We are pleased to offer the Software Intern position to you."):
            with self.subTest(body=body):
                self.assertEqual(classify_monitored_message("Your offer from Acme Robotics", body), ("offer", 0.95))


class StatedDateTests(unittest.TestCase):
    RECEIVED = datetime(2026, 9, 27, 12, tzinfo=timezone.utc)

    def found(self, text):
        return application_inbox.stated_deadline(text, self.RECEIVED)

    def test_only_a_date_right_after_its_cue_is_a_deadline(self):
        both = self.found("This invitation was sent by the Acme recruiting team on September 28, 2026. "
                          "Please complete the test before October 5, 2026.")
        self.assertEqual((both.on, both.doubts), (date(2026, 10, 5), ()), "the day it was sent is not the deadline")
        self.assertIsNone(self.found("This test was posted by Acme on October 1, 2026."))
        for text, day in (("Your questions are due by Friday, October 2.", date(2026, 10, 2)),
                          ("Please complete it by 11:59 PM PT on October 3, 2026.", date(2026, 10, 3)),
                          ("Take the test here before it expires on October 5.", date(2026, 10, 5)),
                          ("The deadline to return it is October 14, 2026.", date(2026, 10, 14))):
            with self.subTest(text=text):
                self.assertEqual(self.found(text).on, day)

    def test_more_than_one_date_is_a_doubt(self):
        found = self.found("Complete the test by October 5, 2026. Your references are due by October 20, 2026.")
        self.assertEqual(found.on, date(2026, 10, 5))
        self.assertIn(application_inbox.MANY_DATES, found.doubts)

    def test_a_numeric_date_that_reads_two_ways_is_a_doubt(self):
        found = self.found("Please complete the assessment by 10/11/2026.")
        self.assertEqual(found.on, date(2026, 10, 11))
        self.assertIn(application_inbox.TWO_READINGS, found.doubts)
        self.assertEqual(self.found("Please complete the assessment by 13/10/2026.").on, date(2026, 10, 13), "only day/month reads")
        self.assertEqual(self.found("Please complete the assessment by 10/10/2026.").doubts, (), "the same either way")


class ReviewFixAuthenticationTests(unittest.TestCase):
    def check(self, raw):
        return mail_trust.authenticate(mail_from(raw).message)

    def test_a_failed_dmarc_is_never_overridden_by_aligned_dkim(self):
        headers = ("Authentication-Results: mx.google.com;\n       dkim=pass header.i=@greenhouse-mail.io header.s=s1 header.b=abc;\n"
                   "       dmarc=fail (p=NONE sp=NONE dis=NONE) header.from=us.greenhouse-mail.io\n")
        self.assertEqual(self.check(acme_confirmation(headers=headers)).reason, "Gmail's DMARC check failed")
        self.assertEqual(self.check(acme_confirmation(headers=headers.replace("dmarc=fail", "dmarc=temperror"))).reason,
                         "Gmail could not complete its DMARC check")
        no_policy = self.check(acme_confirmation(headers=headers.replace("dmarc=fail", "dmarc=none")))
        self.assertEqual((no_policy.ok, no_policy.method), (True, "dkim"), "with no DMARC policy, aligned DKIM still counts")

    def test_without_the_package_the_reason_says_so(self):
        with mock.patch.object(mail_trust, "_PSL", None), mock.patch.object(mail_trust, "_PSL_MISSING", True):
            result = self.check(acme_confirmation())
        self.assertEqual((result.ok, result.reason), (False, mail_trust.NO_DOMAIN_CHECK))


class ReviewFixMailTests(MailCase):
    def test_a_confirmation_that_mentions_other_candidates_or_a_future_interview_only_confirms(self):
        self.started()
        sentences = (
            "We are reviewing your application along with other candidates and will be in touch soon.",
            "If your background is a fit we will reach out to schedule an interview.",
            "If your background is a match, a recruiter will reach out to schedule a call.",
            "Applications are reviewed on a rolling basis until the position has been filled.",
        )
        for number, sentence in enumerate(sentences):
            self.deliver(f"m-c{number}", job_mail(
                subject="Thank you for applying to Acme Robotics",
                body=f"Thank you for applying to the Mechanical Engineering Intern role at Acme Robotics. We have received your application. {sentence}",
            ), minutes_ago=10 - number)
        self.pass_once()
        self.assertEqual(self.stage(self.acme)[0], "applied", "never rejected or moved to interview")
        self.assertEqual({(action["action_type"], action["after"].get("stage")) for action in self.actions()}, {("application.stage", "applied")})
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM application_tasks WHERE application_id=?", (self.acme,)).fetchone()[0], 0)

    def test_real_news_worded_around_a_hedge_word_gets_its_card_and_its_change(self):
        self.started()
        self.deliver("m-h1", job_mail(
            subject="An update on your Acme Robotics application",
            body="Hi Sam,\n\nThank you for your interest in the Mechanical Engineering Intern role at Acme Robotics. "
                 "We know this may be disappointing, but we have decided to proceed with other candidates.",
        ), minutes_ago=9)
        self.deliver("m-h2", orbit_mail(
            "Next steps with Orbit Systems",
            "Hi Sam,\n\nFollowing your application in May, if you're available, we would like to schedule an interview with you this week.",
        ), minutes_ago=8)
        self.deliver("m-h3", job_mail(
            sender="Nimbus Aero <notifications@ashbyhq.com>", subject="Welcome to Nimbus Aero",
            body="Hi Sam,\n\nWe are pleased to offer you the opportunity to join Nimbus Aero as a Flight Software Intern this summer.",
        ), minutes_ago=7)
        self.pass_once()
        events = dict(self.conn.execute("SELECT external_id, event_type FROM monitored_events WHERE external_id LIKE 'gmail:m-h%'").fetchall())
        self.assertEqual(events, {"gmail:m-h1": "rejected", "gmail:m-h2": "interview", "gmail:m-h3": "offer"}, "each gets its card")
        changes = {(action["evidence"]["gmail_id"], action["action_type"], action["after"].get("stage") or action["after"].get("task", {}).get("title"))
                   for action in self.actions()}
        self.assertLessEqual({("m-h1", "application.stage", "rejected"), ("m-h2", "application.stage", "interview"),
                              ("m-h2", "application.task", "Schedule interview")}, changes)
        self.assertEqual([notice["title"] for notice in automation.list_notices(self.conn, USER)], ["Nimbus Aero may have sent an offer"])

    def test_rejecting_every_proposal_from_one_email_counts_once_for_the_breaker(self):
        self.started()
        invite = "We'd like to invite you to interview for the Controls Co-op role at Orbit Systems."
        self.deliver("m-80", orbit_mail("Interview invitation: Orbit Systems", invite, headers=""), minutes_ago=5)
        self.pass_once()
        proposals = self.actions(status="proposed")
        self.assertEqual({action["action_type"] for action in proposals}, {"application.stage", "application.task"})
        results = [automation.reject(self.conn, action["id"], USER) for action in proposals]
        self.assertEqual([result["feature_paused"] for result in results], [False, False])
        self.assertEqual(automation.mode(self.conn, USER, FEATURE), "on", "one misread email is one strike")
        self.deliver("m-81", orbit_mail("Interview invitation: Orbit Systems", invite, headers=""), minutes_ago=1)
        self.pass_once()
        second = self.actions(status="proposed")
        tripped = automation.reject(self.conn, second[0]["id"], USER)
        self.assertTrue(tripped["feature_paused"], "a second email turned down is the second strike")
        self.assertIn("changes from 2 of its last 2 emails", tripped["breaker_notice"]["title"])

    def test_turning_down_roles_that_are_not_tracked_never_trips_the_breaker(self):
        self.started()
        for number, company in enumerate(("Nimbus Aero", "Vega Dynamics", "Quill Labs")):
            self.deliver(f"m-8{5 + number}", job_mail(sender=f"{company} <notifications@ashbyhq.com>", subject=f"Thank you for applying to {company}",
                                                      body=f"Thanks for applying to {company} for the Flight Software Intern position."),
                         minutes_ago=10 - number)
        self.pass_once()
        captures = self.actions(action_type="application.capture_proposal")
        self.assertEqual(len(captures), 3)
        for action in captures:
            self.assertFalse(automation.reject(self.conn, action["id"], USER)["feature_paused"])
        self.assertEqual(automation.mode(self.conn, USER, FEATURE), "on")

    def test_an_offer_for_a_closed_application_is_proposed_with_a_notice_and_a_card(self):
        self.started()
        update_application(self.conn, self.orbit, stage="archived", user_id=USER)
        with self.conn:
            self.conn.execute("UPDATE application_events SET created_at=? WHERE application_id=? AND event_type='stage_changed'",
                              ((now_utc() - timedelta(days=1)).isoformat(), self.orbit))
        self.deliver("m-90", orbit_mail("Offer letter - Controls Co-op", "Congratulations! We are pleased to offer you the Controls Co-op role at Orbit Systems."))
        self.pass_once()
        [action] = self.actions()
        self.assertEqual((action["status"], action["after"]["stage"]), ("proposed", "offer"))
        self.assertIn("the application is archived", action["evidence"]["why_proposal"])
        [notice] = automation.list_notices(self.conn, USER)
        self.assertEqual(notice["title"], "Orbit Systems may have sent an offer")
        event = self.conn.execute("SELECT event_type, status FROM monitored_events WHERE external_id='gmail:m-90'").fetchone()
        self.assertEqual(tuple(event), ("offer", "pending"))
        self.assertEqual(self.stage(self.orbit)[0], "archived")

    def test_an_offer_no_application_matches_gets_a_notice_and_a_card(self):
        self.started()
        self.deliver("m-91", job_mail(sender="Nimbus Aero <notifications@ashbyhq.com>", subject="Offer letter - Flight Software Intern",
                                      body="Congratulations! We are pleased to offer you the Flight Software Intern position at Nimbus Aero."))
        self.pass_once()
        [notice] = automation.list_notices(self.conn, USER)
        self.assertEqual(notice["title"], "Nimbus Aero may have sent an offer")
        event = self.conn.execute("SELECT event_type, status FROM monitored_events WHERE external_id='gmail:m-91'").fetchone()
        self.assertEqual(tuple(event), ("offer", "pending"))

    def test_switching_off_while_a_pass_reads_never_writes_the_cursor_back(self):
        self.started()
        self.deliver("m-100", acme_confirmation())
        other = connect_product(self.platform_path)
        self.addCleanup(other.close)
        handler = self.gmail.handler

        def switched_off_meanwhile(request):
            if request.url.path.endswith("/history"):
                # The student turns it off from another request while Gmail is answering this one.
                with other:
                    other.execute("UPDATE user_settings SET value='off', updated_at=? WHERE user_id=? AND key=?", (utc_now(), USER, FEATURE))
                application_inbox.note_off(other, USER)
            return handler(request)

        self.gmail.handler = switched_off_meanwhile
        result = self.pass_once()
        self.assertEqual(result["state"], "off")
        sync = self.sync()
        self.assertEqual((sync["history_id"], sync["enabled_at"], json.loads(sync["pending_ids_json"])), ("", None, []))
        self.assertIsNone(self.message_row("m-100"), "not recorded as read while the switch was off")
        del self.gmail.handler
        self.switch("on")
        self.pass_once()
        self.assertIsNotNone(self.sync()["enabled_at"], "turned on again, it starts afresh")

    def test_the_watcher_leaves_the_cursor_alone_when_it_cannot_read_the_switch(self):
        from opportunity_app import outreach_inbox

        self.started()
        watcher = outreach_inbox.InboxWatcher(self.platform_path, client_factory=self.factory, decisions_for=lambda conn, user_id: None)
        real_mode = automation.mode

        def unreadable(conn, user_id, key):
            if key == FEATURE:
                raise sqlite3.OperationalError("database is locked")
            return real_mode(conn, user_id, key)

        with mock.patch.object(outreach_inbox.automation, "mode", side_effect=unreadable):
            watcher.run_once()
        self.assertEqual(self.sync()["history_id"], "100", "not knowing the switch is not the switch being off")

    def test_a_busy_database_stops_the_pass_and_keeps_the_message_queued(self):
        self.started()
        self.deliver("m-110", acme_confirmation())
        real = automation.perform
        calls = []

        def locked_once(*args, **kwargs):
            calls.append(kwargs.get("action_type"))
            if len(calls) == 1:
                raise sqlite3.OperationalError("database is locked")
            return real(*args, **kwargs)

        with mock.patch.object(application_inbox.automation, "perform", side_effect=locked_once):
            result = self.pass_once()
        self.assertEqual(result["state"], "database_busy")
        self.assertIsNone(self.message_row("m-110"), "not set aside as an error")
        self.assertEqual(json.loads(self.sync()["pending_ids_json"]), ["m-110"])
        self.assertEqual(self.pass_once()["state"], "ok")
        self.assertEqual(self.message_row("m-110")["state"], "done")
        self.assertEqual(self.stage(self.acme)[0], "applied")

    def test_the_first_look_back_reaches_past_when_the_live_cursor_was_taken(self):
        self.switch("on", since=now_utc() - timedelta(hours=1))
        before_call = int(datetime.now(timezone.utc).timestamp())
        # A pass slow to start: its own clock says ten minutes ago.
        application_inbox.run_pass(self.conn, user_id=USER, client_factory=self.factory, force=True, now=now_utc() - timedelta(minutes=10))
        until = int(self.sync()["backfill_query"].split("before:", 1)[1].split()[0])
        self.assertGreaterEqual(until, before_call, "mail from while the pass got ready is in the look back")

    def test_approve_all_leaves_offers_unverified_senders_and_guesses_one_by_one(self):
        received = now_utc() - timedelta(days=10)
        self.gmail.store("b-10", acme_confirmation(date=received), received)
        self.gmail.store("b-11", orbit_mail("Offer letter - Controls Co-op", "We are pleased to offer you the Controls Co-op role at Orbit Systems.",
                                            headers="", date=received), received + timedelta(minutes=1))
        self.gmail.backfill_found = ["b-10", "b-11"]
        self.switch("on")
        self.pass_once()
        status = application_inbox.status(self.conn, USER)
        self.assertEqual((status["backfill_found"], status["backfill_approvable"]), (2, 1))
        counts = application_inbox.approve_backfill(self.conn, USER)
        self.assertEqual(counts, {"approved": 1, "superseded": 0, "left": 1})
        self.assertEqual((self.stage(self.acme)[0], self.stage(self.orbit)[0]), ("applied", "applied"), "the unverified offer waits")
        [offer] = self.actions(status="proposed")
        self.assertEqual(offer["after"]["stage"], "offer")

    def test_a_date_right_after_its_cue_is_the_deadline_and_the_day_it_was_sent_is_not(self):
        self.started()
        sent, due = in_days(1), in_days(8)
        self.deliver("m-150", job_mail(
            sender="HackerRank <support@hackerrankforwork.com>", subject="Acme Robotics has invited you to take a test",
            body=(f"Acme Robotics has invited you to take the Mechanical Engineering Intern Coding Test. This invitation was sent by the "
                  f"Acme recruiting team on {spelled(sent)}. Please complete the test before {spelled(due)}."),
        ))
        self.pass_once()
        [deadline] = self.conn.execute("SELECT deadline_on FROM email_deadlines WHERE application_id=?", (self.acme,)).fetchall()
        self.assertEqual(deadline["deadline_on"], due.isoformat())
        [task] = self.conn.execute("SELECT due_at FROM application_tasks WHERE application_id=?", (self.acme,)).fetchall()
        self.assertTrue(task["due_at"].startswith(due.isoformat()))

    def test_a_numeric_date_that_reads_two_ways_only_proposes(self):
        self.started()
        due = next(day for day in (in_days(offset) for offset in range(20, 120)) if day.day <= 12 and day.day != day.month)
        self.deliver("m-151", job_mail(
            sender="HackerRank <support@hackerrankforwork.com>", subject="Acme Robotics has invited you to take a test",
            body=f"Acme Robotics invited you to the Mechanical Engineering Intern Coding Test. Please complete it by {due.month}/{due.day}/{due.year}.",
        ))
        self.pass_once()
        for action_type in ("application.deadline", "application.task"):
            [action] = self.actions(action_type=action_type)
            self.assertEqual(action["status"], "proposed", action_type)
            self.assertIn(application_inbox.TWO_READINGS, action["evidence"]["why_proposal"])

    def proposed_test_task(self, gmail_id, token):
        """An unverified HackerRank invitation, so its task is only proposed; returns (task action, link)."""
        link = f"https://www.hackerrank.com/test/{token}/login?token={token}-secret"
        self.deliver(gmail_id, job_mail(
            sender="HackerRank <support@hackerrankforwork.com>", subject="Acme Robotics has invited you to take a test", headers="",
            body=f"Acme Robotics invited you to the Mechanical Engineering Intern Coding Test. Please complete it by {spelled(in_days(30))}.\n\n{link}",
        ))
        self.pass_once()
        [task] = [action for action in self.actions(action_type="application.task") if action["evidence"]["gmail_id"] == gmail_id]
        self.assertEqual(task["status"], "proposed", "the sender was not verified")
        return task, link

    def assert_nowhere_but_the_task(self, token):
        """PLAN 1.6: the link is on the task itself and nowhere else, not even while its proposal waits."""
        for row in self.conn.execute("SELECT * FROM automation_actions").fetchall():
            self.assertNotIn(token, json.dumps(dict(row)))
        self.assertNotIn(token, json.dumps(export_account(self.conn, user_id=USER)["automation_actions"]))

    def test_a_proposed_task_keeps_its_link_off_the_ledger_while_it_waits_and_once_decided(self):
        self.started()
        task, link = self.proposed_test_task("m-152", "xyz789")
        self.assert_nowhere_but_the_task("xyz789")
        self.assertEqual(task["after"]["task"]["link_host"], "www.hackerrank.com", "the ledger names the host")
        automation.approve(self.conn, task["id"], USER)
        self.assertEqual(self.conn.execute("SELECT link FROM application_tasks WHERE application_id=?", (self.acme,)).fetchone()[0], link,
                         "approving puts the whole link on the task")
        [deadline] = self.actions(action_type="application.deadline")
        automation.reject(self.conn, deadline["id"], USER)
        self.assert_nowhere_but_the_task("xyz789")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM automation_held").fetchone()[0], 0, "nothing held once decided")

    def test_every_other_decision_forgets_the_held_link(self):
        self.started()
        rejected, _ = self.proposed_test_task("m-154", "rej111")
        automation.reject(self.conn, rejected["id"], USER)
        ignored, _ = self.proposed_test_task("m-155", "ign222")
        event = monitored_event(self.conn, application_inbox._event_id(self.conn, USER, "m-155"), user_id=USER)
        application_inbox.decide_event(self.conn, event, "ignore", None, user_id=USER)
        self.assertEqual(self.actions(id=ignored["id"])[0]["status"], "expired")
        waiting, _ = self.proposed_test_task("m-156", "old333")
        with self.conn:
            self.conn.execute("UPDATE automation_actions SET created_at=? WHERE id=?", ((now_utc() - timedelta(days=400)).isoformat(), waiting["id"]))
        application_inbox.purge_excerpts(self.conn)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM automation_held").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM application_tasks WHERE application_id=?", (self.acme,)).fetchone()[0], 0)

    def test_a_corrected_approval_puts_the_held_link_on_the_chosen_application(self):
        self.started()
        task, link = self.proposed_test_task("m-157", "cor444")
        approved = automation.approve(self.conn, task["id"], USER, subject_id=self.orbit)
        self.assertEqual((approved["status"], approved["subject_id"]), ("applied", self.orbit))
        self.assertEqual(self.conn.execute("SELECT link FROM application_tasks WHERE application_id=?", (self.orbit,)).fetchone()[0], link)
        self.assert_nowhere_but_the_task("cor444")

    def test_an_approved_deadline_from_an_unverified_sender_says_so_in_urgent(self):
        self.started()
        due = in_days(20)
        self.deliver("m-153", job_mail(
            sender="Acme Robotics Hiring Team <no-reply@us.greenhouse-mail.io>", subject="Next step: online assessment", headers="",
            body=f"Thanks for applying to the Mechanical Engineering Intern role at Acme Robotics. Please complete our online assessment by {spelled(due)}.",
        ))
        self.pass_once()
        [deadline] = self.actions(action_type="application.deadline")
        automation.approve(self.conn, deadline["id"], USER)
        queue = urgent_queue(self.conn, user_id=USER, days=60, now=datetime.combine(due - timedelta(days=5), datetime.min.time(), tzinfo=timezone.utc))
        [item] = [item for item in queue["items"] if item["kind"] == "email_deadline"]
        self.assertTrue(item["date_note"].startswith("From greenhouse-mail.io (sender not verified), received "), item["date_note"])

    def test_mail_from_the_domain_of_an_application_added_later_is_read(self):
        self.started()
        self.add_application("nimbus", "Nimbus Aero", "Flight Software Intern", url="https://careers.nimbus-aero.com/jobs/42")
        self.deliver("m-120", job_mail(sender="Nimbus Aero <talent@nimbus-aero.com>", subject="Interview invitation: Nimbus Aero",
                                       body="Hi Sam, we'd like to invite you to interview for the Flight Software Intern role at Nimbus Aero."))
        self.pass_once()
        self.assertEqual(self.message_row("m-120")["state"], "done")
        [stage] = self.actions(action_type="application.stage")
        self.assertEqual(stage["status"], "proposed")
        self.assertIn("mail from nimbus-aero.com is not trusted for Nimbus Aero yet", stage["evidence"]["why_proposal"])

    def test_a_newsletter_or_a_forward_never_acts_on_its_own(self):
        self.started()
        invite = "Hi Sam,\n\nWe'd like to invite you to interview for the Controls Co-op role at Orbit Systems."
        newsletter = arh("hire.lever.co") + "List-Unsubscribe: <mailto:unsubscribe@hire.lever.co>\n"
        self.deliver("m-130", orbit_mail("Interview invitation: Orbit Systems", invite, headers=newsletter), minutes_ago=9)
        self.deliver("m-131", orbit_mail("Fwd: Interview invitation: Orbit Systems", invite), minutes_ago=8)
        self.deliver("m-132", orbit_mail("Interview invitation", f"---------- Forwarded message ----------\nFrom: Orbit Systems\n\n{invite}"), minutes_ago=7)
        self.pass_once()
        stages = {action["evidence"]["gmail_id"]: action for action in self.actions(action_type="application.stage")}
        for gmail_id, reason in (("m-130", "it is a newsletter or mailing-list email"), ("m-131", "it is a forwarded email"),
                                 ("m-132", "it is a forwarded email")):
            with self.subTest(gmail_id=gmail_id):
                self.assertEqual(stages[gmail_id]["status"], "proposed")
                self.assertIn(reason, stages[gmail_id]["evidence"]["why_proposal"])
        self.assertEqual(self.stage(self.orbit)[0], "applied")

    def test_a_stage_change_made_after_the_email_was_planned_stops_the_automatic_change(self):
        self.started()
        self.deliver("m-140", orbit_mail("Update on your Orbit Systems application",
                                         "We regret to inform you that we will not be moving forward with your application for Controls Co-op."))
        real = automation.perform
        results = []

        def student_moves_first(*args, **kwargs):
            if kwargs.get("action_type") == "application.stage":
                # The student's own change lands between plan()'s read and perform()'s transaction.
                with self.conn:
                    self.conn.execute("UPDATE applications SET stage='offer' WHERE id=?", (self.orbit,))
            results.append(real(*args, **kwargs))
            return results[-1]

        with mock.patch.object(application_inbox.automation, "perform", side_effect=student_moves_first):
            self.pass_once()
        self.assertEqual(results, [None], "the guard inside the transaction said no")
        self.assertEqual(self.actions(action_type="application.stage"), [])
        self.assertEqual(self.stage(self.orbit)[0], "offer")


class JevTests(MailCase):
    """A Jev answer acts on its own only when the rules agree; otherwise it proposes, and never drops what the rules found."""

    REJECTION = ("Update on your Orbit Systems application",
                 "Hi Sam,\n\nThank you for your interest in the Controls Co-op role at Orbit Systems. "
                 "We regret to inform you that we will not be moving forward with your application.")

    def run_with(self, label, confidence, raw):
        from test_inbox_classifiers import FakeJev

        self.started()
        self.deliver("m-160", raw)
        self.pass_once(decisions=FakeJev(label, confidence))
        return self.actions(action_type="application.stage")

    def test_jev_agreeing_with_the_rules_may_act(self):
        [action] = self.run_with("rejected", 0.9, orbit_mail(*self.REJECTION))
        self.assertEqual((action["status"], action["after"]["stage"]), ("applied", "rejected"))

    def test_jev_finding_nothing_where_the_rules_find_a_rejection_proposes_it(self):
        [action] = self.run_with("recruiter_reply", 0.6, orbit_mail(*self.REJECTION))
        self.assertEqual((action["status"], action["after"]["stage"]), ("proposed", "rejected"))
        self.assertIn("Jev and the keyword rules disagree about what it means (recruiter_reply or rejected)", action["evidence"]["why_proposal"])
        self.assertEqual(self.stage(self.orbit)[0], "applied")
        event = self.conn.execute("SELECT event_type, status FROM monitored_events WHERE external_id='gmail:m-160'").fetchone()
        self.assertEqual(tuple(event), ("rejected", "pending"))

    def test_jev_disagreeing_with_an_actionable_label_proposes(self):
        invite = orbit_mail("Interview invitation: Orbit Systems", "Hi Sam,\n\nWe'd like to invite you to interview for the Controls Co-op role at Orbit Systems.")
        [action] = self.run_with("rejected", 0.9, invite)
        self.assertEqual((action["status"], action["after"]["stage"]), ("proposed", "rejected"))
        self.assertTrue(any(reason.startswith("Jev and the keyword rules disagree") for reason in action["evidence"]["why_proposal"]))
        self.assertEqual(self.stage(self.orbit)[0], "applied")


class ReviewFixCursorTests(MailCase):
    def test_a_history_cut_short_resumes_record_by_record(self):
        self.started()
        self.gmail.history_page_size = 1
        for number in range(5):
            self.deliver(f"h-{number}", job_mail(sender="Someone <someone@us.greenhouse-mail.io>", subject="Hello", body="Hi."), minutes_ago=10 - number)
        with mock.patch.object(application_inbox, "MAX_LIST_PAGES", 2):
            self.pass_once()
            self.assertEqual(self.sync()["history_id"], "102", "the last record read, not the newest")
            self.pass_once()
            self.pass_once()
        recorded = {row["gmail_id"] for row in self.conn.execute("SELECT gmail_id FROM application_mail_messages").fetchall()}
        self.assertEqual(recorded, {f"h-{number}" for number in range(5)})
        self.assertEqual(self.sync()["history_id"], str(self.gmail.history_id))

    def test_a_recovery_search_resumes_across_passes_before_live_reading_goes_on(self):
        self.started()
        with self.conn:
            self.conn.execute("UPDATE application_mail_sync SET last_ok_at=?", ((now_utc() - timedelta(hours=3)).isoformat(),))
        for number in range(3):
            self.gmail.store(f"r-{number}", job_mail(sender="Someone <someone@us.greenhouse-mail.io>", subject="Hello", body="Hi."),
                             now_utc() - timedelta(minutes=30 - number))
        self.gmail.recovery_found = [f"r-{number}" for number in range(3)]
        self.gmail.list_page_size = 1
        self.gmail.history_expired = True
        self.gmail.history_id = 555
        with mock.patch.object(application_inbox, "MAX_LIST_PAGES", 2):
            self.pass_once()
            sync = self.sync()
            self.assertEqual((sync["recovery_state"], sync["recovery_history_id"], sync["history_id"]), ("running", "555", "100"))
            self.pass_once()
        sync = self.sync()
        self.assertEqual((sync["recovery_state"], sync["history_id"]), ("", "555"), "live reading goes on from the cursor taken before the search")
        recorded = {row["gmail_id"] for row in self.conn.execute("SELECT gmail_id FROM application_mail_messages").fetchall()}
        self.assertEqual(recorded, {"r-0", "r-1", "r-2"})


class ReviewFixDecisionTests(DecisionTests):
    def test_confirming_the_card_for_another_application_moves_what_the_email_added_on_its_own(self):
        self.proposed_interview()
        event = self.conn.execute("SELECT id FROM monitored_events WHERE external_id='gmail:m-50'").fetchone()
        decide_monitored_event(self.conn, event["id"], "confirm", self.acme, user_id=USER)
        tasks = {row["application_id"]: row["title"] for row in self.conn.execute("SELECT application_id, title FROM application_tasks WHERE status='open'")}
        self.assertEqual(tasks, {self.acme: "Schedule interview"}, "the task follows the student's choice")
        self.assertEqual(automation.mode(self.conn, USER, FEATURE), "on")

    def test_a_card_whose_stage_proposal_was_overtaken_never_writes_the_stage(self):
        self.proposed_interview()
        update_application(self.conn, self.orbit, stage="rejected", user_id=USER)  # the student, since
        event = self.conn.execute("SELECT id FROM monitored_events WHERE external_id='gmail:m-50'").fetchone()
        decided = decide_monitored_event(self.conn, event["id"], "confirm", self.orbit, user_id=USER)
        self.assertEqual(self.stage(self.orbit)[0], "rejected")
        [stage_action] = self.actions(action_type="application.stage")
        self.assertEqual(stage_action["status"], "superseded")
        self.assertEqual(decided["status"], "confirmed", "its task had been added on its own")

    def test_confirming_a_card_whose_only_proposal_was_overtaken_says_so_and_changes_nothing(self):
        self.started()
        self.deliver("m-51", orbit_mail("Update from Orbit Systems", "We regret to inform you that we will not be moving forward with your application."))
        self.pass_once()
        [proposal] = self.actions(status="proposed")
        self.assertEqual(proposal["after"]["stage"], "rejected")
        update_application(self.conn, self.orbit, stage="interview", user_id=USER)
        event = self.conn.execute("SELECT id FROM monitored_events WHERE external_id='gmail:m-51'").fetchone()
        with self.assertRaises(automation.Superseded):
            decide_monitored_event(self.conn, event["id"], "confirm", self.orbit, user_id=USER)
        self.assertEqual(self.stage(self.orbit)[0], "interview")
        status = self.conn.execute("SELECT status FROM monitored_events WHERE id=?", (event["id"],)).fetchone()[0]
        self.assertEqual(status, "ignored", "settled, so a second Confirm cannot write the stage either")

    def test_confirming_a_card_for_an_application_that_cannot_take_it_decides_nothing(self):
        self.proposed_interview()
        offer_app = self.add_application("acme-offer", "Acme Robotics", "Electrical Engineering Intern", stage="offer")
        event = self.conn.execute("SELECT id FROM monitored_events WHERE external_id='gmail:m-50'").fetchone()
        with self.assertRaisesRegex(automation.CorrectionRefused, "stages only move forward"):
            decide_monitored_event(self.conn, event["id"], "confirm", offer_app, user_id=USER)
        self.assertEqual(self.stage(offer_app)[0], "offer")
        self.assertEqual(len(self.actions(status="proposed")), 1, "nothing was decided")


class ReviewFixApiTests(ApiTests):
    def test_an_approval_overtaken_by_the_student_settles_the_email_card(self):
        self.started()
        self.deliver("m-73", orbit_mail("Update from Orbit Systems", "We'd like to invite you to interview. Reply with times."))
        self.pass_once()
        [proposal] = self.actions(status="proposed")
        update_application(self.conn, self.orbit, stage="rejected", user_id=USER)
        response = self.client.post(f"/api/v1/automation/actions/{proposal['id']}/approve", headers=AUTH)
        self.assertEqual(response.status_code, 409)
        event = self.conn.execute("SELECT id, status FROM monitored_events WHERE external_id='gmail:m-73'").fetchone()
        self.assertEqual(event["status"], "confirmed", "nothing is left to approve, so the card is settled")
        again = self.client.post(f"/api/v1/monitored-events/{event['id']}/decision", headers=AUTH,
                                 json={"decision": "confirm", "application_id": self.orbit})
        self.assertEqual(again.status_code, 409)
        self.assertEqual(self.stage(self.orbit)[0], "rejected", "the card never writes a stage the ledger refused")

    def test_a_correction_is_held_to_the_same_forward_only_rules(self):
        self.switch("on")
        offer_app = self.add_application("acme-offer", "Acme Robotics", "Electrical Engineering Intern", stage="offer")
        rejection = automation.perform(
            self.conn, user_id=USER, feature=FEATURE, action_type="application.stage", subject_kind="application", subject_id=self.orbit,
            after={"stage": "rejected"}, evidence={"gmail_id": "m-74"}, summary="Orbit Systems (Controls Co-op): move to rejected, from an email by lever.co",
            basis="test", confidence=0.92, idempotency_key="gmail:m-74:app-job-b:application.stage", auto=False,
        )
        response = self.client.post(f"/api/v1/automation/actions/{rejection['id']}/approve", headers=AUTH, json={"subject_id": offer_app})
        self.assertEqual(response.status_code, 422, response.text)
        self.assertIn("never overwrites an offer", response.json()["detail"])
        self.assertEqual(self.stage(offer_app)[0], "offer")
        self.assertEqual(self.actions()[0]["status"], "proposed", "nothing was decided; another application can be chosen")
        interview_app = self.add_application("acme-int", "Acme Robotics", "Firmware Intern", stage="interview")
        confirmation = automation.perform(
            self.conn, user_id=USER, feature=FEATURE, action_type="application.stage", subject_kind="application", subject_id=self.acme,
            after={"stage": "applied", "applied_at": "2026-09-01T10:00:00+00:00"}, evidence={"gmail_id": "m-75"},
            summary="Acme Robotics (Mechanical Engineering Intern): move to applied, from an email by greenhouse-mail.io",
            basis="test", confidence=0.9, idempotency_key="gmail:m-75:acme:application.stage", auto=False,
        )
        approved = automation.approve(self.conn, confirmation["id"], USER, subject_id=interview_app)
        stage, applied_at = self.stage(interview_app)
        self.assertEqual(stage, "interview", "a confirmation never moves an application back")
        self.assertIsNotNone(applied_at)
        self.assertEqual(approved["summary"], "Acme Robotics (Firmware Intern): move to applied, from an email by greenhouse-mail.io",
                         "the record names the application it changed")
        self.assertEqual(approved["evidence"]["proposed_summary"], confirmation["summary"])
        self.assertIn("(Acme Robotics (Mechanical Engineering Intern))", approved["note"])

    def test_a_guessed_link_is_never_shown_as_confirmed(self):
        second = self.add_application("acme-2", "Acme Robotics", "Firmware Intern")
        self.started()
        self.deliver("m-76", job_mail(subject="Update from Acme Robotics", body="Hi Sam, a quick update on your application: nothing new yet."), minutes_ago=6)
        self.deliver("m-77", orbit_mail("Update from Orbit Systems", "Hi Sam, a quick update on your application: nothing new yet."), minutes_ago=5)
        self.pass_once()
        for application_id in (self.acme, second):
            detail = self.client.get(f"/api/v1/applications/{application_id}", headers=AUTH).json()
            self.assertEqual(detail["emails"], [], "two open Acme applications: the email is linked to neither until the student picks")
        [orbit_email] = self.client.get(f"/api/v1/applications/{self.orbit}", headers=AUTH).json()["emails"]
        self.assertEqual(orbit_email["matched_by"], "company_single", "listed, and labelled as matched by the company alone")



# --- With internal automation: an application the app archived after no reply ------------------


ORBIT_INVITE = ("Interview invitation: Orbit Systems",
                "Hi Sam,\n\nWe'd like to invite you to interview for the Controls Co-op role at Orbit Systems.")
ORBIT_REJECTION = ("Update on your Orbit Systems application",
                   "We regret to inform you that we will not be moving forward with your application for Controls Co-op.")
ORBIT_CONFIRMATION = ("Thank you for applying to Orbit Systems",
                      "Hi Sam,\n\nThank you for applying to the Controls Co-op role at Orbit Systems. We have received your application.")


class AutomaticArchiveTests(MailCase):
    """archive_silent_applications (internal_automation) archives; a later job email may reopen only that archive."""

    def archive_automatically(self, *, hours_ago=24):
        """Orbit silent 61 days, archived by the switch, with the archive dated ``hours_ago``."""
        applied_at = (now_utc() - timedelta(days=61)).isoformat()
        with self.conn:
            self.conn.execute("UPDATE applications SET stage='applied', applied_at=? WHERE id=?", (applied_at, self.orbit))
        automation.set_mode(self.conn, USER, "archive_silent_applications", "on")
        [done] = internal_automation.archive_silent_applications(self.conn, USER, force=True)
        self.assertEqual(done["application_id"], self.orbit)
        stamp = (now_utc() - timedelta(hours=hours_ago)).isoformat()
        with self.conn:
            self.conn.execute("UPDATE automation_actions SET created_at=?, applied_at=? WHERE id=?", (stamp, stamp, done["action_id"]))
            self.conn.execute(
                "UPDATE application_events SET created_at=? WHERE application_id=? AND event_type='stage_changed' AND detail_json LIKE ?",
                (stamp, self.orbit, f"%{done['action_id']}%"),
            )
        self.assertTrue(internal_automation.automation_archived(self.conn, self.orbit))
        return done, applied_at

    def archive_as_the_student(self, *, hours_ago=24):
        update_application(self.conn, self.orbit, stage="archived", user_id=USER)
        with self.conn:  # older than the email, so manual wins is not what keeps it
            self.conn.execute("UPDATE application_events SET created_at=? WHERE application_id=? AND event_type='stage_changed'",
                              ((now_utc() - timedelta(hours=hours_ago)).isoformat(), self.orbit))
        self.assertFalse(internal_automation.automation_archived(self.conn, self.orbit))

    def orbit_tasks(self):
        return self.conn.execute("SELECT title FROM application_tasks WHERE application_id=?", (self.orbit,)).fetchall()

    def test_an_interview_email_reopens_an_application_the_app_archived(self):
        self.started()
        self.archive_automatically()
        self.deliver("m-900", orbit_mail(*ORBIT_INVITE))
        self.pass_once()
        self.assertEqual(self.stage(self.orbit)[0], "interview")
        [move] = self.actions(feature=FEATURE, action_type="application.stage")
        self.assertEqual((move["status"], move["decided_by"], move["before"]["stage"]), ("applied", "system", "archived"))
        self.assertIn("reopen (archived automatically after no reply) and move to interview", move["summary"])
        self.assertEqual([row["title"] for row in self.orbit_tasks()], ["Schedule interview"])
        self.assertFalse(internal_automation.automation_archived(self.conn, self.orbit), "the email's change is now the latest")
        # Undoing the email's change puts it back in Archived, and the app's archive does not count again.
        automation.undo(self.conn, move["id"], USER)
        self.assertEqual(self.stage(self.orbit)[0], "archived")
        self.assertFalse(internal_automation.automation_archived(self.conn, self.orbit))

    def test_a_rejection_moves_an_automatic_archive_to_rejected(self):
        self.started()
        self.archive_automatically()
        self.deliver("m-901", orbit_mail(*ORBIT_REJECTION))
        self.pass_once()
        self.assertEqual(self.stage(self.orbit)[0], "rejected")

    def test_an_application_the_student_archived_is_never_reopened(self):
        self.started()
        self.archive_as_the_student()
        self.deliver("m-902", orbit_mail(*ORBIT_INVITE), minutes_ago=6)
        self.deliver("m-903", orbit_mail(*ORBIT_REJECTION), minutes_ago=5)
        self.deliver("m-904", orbit_mail(*ORBIT_CONFIRMATION), minutes_ago=4)
        self.pass_once()
        self.assertEqual(self.stage(self.orbit)[0], "archived")
        self.assertEqual([action for action in self.actions(feature=FEATURE) if action["subject_id"] == self.orbit], [])
        self.assertEqual(self.orbit_tasks(), [], "no task is added to an application the student closed")

    def test_the_reopen_is_checked_again_inside_its_own_transaction(self):
        self.started()
        archive, _ = self.archive_automatically()
        self.deliver("m-905", orbit_mail(*ORBIT_INVITE))
        real_perform = automation.perform
        calls = []

        def student_archives_first(*args, **kwargs):
            if not calls:
                # After plan() saw the app's archive, before the change's transaction: the student
                # takes the app's archive back and archives it themselves.
                automation.undo(self.conn, archive["action_id"], USER)
                update_application(self.conn, self.orbit, stage="archived", user_id=USER)
            calls.append(kwargs["action_type"])
            return real_perform(*args, **kwargs)

        with mock.patch.object(automation, "perform", student_archives_first):
            self.pass_once()
        self.assertEqual(calls, ["application.stage", "application.task"], "both were planned from the app's archive")
        self.assertEqual(self.stage(self.orbit)[0], "archived", "the student's archive stands")
        self.assertEqual(self.actions(feature=FEATURE), [], "the guard said no inside the transaction, so nothing was recorded")
        self.assertEqual(self.orbit_tasks(), [])

    def test_a_confirmation_reopens_only_when_it_came_after_the_archive(self):
        self.started()
        _, applied_at = self.archive_automatically(hours_ago=1)
        self.deliver("m-906", orbit_mail(*ORBIT_CONFIRMATION), minutes_ago=120)
        self.pass_once()
        self.assertEqual(self.stage(self.orbit)[0], "archived", "an older confirmation tells the archive nothing new")
        self.assertEqual(self.actions(feature=FEATURE), [])
        self.deliver("m-907", orbit_mail(*ORBIT_CONFIRMATION), minutes_ago=5)
        self.pass_once()
        stage, kept = self.stage(self.orbit)
        self.assertEqual(stage, "applied", "the company wrote after the archive, so it is open again")
        self.assertEqual(datetime.fromisoformat(kept), datetime.fromisoformat(applied_at), "the applied date stays the earlier one")

    def test_a_task_with_a_reopen_that_waits_also_waits(self):
        self.started()
        self.archive_automatically()
        # The company alone matches (no role named), which may add a task on its own but never move a stage.
        self.deliver("m-908", orbit_mail("Update from Orbit Systems", "We'd like to invite you to interview. Reply with times."))
        self.pass_once()
        statuses = {action["action_type"]: action["status"] for action in self.actions(feature=FEATURE)}
        self.assertEqual(statuses, {"application.stage": "proposed", "application.task": "proposed"})
        [task] = self.actions(feature=FEATURE, action_type="application.task")
        self.assertIn("reopening the application, which the app archived after no reply, waits for you", task["evidence"]["why_proposal"])
        self.assertEqual(self.stage(self.orbit)[0], "archived")
        self.assertEqual(self.orbit_tasks(), [], "never a task on its own on an application that is still archived")

    def test_a_correction_may_reopen_only_the_apps_own_archive(self):
        self.switch("on")
        self.archive_automatically()
        interview = automation.perform(
            self.conn, user_id=USER, feature=FEATURE, action_type="application.stage", subject_kind="application", subject_id=self.acme,
            after={"stage": "interview"}, evidence={"gmail_id": "m-909"}, summary="Acme Robotics (Mechanical Engineering Intern): move to interview",
            basis="test", confidence=0.9, idempotency_key="gmail:m-909:acme:application.stage", auto=False,
        )
        approved = automation.approve(self.conn, interview["id"], USER, subject_id=self.orbit)
        self.assertEqual((approved["status"], approved["subject_id"]), ("applied", self.orbit))
        self.assertEqual(self.stage(self.orbit)[0], "interview")
        closed = self.add_application("orbit-2", "Orbit Systems", "Avionics Intern", stage="applied")
        update_application(self.conn, closed, stage="archived", user_id=USER)
        again = automation.perform(
            self.conn, user_id=USER, feature=FEATURE, action_type="application.stage", subject_kind="application", subject_id=self.acme,
            after={"stage": "interview"}, evidence={"gmail_id": "m-910"}, summary="Acme Robotics (Mechanical Engineering Intern): move to interview",
            basis="test", confidence=0.9, idempotency_key="gmail:m-910:acme:application.stage", auto=False,
        )
        with self.assertRaises(automation.CorrectionRefused) as refused:
            automation.approve(self.conn, again["id"], USER, subject_id=closed)
        self.assertIn("is archived", str(refused.exception))
        self.assertEqual(self.stage(closed)[0], "archived")


if __name__ == "__main__":
    unittest.main()
