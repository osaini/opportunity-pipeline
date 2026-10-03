"""Apply for me's security-code reader (apply/security_code.py, spec D10 B): the parent side of Greenhouse's emailed code.

No browser and no network: Gmail is a scripted httpx.MockTransport that raises for any host but Gmail's, and the clock is
passed in. The code is a made-up string; every test that sees it found asserts it appears nowhere but the pipe reply.
"""

import base64
import json
import logging
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx
from cryptography.fernet import Fernet

from opportunity_app.apply import runs as apply_runs, security_code, watch as apply_watch
from opportunity_app.mail import gmail_connection
from opportunity_app.automation import ledger as automation
from opportunity_app.integrations.gmail_client import GmailAuthError, READ_SCOPE
from opportunity_app.student.profile import update_profile
from opportunity_app.core.timestamps import utc_now

from helpers_apply import ApplyCase, USER, setUpModule, tearDownModule  # noqa: F401 (module fixtures: unittest and pytest find them here)
from helpers_gmail import forget_gmail_backoff, rate_limited
from test_application_inbox import job_mail

try:
    import realdata_guard
except ImportError:  # imported as tests.<module>, with tests/ not on sys.path
    from tests import realdata_guard
# Guards this module's own run against opening data/*.db (AGENTS.md hard rule 1); see tests/realdata_guard.py.
realdata_guard.install()

EMAIL = "sam.rivera@example.test"
CODE = "X7KQ2M9P"
SUBJECT = "Security code for your application to Bluefin Robotics"
SENDER = "Bluefin Robotics <no-reply@us.greenhouse-mail.io>"
BODY = f"Hi Sam,\n\nCopy and paste this code into the security code field on your application:\n\n{CODE}\n\nAfter you enter the code, resubmit your application."


def iso(moment):
    return moment.astimezone(timezone.utc).isoformat(timespec="microseconds")


class CodeGmail:
    """Gmail as the reader sees it: a list of messages, each served raw. Anything but a GET to Gmail's host is a failure."""

    def __init__(self):
        self.messages = {}
        self.requests = []
        self.queries = []
        self.respond = None

    def add(self, gmail_id, raw, received, labels=("INBOX",)):
        self.messages[gmail_id] = (raw, int(received.timestamp() * 1000), list(labels))

    def handler(self, request):
        if request.url.host != "gmail.googleapis.com" or request.method != "GET":
            raise AssertionError(f"unexpected request: {request.method} {request.url}")
        self.requests.append(request)
        if self.respond is not None:
            return self.respond(request)
        path = request.url.path
        if path.endswith("/messages"):
            self.queries.append(request.url.params.get("q", ""))
            return httpx.Response(200, json={"messages": [{"id": key} for key in self.messages]})
        message_id = path.rsplit("/", 1)[1]
        if message_id not in self.messages:
            return httpx.Response(404)
        raw, received, labels = self.messages[message_id]
        return httpx.Response(200, json={"id": message_id, "threadId": f"t-{message_id}", "labelIds": labels, "internalDate": str(received),
                                         "raw": base64.urlsafe_b64encode(raw).decode()})


class CodeCase(ApplyCase):
    def setUp(self):
        super().setUp()
        self.key = Fernet.generate_key().decode()
        env = mock.patch.dict(os.environ, {"PIPELINE_CONNECTION_KEY": self.key})
        env.start()
        self.addCleanup(env.stop)
        forget_gmail_backoff(self)
        self.gmail = CodeGmail()
        self.factory = lambda: httpx.Client(transport=httpx.MockTransport(self.gmail.handler))
        self.reader = security_code.SecurityCodeReader(self.factory)
        self.connect()
        update_profile(self.conn, {"contact": {"email": EMAIL}}, ["contact"], user_id=USER)
        self.handed = self.at(minutes=-5)
        self.token = self.raw_claim(state="clicking", mode="one_click", handed_over_at=iso(self.handed))
        self.records = []
        handler = logging.Handler()
        handler.emit = self.records.append
        logging.getLogger().addHandler(handler)
        self.addCleanup(logging.getLogger().removeHandler, handler)
        self.old_level = logging.getLogger().level
        logging.getLogger().setLevel(logging.DEBUG)
        self.addCleanup(logging.getLogger().setLevel, self.old_level)

    def connect(self, *, account=EMAIL, status="connected", scopes=(READ_SCOPE,)):
        fernet = Fernet(self.key.encode())
        with self.conn:
            self.conn.execute("DELETE FROM connector_accounts WHERE user_id=?", (USER,))
            if status is not None:
                self.conn.execute(
                    "INSERT INTO connector_accounts(id, user_id, provider, scopes_json, encrypted_access_token, encrypted_refresh_token, status, "
                    "created_at, updated_at, account_email) VALUES('connector-gmail', ?, 'gmail_drafts', ?, ?, ?, ?, ?, ?, ?)",
                    (USER, json.dumps(list(scopes)), fernet.encrypt(b"valid-token").decode(), fernet.encrypt(b"refresh").decode(), status,
                     utc_now(), utc_now(), account),
                )

    def code_mail(self, gmail_id="gm-1", *, subject=SUBJECT, body=BODY, sender=SENDER, minutes=-1, headers=None, labels=("INBOX",)):
        self.gmail.add(gmail_id, job_mail(sender=sender, subject=subject, body=body, headers=headers), self.at(minutes=minutes), labels)

    def ask(self, seconds=0, token=None):
        return self.reader.answer(self.conn, user_id=USER, token=token or self.token, now=self.at(seconds=seconds))

    def security(self, token=None):
        return json.loads(self.claim_row(token or self.token)["detail_json"]).get(security_code.RECORD_KEY, {})

    def typed(self, seconds=0, token=None):
        return self.reader.confirm_typed(self.conn, user_id=USER, token=token or self.token, now=self.at(seconds=seconds))

    def everywhere(self):
        """Every piece of text the database, the notices and the log hold."""
        texts = []
        for (name,) in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
            for row in self.conn.execute(f'SELECT * FROM "{name}"').fetchall():
                texts += [str(value) for value in tuple(row)]
        texts += [record.getMessage() for record in self.records]
        return "\n".join(texts)


class FoundTests(CodeCase):
    def test_one_verified_greenhouse_email_after_the_hand_over_gives_its_code(self):
        self.code_mail()
        answer = self.ask()
        self.assertEqual((answer.status, answer.code, answer.reason), ("found", CODE, ""))
        self.assertEqual(answer.message(), {"op": "security_code", "status": "found", "reason": "", "code": CODE})
        self.assertNotIn(CODE, repr(answer))
        self.assertNotIn(CODE, str(answer))
        record = self.security()
        self.assertEqual(record["reader"], "handed", "handing the code out is not typing it")
        self.assertEqual(set(record), {"prompted_at", "reader", "handed_at"})
        self.assertEqual(json.loads(self.claim_row(self.token)["detail_json"])["waiting"], "security_code")
        self.assertEqual(self.notices(), [], "nothing is announced until the child says it typed")
        self.assertEqual(apply_watch.ats_statistics(self.conn, USER)["security_code_typed"], 0)
        self.assertTrue(self.typed(seconds=3))
        record = self.security()
        self.assertEqual((record["reader"], set(record)), ("typed", {"prompted_at", "reader", "handed_at", "typed_at"}))
        self.assertEqual(self.notices(), ["Apply for me entered the security code Greenhouse emailed you for Bluefin Robotics"])
        self.assertEqual(apply_watch.ats_statistics(self.conn, USER)["security_code_typed"], 1)
        self.assertNotIn(CODE, self.everywhere(), "the code is in the pipe reply and nowhere else")
        [query] = self.gmail.queries
        self.assertIn('from:(greenhouse.io OR greenhouse-mail.io) subject:"security code" after:', query)
        self.assertTrue(all(request.method == "GET" for request in self.gmail.requests))

    def test_a_second_request_after_a_typed_code_falls_back_without_a_notice(self):
        self.code_mail()
        self.assertEqual(self.ask().status, "found")
        self.assertTrue(self.typed())
        again = self.ask(seconds=20)
        self.assertEqual((again.status, again.reason, again.code), ("fallback", "already_used", ""))
        restarted = security_code.SecurityCodeReader(self.factory).answer(self.conn, user_id=USER, token=self.token, now=self.at(seconds=40))
        self.assertEqual((restarted.status, restarted.reason), ("fallback", "already_used"), "the claim remembers it, not only the process")
        self.assertEqual(len(self.notices()), 1, "only the typed notice")
        self.assertFalse(self.typed(seconds=50), "typed once")

    def test_a_code_handed_out_and_never_acknowledged_tells_the_student_to_type_it(self):
        """The child's wait ran out (or it crashed, or the window closed): the app cannot know the code was typed."""
        self.code_mail()
        self.assertEqual(self.ask().status, "found")
        again = self.ask(seconds=50)
        self.assertEqual((again.status, again.reason, again.code), ("fallback", "not_confirmed", ""))
        [notice] = automation.list_notices(self.conn, USER)
        self.assertEqual(notice["title"], "Bluefin Robotics: Greenhouse emailed you a security code. Type it into the Chromium window.")
        self.assertEqual(notice["body"], "The app couldn't read it from your email: the app found the code but could not tell that it was entered.")
        self.assertNotIn("typed", notice["title"] + notice["body"])
        restarted = security_code.SecurityCodeReader(self.factory).answer(self.conn, user_id=USER, token=self.token, now=self.at(seconds=70))
        self.assertEqual((restarted.status, restarted.reason), ("fallback", "not_confirmed"), "the claim remembers it, with no second notice")
        self.assertEqual(len(self.notices()), 1)
        self.assertFalse(self.typed(seconds=80), "a late acknowledgement after the student was asked to type is not counted")
        stats = apply_watch.ats_statistics(self.conn, USER)
        self.assertEqual((stats["security_code_prompts"], stats["security_code_typed"]), (1, 0))
        self.assertNotIn(CODE, self.everywhere())

    def test_the_acknowledgement_counts_only_for_a_code_that_was_handed_out(self):
        self.assertFalse(self.typed(), "nothing was handed out")
        self.assertFalse(self.typed(token="no-such-token"))
        self.code_mail()
        self.ask()
        self.assertTrue(self.typed())
        with self.conn:
            self.conn.execute("UPDATE application_submit_claims SET state='submitted', verification='not_watched' WHERE token=?", (self.token,))
        self.assertFalse(self.typed(), "the claim is no longer ours to answer")

    def test_the_pipe_message_carries_the_code_only_when_found(self):
        self.assertEqual(security_code.CodeAnswer("waiting").message(), {"op": "security_code", "status": "waiting", "reason": ""})
        self.assertEqual(security_code.CodeAnswer("fallback", reason="two_candidates").message(),
                         {"op": "security_code", "status": "fallback", "reason": "two_candidates"})
        self.assertNotIn("code", security_code.CodeAnswer("fallback", code=CODE, reason="x").message())
        self.assertEqual(security_code.CodeAnswer("found", code=CODE).message()["code"], CODE)


class FallbackTests(CodeCase):
    def test_two_candidates_fall_back(self):
        self.code_mail("gm-1")
        self.code_mail("gm-2", body=BODY.replace(CODE, "AB12CD34"))
        answer = self.ask()
        self.assertEqual((answer.status, answer.reason, answer.code), ("fallback", "two_candidates", ""))
        self.assertEqual(self.security()["reader"], "fallback")
        [title] = self.notices()
        self.assertEqual(title, "Bluefin Robotics: Greenhouse emailed you a security code. Type it into the Chromium window.")
        [notice] = automation.list_notices(self.conn, USER)
        self.assertEqual(notice["body"], "The app couldn't read it from your email: more than one security code email arrived.")
        self.assertEqual(self.ask(seconds=20).reason, "two_candidates", "the same answer again, with no second notice")
        self.assertEqual(len(self.notices()), 1)
        self.assertNotIn(CODE, self.everywhere())

    def test_a_message_with_two_codes_is_unclear(self):
        self.code_mail(body=f"Your code is {CODE}.\n\nAnother reference: Q9W8E7R6.")
        answer = self.ask()
        self.assertEqual((answer.status, answer.reason), ("fallback", "unclear_code"))
        self.code_mail("gm-1", body="Hi Sam,\n\nA security code was requested, but it is not in this email.")
        self.assertEqual(self.ask(seconds=20).reason, "unclear_code", "recorded: the fallback stands")

    def test_a_mailbox_that_could_not_be_read_for_the_window_is_not_reported_as_no_email(self):
        self.gmail.respond = lambda request: httpx.Response(500)
        self.assertEqual(self.ask(0).status, "waiting")
        self.assertEqual(self.security().get("last_look_ok", False), False)
        out = self.ask(600)
        self.assertEqual((out.status, out.reason), ("fallback", "gmail_unreachable"))
        [notice] = automation.list_notices(self.conn, USER)
        self.assertEqual(notice["body"], "The app couldn't read it from your email: the app couldn't reach Gmail in time, so it doesn't know whether the email arrived.")
        self.assertNotIn("no security code email arrived", notice["body"])

    def test_a_look_that_worked_and_then_failed_does_not_claim_no_email_either(self):
        self.assertEqual(self.ask(0).status, "waiting")
        self.assertTrue(self.security()["last_look_ok"], "an empty mailbox, read to its end")
        self.gmail.respond = lambda request: httpx.Response(500)
        self.assertEqual(self.ask(20).status, "waiting")
        self.assertFalse(self.security()["last_look_ok"], "the latest look could not read it")
        self.assertEqual(self.ask(600).reason, "gmail_unreachable")

    def test_no_message_waits_then_times_out_at_10_minutes(self):
        first = self.ask()
        self.assertEqual((first.status, first.reason, first.code), ("waiting", "", ""))
        self.assertEqual(self.security()["reader"], "waiting")
        self.assertEqual(self.notices(), [])
        late = self.ask(seconds=599)
        self.assertEqual(late.status, "waiting")
        out = self.ask(seconds=600)
        self.assertEqual((out.status, out.reason), ("fallback", "timed_out"))
        [notice] = automation.list_notices(self.conn, USER)
        self.assertIn("no security code email arrived within 10 minutes", notice["body"])
        self.assertEqual(self.security()["reader"], "fallback")

    def test_a_different_application_address_falls_back_without_reading_gmail(self):
        cases = {
            "other address": (dict(account="someone.else@example.test"), "other_address"),
            "address not known yet": (dict(account=""), "unknown_address"),
            "not connected": (dict(status=None), "no_gmail"),
            "needs reconnect": (dict(status="error"), "needs_reconnect"),
            "no read permission": (dict(scopes=("https://www.googleapis.com/auth/gmail.compose",)), "no_read_permission"),
        }
        for name, (kwargs, reason) in cases.items():
            with self.subTest(case=name):
                with self.conn:
                    self.conn.execute("DELETE FROM automation_notices")
                    self.conn.execute("UPDATE application_submit_claims SET detail_json='{}' WHERE token=?", (self.token,))
                self.connect(**kwargs)
                self.code_mail()
                answer = self.ask()
                self.assertEqual((answer.status, answer.reason), ("fallback", reason))
                self.assertEqual(self.gmail.requests, [], "no request was made")
                self.assertEqual(len(self.notices()), 1)
                self.reader = security_code.SecurityCodeReader(self.factory)
        self.connect()
        with self.conn:
            self.conn.execute("UPDATE application_submit_claims SET detail_json='{}' WHERE token=?", (self.token,))
        self.assertEqual(self.ask(seconds=1).status, "found", "the right account reads")

    def test_a_gmail_auth_error_falls_back_to_reconnect(self):
        with mock.patch.object(security_code.GmailClient, "request", side_effect=GmailAuthError("Gmail rejected the connection")):
            answer = self.ask()
        self.assertEqual((answer.status, answer.reason), ("fallback", "needs_reconnect"))
        self.assertIn("Gmail needs reconnecting", automation.list_notices(self.conn, USER)[0]["body"])

    def test_each_fallback_leaves_one_notice_without_values(self):
        self.code_mail("gm-1")
        self.code_mail("gm-2", body=BODY.replace(CODE, "AB12CD34"))
        self.ask()
        self.ask(seconds=30)
        [notice] = automation.list_notices(self.conn, USER)
        for text in (notice["title"], notice["body"]):
            for forbidden in (CODE, "AB12CD34", "@", "http", "gm-1"):
                self.assertNotIn(forbidden, text)


class IgnoredTests(CodeCase):
    def test_unverified_wrong_domain_before_hand_over_or_other_company_messages_are_ignored(self):
        cases = {
            "no sender check": dict(headers=""),
            "not greenhouse": dict(sender="Bluefin Robotics <no-reply@hire.lever.co>"),
            "before the hand-over": dict(minutes=-6),
            "another company": dict(subject="Security code for your application to Orbit Systems"),
            "only part of the name": dict(subject="Security code for your application to Bluefin"),
            "a sent copy": dict(labels=("SENT",)),
            "not a security code email": dict(subject="Thank you for applying to Bluefin Robotics"),
        }
        for name, kwargs in cases.items():
            with self.subTest(case=name):
                self.gmail.messages.clear()
                self.reader = security_code.SecurityCodeReader(self.factory)
                self.code_mail(**kwargs)
                with self.conn:
                    self.conn.execute("UPDATE application_submit_claims SET detail_json='{}' WHERE token=?", (self.token,))
                answer = self.ask()
                self.assertEqual((answer.status, answer.code), ("waiting", ""), "never a code from a message that does not qualify")
        self.gmail.messages.clear()
        self.code_mail()
        self.reader = security_code.SecurityCodeReader(self.factory)
        self.assertEqual(self.ask().status, "found")

    def test_a_qualifying_message_among_ignored_ones_is_the_only_candidate(self):
        self.code_mail("noise-1", headers="")
        self.code_mail("noise-2", subject="Security code for your application to Orbit Systems")
        self.code_mail("the-one")
        self.assertEqual(self.ask().code, CODE)


class PacingTests(CodeCase):
    def test_looks_are_spaced_15_seconds_apart(self):
        self.assertEqual(self.ask(0).status, "waiting")
        first = len(self.gmail.requests)
        self.assertGreater(first, 0)
        self.assertEqual(self.ask(5).status, "waiting")
        self.assertEqual(len(self.gmail.requests), first, "no new look inside 15 seconds")
        self.code_mail()
        self.assertEqual(self.ask(14).status, "waiting")
        self.assertEqual(self.ask(15).status, "found", "the next look finds it")

    def test_gmail_throttled_or_unreachable_keeps_waiting(self):
        self.gmail.respond = lambda request: rate_limited()
        self.assertEqual(self.ask(0).status, "waiting")
        self.assertEqual(self.security()["reader"], "waiting", "nothing is given up over a slow Gmail")
        self.assertEqual(self.notices(), [])

        def unreachable(request):
            raise httpx.ConnectError("no route", request=request)

        self.reader = security_code.SecurityCodeReader(self.factory)
        gmail_connection._BACKOFF.clear()
        self.gmail.respond = unreachable
        self.assertEqual(self.ask(1).status, "waiting")
        self.gmail.respond = lambda request: httpx.Response(500)
        gmail_connection._BACKOFF.clear()
        self.reader = security_code.SecurityCodeReader(self.factory)
        self.assertEqual(self.ask(2).status, "waiting")
        self.assertEqual(self.ask(601).reason, "gmail_unreachable", "the window still ends on time, and says the app never read the mailbox")


class CurrentClaimTests(CodeCase):
    def test_a_claim_not_clicking_is_not_current(self):
        self.code_mail()
        with self.conn:
            self.conn.execute("UPDATE application_submit_claims SET state='submitted', verification='not_watched' WHERE token=?", (self.token,))
        for token in (self.token, "no-such-token"):
            answer = self.ask(token=token)
            self.assertEqual((answer.status, answer.reason, answer.code), ("fallback", "not_current", ""))
        self.assertEqual(self.gmail.requests, [])
        self.assertEqual((self.security(), self.notices()), ({}, []), "nothing recorded for a claim that is not ours to answer")
        with self.conn:
            self.conn.execute("UPDATE application_submit_claims SET state='clicking', instance='another-process' WHERE token=?", (self.token,))
        self.assertEqual(self.ask().reason, "not_current")

    def test_the_prompt_counts_in_the_statistics(self):
        self.ask()
        stats = apply_watch.ats_statistics(self.conn, USER)
        self.assertEqual((stats["security_code_prompts"], stats["security_code_typed"]), (1, 0), "a prompt counts whatever becomes of it (R1)")
        self.code_mail()
        self.ask(seconds=20)
        stats = apply_watch.ats_statistics(self.conn, USER)
        self.assertEqual((stats["security_code_prompts"], stats["security_code_typed"]), (1, 0), "handed out is not typed")
        self.typed(seconds=25)
        stats = apply_watch.ats_statistics(self.conn, USER)
        self.assertEqual((stats["security_code_prompts"], stats["security_code_typed"]), (1, 1), "one prompt, typed once")
        self.assertIn("Greenhouse asked for a code 1 time; the app typed it 1 time.", stats["lines"][2])

    def test_a_prompt_survives_the_settle_that_records_security_code_true(self):
        """6.14 settles a prompted claim with detail.security_code=true; a shallow merge must not erase the count."""
        self.code_mail()
        self.ask()
        self.typed(seconds=5)
        apply_runs.settle(self.conn, self.token, user_id=USER, state="submitted", confirmation_seen=True, watch=True,
                          detail={"security_code": True}, now=self.at(seconds=10))
        detail = json.loads(self.claim_row(self.token)["detail_json"])
        self.assertIs(detail["security_code"], True)
        stats = apply_watch.ats_statistics(self.conn, USER)
        self.assertEqual((stats["security_code_prompts"], stats["security_code_typed"]), (1, 1))
        self.assertEqual(stats["recent"]["security_code_prompts"], 0, "the watch is still running: not in 8.8's finished window yet")

    def test_a_prompt_the_student_answered_alone_counts_from_the_boolean(self):
        """No reader record at all (D10 A from the start): only detail.security_code=true says Greenhouse asked."""
        token = self.raw_claim(state="submitted", mode="one_click", handed_over_at=iso(self.at(minutes=-30)), verification="email_confirmed",
                               detail={"security_code": True})
        self.assertEqual(apply_watch.ats_statistics(self.conn, USER)["security_code_prompts"], 1)
        self.assertEqual(apply_watch.ats_statistics(self.conn, USER)["recent"]["security_code_prompts"], 1)
        self.assertEqual(self.claim_row(token)["state"], "submitted")


class ExtractCodeTests(unittest.TestCase):
    def test_extract_code(self):
        cases = [
            ("Copy and paste this code into the field:\n\nX7KQ2M9P\n\nThanks", "X7KQ2M9P"),
            ("Your security code is X7KQ2M9P. Enter it to continue.", "X7KQ2M9P"),
            ("Your code:\nXQKMZPTA\nThanks", "XQKMZPTA"),
            ("Your code is X7KQ2M9P or X7KQ2M9P", "X7KQ2M9P"),
            ("Your code is X7KQ2M9P and Q9W8E7R6", None),
            ("Your code is on its way, resubmit your application with it", None),
            ("Thank you for your interest, enter the code resubmit now", None),
            ("No such word here: X7KQ2M9P", None),
            ("", None),
            ("Your code is X7KQ2M9PZ", None),
            ("Your code is AAAA-BBBB", None),
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(security_code.extract_code(text), expected)


if __name__ == "__main__":
    unittest.main()
