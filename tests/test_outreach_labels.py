"""The Gmail label on every outreach thread (sent emails and replies), against a mocked Gmail."""

import json
import os
import re
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from opportunity_app import STATIC_DIR, automation, inbox_watcher, outreach, outreach_gmail, outreach_inbox, outreach_labels, schema
from opportunity_app.api import create_app
from opportunity_app.inbox_watcher import InboxWatcher
from opportunity_app.outreach_inbox import decide_possible_reply
from opportunity_app.database import connect_product, has_column
from opportunity_app.settings_store import get_setting, put_setting
from opportunity_app.timestamps import utc_now

from helpers_platform import build_and_migrate
from helpers_gmail import (
    ACCOUNT, LABEL_SCOPES, MODIFY, SCOPES, FakeGmail, failure_notice, forget_gmail_backoff, rate_limited,
)

AUTH = {"Authorization": "Bearer label-owner"}
USER = "local-user"
LABEL = "opportunities"
START = datetime(2026, 9, 29, 15, 0, tzinfo=timezone.utc)
# What a thread read asks Gmail for: the metadata format, and the headers that tell a delivery failure notice apart.
THREAD_READ = ("metadata", ["from", "subject", "content-type", "x-failed-recipients"])
# Every local part is_delivery_notice skips by sender, as the sweep's -from:(...) lists them.
DAEMONS = "mail-daemon OR mailer-daemon OR mailerdaemon OR postmaster"
INSUFFICIENT = {"error": {"code": 403, "message": "Request had insufficient authentication scopes.",
                          "errors": [{"reason": "insufficientPermissions", "domain": "global"}]}}


def insufficient():
    return httpx.Response(403, json=INSUFFICIENT)


def invalid_label():
    return httpx.Response(400, json={"error": {"code": 400, "message": "Invalid label: Label_gone"}})


class OpensOnAnyStatement:
    """A connection that, like PostgreSQL's, has a transaction open after any statement, a SELECT included."""

    def __init__(self, conn):
        self.conn, self.open = conn, False

    @property
    def in_transaction(self):
        return self.open or self.conn.in_transaction

    def execute(self, *args):
        self.open = True
        return self.conn.execute(*args)

    def commit(self):
        self.open = False
        self.conn.commit()

    def rollback(self):
        self.open = False
        self.conn.rollback()

    def __enter__(self):
        return self

    def __exit__(self, kind, *_rest):
        self.open = False
        self.conn.commit() if kind is None else self.conn.rollback()


class LabelCase(unittest.TestCase):
    """A connected student with LABEL_SCOPES, a mocked Gmail, and helpers to seed captured replies."""

    scopes = LABEL_SCOPES
    account_email = ACCOUNT

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        _, self.platform_path = build_and_migrate(Path(self.tempdir.name))
        self.key = Fernet.generate_key().decode()
        env = mock.patch.dict("os.environ", {
            "GOOGLE_OAUTH_CLIENT_ID": "client-id", "GOOGLE_OAUTH_CLIENT_SECRET": "client-secret",
            "PIPELINE_CONNECTION_KEY": self.key, "PIPELINE_OUTREACH_ACCOUNT": ACCOUNT,
        })
        env.start()
        self.addCleanup(env.stop)
        self.gmail = FakeGmail()
        forget_gmail_backoff(self)
        outreach_labels._IDS.clear()
        self.addCleanup(outreach_labels._IDS.clear)
        self.conn = connect_product(self.platform_path)
        self.addCleanup(self.conn.close)
        self.connect(self.scopes, self.account_email)

    # --- Seeding ----------------------------------------------------------------------

    def factory(self):
        return httpx.Client(transport=httpx.MockTransport(self.gmail.handler))

    def connect(self, scopes, account_email="", status="connected"):
        fernet = Fernet(self.key.encode())
        with self.conn:
            self.conn.execute(
                "INSERT INTO connector_accounts(id, user_id, provider, scopes_json, encrypted_access_token, encrypted_refresh_token, "
                "status, created_at, updated_at, account_email) VALUES(?, ?, 'gmail_drafts', ?, ?, ?, ?, ?, ?, ?)",
                (f"connector-gmail_drafts-{USER}", USER, json.dumps(list(scopes)), fernet.encrypt(b"valid-token").decode(),
                 fernet.encrypt(b"refresh-token").decode(), status, utc_now(), utc_now(), account_email),
            )

    def reply(self, gmail_id, thread_id="", *, kind="reply", received="2026-09-20T10:00:00+00:00", **columns):
        values = {"user_id": USER, "gmail_id": gmail_id, "target_id": "t-1", "kind": kind, "sender": "greg@bovi.example",
                  "received_at": received, "recorded_at": utc_now(), "thread_id": thread_id, **columns}
        with self.conn:
            self.conn.execute(
                f"INSERT INTO outreach_inbox_messages({', '.join(values)}) VALUES({', '.join('?' * len(values))})", tuple(values.values()),
            )

    def thread(self, thread_id, *messages):
        """Gmail's thread: each message is an id, or (id, labelIds)."""
        self.gmail.label_threads[thread_id] = [
            {"id": item, "labelIds": ["INBOX"]} if isinstance(item, str) else {"id": item[0], "labelIds": list(item[1])}
            for item in messages
        ]

    def seeded(self, count, *, prefix="m"):
        """count captured replies, each alone in its own thread, oldest first."""
        for number in range(count):
            self.thread(f"{prefix}-thread-{number:02d}", f"{prefix}-{number:02d}")
            self.reply(f"{prefix}-{number:02d}", f"{prefix}-thread-{number:02d}", received=f"2026-09-20T10:{number:02d}:00+00:00")

    def run_pass(self, now=START):
        return outreach_labels.label_replies(self.conn, user_id=USER, client_factory=self.factory, now=now)

    def run_pass_in_postgres_style(self, now=START):
        """A pass on a connection that opens a transaction on any statement: (its result, [(what Gmail was asked, whether one was open)])."""
        wrapped, asked = OpensOnAnyStatement(self.conn), []

        def handler(request):
            asked.append((f"{request.method} {request.url.path.rsplit('/', 1)[-1]} {request.url.params.get('q', '')}".strip(), wrapped.in_transaction))
            return self.gmail.handler(request)

        result = outreach_labels.label_replies(
            wrapped, user_id=USER, now=now, client_factory=lambda: httpx.Client(transport=httpx.MockTransport(handler)),
        )
        return result, asked

    def row(self, gmail_id):
        row = self.conn.execute("SELECT * FROM outreach_inbox_messages WHERE user_id=? AND gmail_id=?", (USER, gmail_id)).fetchone()
        self.conn.rollback()
        return dict(row)

    def paths(self):
        return [f"{request.method} {request.url.path.split('/users/me')[-1]}" for request in self.gmail.requests]

    def modifies(self):
        return [call for call in self.gmail.batch_modifies]

    def setting(self, key):
        value = get_setting(self.conn, USER, key)
        self.conn.rollback()
        return value

    def target(self, target_id, **columns):
        """A company the student wrote to (status sent unless said otherwise); the columns are outreach_targets'."""
        values = {"id": target_id, "user_id": USER, "company": f"Bovi {target_id}", "status": "sent",
                  "created_at": "2026-09-01T12:00:00+00:00", "updated_at": "2026-09-01T12:00:00+00:00", **columns}
        with self.conn:
            self.conn.execute(
                f"INSERT INTO outreach_targets({', '.join(values)}) VALUES({', '.join('?' * len(values))})", tuple(values.values()),
            )

    def event(self, target_id, event_type, detail, created=None):
        """An outreach event; detail is a dict (written as JSON) or the raw text."""
        self.events = getattr(self, "events", 0) + 1
        with self.conn:
            self.conn.execute(
                "INSERT INTO outreach_events(id, target_id, user_id, event_type, detail, created_at) VALUES(?, ?, ?, ?, ?, ?)",
                (f"e-{self.events:04d}", target_id, USER, event_type, detail if isinstance(detail, str) else json.dumps(detail),
                 created or f"2026-09-02T09:{self.events % 60:02d}:00+00:00"),
            )

    def sent_rows(self):
        """outreach_label_threads by thread id."""
        rows = self.conn.execute("SELECT * FROM outreach_label_threads WHERE user_id=?", (USER,)).fetchall()
        self.conn.rollback()
        return {row["thread_id"]: dict(row) for row in rows}

    def searched(self):
        """The companies whose Sent mail was searched, with how many threads it found."""
        rows = self.conn.execute("SELECT target_id, found FROM outreach_label_searches WHERE user_id=?", (USER,)).fetchall()
        self.conn.rollback()
        return {row["target_id"]: row["found"] for row in rows}

    def history_searches(self):
        """The Sent-history searches Gmail was asked (they hold a { }; the sweep's listing never does)."""
        return [query for query in self.gmail.searches if query.startswith("in:sent") and "{" in query]

    def pause(self):
        with self.conn:
            put_setting(self.conn, USER, automation.PAUSED_KEY, "on", utc_now())


class LabelNameTests(LabelCase):
    def test_the_default_is_opportunities_and_search_form_is_what_gmail_searches(self):
        self.assertEqual((outreach_labels.DEFAULT_LABEL, outreach_labels.label_name(self.conn, USER)), (LABEL, LABEL))
        self.assertEqual(outreach_labels.search_form("Job Search/2026  Fall"), "job-search-2026-fall")
        self.assertEqual(outreach_labels.search_form(""), "")

    def test_a_name_is_saved_cleaned_up_and_empty_means_off(self):
        self.assertEqual(outreach_labels.set_label_name(self.conn, USER, "  Job   Search / 2026 "), "Job Search / 2026")
        self.assertEqual(outreach_labels.label_name(self.conn, USER), "Job Search / 2026")
        self.assertEqual(outreach_labels.set_label_name(self.conn, USER, ""), "")
        self.assertEqual(outreach_labels.label_name(self.conn, USER), "", "an empty name is kept: labelling is off")
        self.assertEqual(outreach_labels.set_label_name(self.conn, USER, None), LABEL)
        self.assertEqual(outreach_labels.label_name(self.conn, USER), LABEL)

    def test_a_name_gmail_would_refuse_is_refused_in_a_sentence(self):
        for bad in ("x" * 101, "bad\x00name", "/leading", "trailing/", "a//b", "inbox", "Spam", "ALL MAIL", "drafts",
                    "CATEGORY_PERSONAL", "category_updates"):
            with self.subTest(bad):
                with self.assertRaises(ValueError) as caught:
                    outreach_labels.set_label_name(self.conn, USER, bad)
                self.assertTrue(str(caught.exception).startswith("A Gmail label name") or "Gmail keeps the name" in str(caught.exception))
        self.assertEqual(outreach_labels.label_name(self.conn, USER), LABEL, "nothing was saved")
        self.assertEqual(outreach_labels.set_label_name(self.conn, USER, "x" * 100), "x" * 100)

    def test_a_name_with_search_syntax_is_refused_naming_the_allowed_characters(self):
        # The sweep writes the name into a Gmail search, so a quote or a colon could change what the search means.
        for bad in ("a(b", "x\"y", "a:b", "tag.name", "a{b}", "a[b]", "a'b", "a+b", "a&b", "a\\b", "a*b", "a@b"):
            with self.subTest(bad):
                with self.assertRaises(ValueError) as caught:
                    outreach_labels.set_label_name(self.conn, USER, bad)
                self.assertEqual(str(caught.exception),
                                 "A Gmail label name can use only letters, digits, spaces, hyphens, underscores and slashes")
        self.assertEqual(outreach_labels.label_name(self.conn, USER), LABEL, "nothing was saved")
        for good in ("Job Search/Replies", "outreach_replies", "Отклики", "2026-fall", "求人/返信"):
            with self.subTest(good):
                self.assertEqual(outreach_labels.set_label_name(self.conn, USER, good), good)
                self.assertEqual(outreach_labels.label_name(self.conn, USER), good)


class LabelPassTests(LabelCase):
    # --- The label and the thread -----------------------------------------------------

    def test_a_confirmed_replys_whole_thread_is_labelled_except_its_drafts(self):
        self.thread("t-1", "m-1", ("m-2", ["SENT"]), ("d-1", ["DRAFT"]))
        self.reply("m-1", "t-1")
        result = self.run_pass()
        self.assertEqual(result["state"], "ok")
        self.assertEqual(self.gmail.label_creates, [
            {"name": LABEL, "labelListVisibility": "labelShow", "messageListVisibility": "show"}])
        label_id = self.gmail.label_named(LABEL)["id"]
        self.assertEqual(self.modifies(), [{"ids": ["m-1", "m-2"], "addLabelIds": [label_id]}],
                         "the student's own email in the thread too, a draft never")
        row = self.row("m-1")
        self.assertEqual(row["label_name"], LABEL)
        self.assertEqual(row["labeled_at"], START.isoformat(timespec="seconds"))
        self.assertEqual(row["label_note"], "", "labelled: no note")
        self.assertEqual((result["detail"]["labelled"], result["detail"]["more"], result["detail"]["label"]), (1, False, LABEL))
        self.assertEqual(self.paths()[:2], ["GET /labels", "POST /labels"])
        self.assertFalse([path for path in self.paths() if path.startswith("DELETE")])

    def test_a_message_that_already_has_the_label_is_not_sent_again(self):
        label = self.gmail.add_label(LABEL)
        self.thread("t-1", ("m-1", ["INBOX", label["id"]]), "m-2")
        self.reply("m-1", "t-1")
        self.run_pass()
        self.assertEqual(self.modifies(), [{"ids": ["m-2"], "addLabelIds": [label["id"]]}])
        self.thread("t-2", ("m-3", ["INBOX", label["id"]]))
        self.reply("m-3", "t-2")
        self.run_pass()
        self.assertEqual(len(self.modifies()), 1, "a thread already carrying it needs no call")
        self.assertIsNotNone(self.row("m-3")["labeled_at"])

    def test_a_label_of_that_name_is_reused_and_not_created_again(self):
        label = self.gmail.add_label(LABEL)
        self.thread("t-1", "m-1")
        self.reply("m-1", "t-1")
        self.run_pass()
        self.assertEqual(self.gmail.label_creates, [])
        self.assertEqual(self.modifies()[0]["addLabelIds"], [label["id"]])

    def test_an_exact_name_wins_over_one_that_differs_in_case_or_spacing(self):
        self.gmail.add_label("Opportunities")
        exact = self.gmail.add_label(LABEL)
        self.gmail.add_label(" opportunities ")
        self.thread("t-1", "m-1")
        self.reply("m-1", "t-1")
        self.run_pass()
        self.assertEqual(self.modifies()[0]["addLabelIds"], [exact["id"]])

    def test_a_name_that_differs_only_in_case_or_spacing_is_the_same_label(self):
        near = self.gmail.add_label("  Opportunities ")
        self.thread("t-1", "m-1")
        self.reply("m-1", "t-1")
        self.run_pass()
        self.assertEqual((self.gmail.label_creates, self.modifies()[0]["addLabelIds"]), ([], [near["id"]]))

    def test_only_the_students_own_labels_count(self):
        self.gmail.add_label(LABEL, kind="system")
        self.thread("t-1", "m-1")
        self.reply("m-1", "t-1")
        self.run_pass()
        self.assertEqual([body["name"] for body in self.gmail.label_creates], [LABEL])

    def test_a_reconnect_to_another_account_never_reuses_the_label_id_of_the_first(self):
        with mock.patch.dict("os.environ", {"PIPELINE_OUTREACH_ACCOUNT": ""}):
            self.gmail.add_label("Work")
            mine = self.gmail.add_label(LABEL)
            self.seeded(1)
            self.run_pass()
            self.assertEqual(self.modifies()[-1]["addLabelIds"], [mine["id"]])
            # The student connects another Google account, where the same id is a different label.
            with self.conn:
                self.conn.execute("UPDATE connector_accounts SET account_email=?", ("someone.else@elsewhere.example",))
            self.gmail.gmail_labels = [
                {"id": "Label_1", "name": "Work", "type": "user"},
                {"id": mine["id"], "name": "Receipts", "type": "user"},
                {"id": "Label_9", "name": LABEL, "type": "user"},
            ]
            self.seeded(1, prefix="b")
            gets = self.gmail.label_gets
            self.assertEqual(self.run_pass(START + timedelta(minutes=3))["detail"]["labelled"], 1)
            self.assertEqual(self.gmail.label_gets, gets + 1, "the labels were listed again for the new mailbox")
            self.assertEqual(self.modifies()[-1], {"ids": ["b-00"], "addLabelIds": ["Label_9"]}, "its own label, never Receipts")

    def test_the_labels_id_is_kept_for_the_next_pass(self):
        self.seeded(1)
        self.run_pass()
        self.thread("t-9", "m-9")
        self.reply("m-9", "t-9")
        self.run_pass()
        self.assertEqual(self.gmail.label_gets, 1)
        self.assertEqual(len(self.modifies()), 2)

    # --- When nothing happens ---------------------------------------------------------

    def test_labelling_off_asks_gmail_for_nothing_and_leaves_replies_unmarked(self):
        self.seeded(2)
        outreach_labels.set_label_name(self.conn, USER, "")
        result = self.run_pass()
        self.assertEqual((result["state"], result["detail"]), ("ok", {"labels": "off"}))
        self.assertEqual(self.gmail.requests, [])
        self.assertEqual(self.row("m-00")["label_name"], "")

    def test_paused_asks_gmail_for_nothing(self):
        self.seeded(2)
        self.pause()
        result = self.run_pass()
        self.assertEqual((result["state"], result["detail"]), ("ok", {"labels": "paused"}))
        self.assertEqual(self.gmail.requests, [])

    def test_nothing_pending_and_nothing_labelled_asks_gmail_for_nothing(self):
        self.assertEqual(self.run_pass(), {"state": "ok", "detail": {"labelled": 0}})
        self.assertEqual(self.gmail.requests, [])

    def test_without_the_modify_scope_it_asks_for_a_reconnect_only_when_there_is_something_to_label(self):
        with self.conn:
            self.conn.execute("UPDATE connector_accounts SET scopes_json=?", (json.dumps(SCOPES),))
        self.assertEqual(self.run_pass(), {"state": "ok", "detail": {"labelled": 0}})
        self.seeded(1)
        self.assertEqual(self.run_pass(), {"state": "needs_label_permission"})
        self.assertEqual(self.gmail.requests, [], "no call at all: the account is known and nothing can be labelled")
        self.assertEqual(self.row("m-00")["label_name"], "", "still waiting for the permission")

    def test_a_held_back_gmail_is_not_asked(self):
        self.seeded(1)
        outreach_gmail._BACKOFF[USER] = (datetime.now(timezone.utc) + timedelta(minutes=5), 1)
        self.assertEqual(self.run_pass(), {"state": "throttled"})
        self.assertEqual(self.gmail.requests, [])

    def test_a_disconnected_gmail_is_not_asked(self):
        self.seeded(1)
        with self.conn:
            self.conn.execute("UPDATE connector_accounts SET status='disconnected'")
        self.assertEqual(self.run_pass(), {"state": "not_connected"})
        with self.conn:
            self.conn.execute("DELETE FROM connector_accounts")
        self.assertEqual(self.run_pass(), {"state": "not_connected"})
        self.assertEqual(self.gmail.requests, [])

    def test_a_connection_in_error_asks_for_a_reconnect_without_a_call(self):
        self.seeded(1)
        with self.conn:
            self.conn.execute("UPDATE connector_accounts SET status='error'")
        self.assertEqual(self.run_pass(), {"state": "needs_reconnect"})
        self.assertEqual(self.gmail.requests, [])

    def test_a_pass_never_leaves_a_transaction_open(self):
        self.seeded(1)
        self.run_pass()
        self.assertFalse(self.conn.in_transaction)

    # --- The account ------------------------------------------------------------------


class AccountTests(LabelCase):
    account_email = ""

    def test_the_account_is_read_once_and_kept(self):
        self.assertEqual(self.run_pass(), {"state": "ok", "detail": {"labelled": 0}})
        self.assertEqual(self.paths(), ["GET /profile"])
        self.assertEqual(self.conn.execute("SELECT account_email FROM connector_accounts").fetchone()[0], ACCOUNT)
        self.run_pass()
        self.assertEqual(self.paths(), ["GET /profile"], "one call per connection")

    def test_the_account_is_read_even_with_labelling_off_and_on_a_connection_that_cannot_label(self):
        with self.conn:
            self.conn.execute("UPDATE connector_accounts SET scopes_json=?", (json.dumps(SCOPES),))
        outreach_labels.set_label_name(self.conn, USER, "")
        self.assertEqual(self.run_pass()["detail"], {"labels": "off"})
        self.assertEqual(self.paths(), ["GET /profile"])
        self.assertEqual(self.conn.execute("SELECT account_email FROM connector_accounts").fetchone()[0], ACCOUNT)

    def test_a_connection_to_another_google_account_labels_nothing(self):
        self.gmail.profile_email = "someone.else@elsewhere.example"
        self.seeded(2)
        self.assertEqual(self.run_pass(), {"state": "wrong_account"})
        self.assertEqual(self.paths(), ["GET /profile"])
        self.assertEqual(self.conn.execute("SELECT account_email FROM connector_accounts").fetchone()[0], "someone.else@elsewhere.example")
        self.assertEqual(self.run_pass(), {"state": "wrong_account"})
        self.assertEqual(self.paths(), ["GET /profile"], "known now, and still wrong, without another call")
        self.assertEqual((self.gmail.batch_modifies, self.gmail.label_creates), ([], []))

    def test_the_address_is_compared_without_regard_to_case(self):
        self.gmail.profile_email = ACCOUNT.upper()
        self.seeded(1)
        self.assertEqual(self.run_pass()["state"], "ok")
        self.assertEqual(len(self.modifies()), 1)

    def test_a_profile_gmail_will_not_give_is_unreachable_and_asked_again(self):
        self.gmail.read_response = lambda: httpx.Response(404)
        self.assertEqual(self.run_pass(), {"state": "unreachable"})
        self.assertEqual(self.conn.execute("SELECT account_email FROM connector_accounts").fetchone()[0], "")
        self.gmail.read_response = None
        self.assertEqual(self.run_pass()["state"], "ok")
        self.assertEqual(self.conn.execute("SELECT account_email FROM connector_accounts").fetchone()[0], ACCOUNT)

    def test_a_rate_limited_profile_is_throttled(self):
        self.gmail.read_response = rate_limited
        self.assertEqual(self.run_pass(), {"state": "throttled"})

    def test_no_call_to_gmail_is_made_inside_a_transaction(self):
        """PostgreSQL opens a transaction on any statement, a SELECT included; SQLite does not, so this one does."""

        wrapped, inside = OpensOnAnyStatement(self.conn), []

        def handler(request):
            inside.append((request.url.path.rsplit("/", 1)[-1], wrapped.in_transaction))
            return self.gmail.handler(request)

        self.seeded(1)
        result = outreach_labels.label_replies(
            wrapped, user_id=USER, now=START, client_factory=lambda: httpx.Client(transport=httpx.MockTransport(handler)),
        )
        self.assertEqual(result["state"], "ok")
        self.assertEqual(inside[0], ("profile", False), "the account is asked before anything is open")
        self.assertEqual([path for path, opened in inside if opened], [])


class WhichRepliesTests(LabelCase):
    def test_only_a_confirmed_reply_is_labelled(self):
        for number, kind in enumerate(("possible", "ignored", "automatic", "dismissed")):
            self.thread(f"t-{kind}", f"m-{kind}")
            self.reply(f"m-{kind}", f"t-{kind}", kind=kind, received=f"2026-09-20T10:0{number}:00+00:00")
        self.assertEqual(self.run_pass(), {"state": "ok", "detail": {"labelled": 0}})
        self.assertEqual(self.gmail.requests, [])

    def possible_reply(self):
        now = utc_now()
        with self.conn:
            self.conn.execute(
                "INSERT INTO outreach_targets(id, user_id, company, contact_email, created_at, updated_at) VALUES('t-1', ?, 'Bovi', "
                "'greg@bovi.example', ?, ?)", (USER, now, now),
            )
        self.thread("t-1", "m-1", ("m-mine", ["SENT"]))
        self.reply("m-1", "t-1", kind="possible", text="Could you send your availability?", subject="Hello", via="domain",
                   reason="shared_address", candidates_json='["t-1"]')
        self.assertEqual(self.run_pass(), {"state": "ok", "detail": {"labelled": 0}})

    def assert_labelled_on_the_next_pass(self):
        self.assertEqual(self.row("m-1")["kind"], "reply")
        result = self.run_pass()
        self.assertEqual((result["state"], result["detail"]["labelled"]), ("ok", 1))
        self.assertEqual([body["ids"] for body in self.modifies()], [["m-1", "m-mine"]])
        self.assertEqual(self.row("m-1")["label_name"], LABEL)

    def test_a_possible_reply_the_student_confirms_is_labelled_on_the_next_pass(self):
        self.possible_reply()
        decide_possible_reply(self.conn, "t-1", "m-1", "reply", user_id=USER)
        self.conn.commit()
        self.assert_labelled_on_the_next_pass()

    def test_a_possible_reply_the_student_pastes_is_labelled_on_the_next_pass(self):
        self.possible_reply()
        outreach.log_reply(self.conn, "t-1", "Could you send your availability?", user_id=USER)
        self.conn.commit()
        self.assert_labelled_on_the_next_pass()

    def test_a_possible_reply_the_student_dismisses_is_never_labelled(self):
        now = utc_now()
        with self.conn:
            self.conn.execute(
                "INSERT INTO outreach_targets(id, user_id, company, contact_email, created_at, updated_at) VALUES('t-1', ?, 'Bovi', "
                "'greg@bovi.example', ?, ?)", (USER, now, now),
            )
        self.thread("t-1", "m-1")
        self.reply("m-1", "t-1", kind="possible", text="Newsletter", subject="Hello", via="domain", reason="shared_address",
                   candidates_json='["t-1"]')
        decide_possible_reply(self.conn, "t-1", "m-1", "not_reply", user_id=USER)
        self.conn.commit()
        self.assertEqual(self.run_pass(), {"state": "ok", "detail": {"labelled": 0}})
        self.assertEqual(self.gmail.requests, [])

    def test_another_students_replies_are_left_alone(self):
        stamp = utc_now()
        with self.conn:
            self.conn.execute("INSERT INTO users(id, email, display_name, role, created_at, updated_at) VALUES('student-2', NULL, 'S', 'student', ?, ?)",
                              (stamp, stamp))
        self.thread("t-2", "m-2")
        self.reply("m-2", "t-2", user_id="student-2")
        self.assertEqual(self.run_pass(), {"state": "ok", "detail": {"labelled": 0}})
        self.assertEqual(self.gmail.requests, [])


class BackfillTests(LabelCase):
    def test_thirty_replies_are_labelled_twenty_five_threads_a_pass(self):
        self.seeded(30)
        first = self.run_pass()
        self.assertEqual((first["state"], first["detail"]["labelled"], first["detail"]["more"]), ("ok", 25, True))
        self.assertEqual(len(self.modifies()), 25)
        self.assertEqual(sum(1 for number in range(30) if self.row(f"m-{number:02d}")["label_name"] == LABEL), 25)
        self.assertEqual(self.row("m-00")["label_name"], LABEL, "the oldest first")
        self.assertEqual(self.row("m-29")["label_name"], "")
        second = self.run_pass(START + timedelta(minutes=3))
        self.assertEqual((second["detail"]["labelled"], second["detail"]["more"]), (5, False))
        self.assertEqual(len(self.modifies()), 30)
        self.assertEqual(self.row("m-29")["label_name"], LABEL)

    def test_two_replies_in_one_thread_are_one_call_and_both_are_marked(self):
        self.thread("t-1", "m-1", "m-2", ("m-3", ["SENT"]))
        self.reply("m-1", "t-1", received="2026-09-20T10:00:00+00:00")
        self.reply("m-2", "t-1", received="2026-09-20T10:05:00+00:00")
        result = self.run_pass()
        self.assertEqual(len(self.modifies()), 1)
        self.assertEqual(self.modifies()[0]["ids"], ["m-1", "m-2", "m-3"])
        self.assertEqual((self.row("m-1")["label_name"], self.row("m-2")["label_name"]), (LABEL, LABEL))
        self.assertEqual(result["detail"]["labelled"], 2)
        self.assertEqual(self.gmail.thread_gets, [("t-1", *THREAD_READ)], "the thread is read once")

    def test_a_reply_whose_message_gmail_no_longer_has_is_set_aside_as_gone_and_never_claimed_labelled(self):
        self.thread("t-1", "m-1")
        self.reply("m-1", "t-1")
        self.reply("m-unlisted", "t-1", received="2026-09-21T10:00:00+00:00")
        result = self.run_pass()
        self.assertEqual(self.row("m-1")["label_name"], LABEL)
        unlisted = self.row("m-unlisted")
        self.assertEqual((unlisted["label_name"], unlisted["labeled_at"]), (LABEL, None), "settled, so it leaves the queue")
        self.assertEqual(unlisted["label_note"], "gone")
        self.assertEqual((result["detail"]["labelled"], result["detail"]["gone"], result["detail"]["failed"]), (1, 1, 0))
        self.assertIn("GET /messages/m-unlisted", self.paths(), "Gmail was asked whether the message is still there")

    def test_a_reply_gmail_has_but_its_thread_does_not_list_is_set_aside_as_failed(self):
        self.thread("t-1", "m-1")
        self.thread("t-elsewhere", "m-unlisted")
        self.reply("m-1", "t-1")
        self.reply("m-unlisted", "t-1", received="2026-09-21T10:00:00+00:00")
        result = self.run_pass()
        unlisted = self.row("m-unlisted")
        self.assertEqual((unlisted["label_name"], unlisted["labeled_at"]), (LABEL, None))
        self.assertEqual(unlisted["label_note"], "failed")
        self.assertEqual((result["detail"]["failed"], result["detail"]["gone"]), (1, 0))
        self.assertEqual([body["ids"] for body in self.modifies()], [["m-1"]], "never labelled on a guess")

    def test_replies_whose_messages_are_gone_from_a_thread_do_not_crowd_out_newer_ones(self):
        self.thread("t-old", ("m-sent", ["SENT"]))
        for number in range(outreach_labels.PER_PASS + 1):
            self.reply(f"m-deleted-{number:02d}", "t-old", received=f"2026-01-01T10:{number:02d}:00+00:00")
        self.thread("t-new", "m-new")
        self.reply("m-new", "t-new")
        first = self.run_pass()
        self.assertEqual((first["detail"]["gone"], first["detail"]["more"]), (outreach_labels.PER_PASS + 1, True))
        self.assertEqual(self.row("m-new")["label_name"], "")
        second = self.run_pass(START + timedelta(minutes=3))
        self.assertEqual((second["detail"]["labelled"], second["detail"]["more"]), (1, False))
        self.assertEqual(self.row("m-new")["label_name"], LABEL)
        self.assertEqual(self.row("m-new")["labeled_at"], (START + timedelta(minutes=3)).isoformat(timespec="seconds"))

    def test_more_says_so_when_pending_rows_are_left_beyond_the_window(self):
        self.seeded(outreach_labels.PER_PASS + 1)
        self.assertTrue(self.run_pass()["detail"]["more"])
        self.assertFalse(self.run_pass(START + timedelta(minutes=3))["detail"]["more"])

    def test_a_reply_without_a_thread_is_read_once_and_its_thread_is_kept(self):
        self.thread("t-1", "m-1")
        self.reply("m-1", "")
        self.run_pass()
        self.assertEqual(self.row("m-1")["thread_id"], "t-1")
        self.assertEqual(self.row("m-1")["label_name"], LABEL)
        self.assertIn("GET /messages/m-1", self.paths())
        self.assertEqual(len(self.modifies()), 1)

    def test_a_reply_gmail_no_longer_has_is_set_aside_as_gone(self):
        self.reply("m-gone", "")
        result = self.run_pass()
        row = self.row("m-gone")
        self.assertEqual((row["label_name"], row["labeled_at"]), (LABEL, None), "settled, and never claimed to be labelled")
        self.assertEqual(row["label_note"], "gone")
        self.assertEqual((result["state"], result["detail"]["gone"], result["detail"]["labelled"]), ("ok", 1, 0))
        self.assertEqual(self.modifies(), [])

    def test_a_thread_gmail_no_longer_has_is_set_aside_as_gone(self):
        self.gmail.gone_threads.add("t-gone")
        self.reply("m-1", "t-gone")
        self.thread("t-2", "m-2")
        self.reply("m-2", "t-2", received="2026-09-21T10:00:00+00:00")
        result = self.run_pass()
        self.assertEqual((self.row("m-1")["labeled_at"], self.row("m-2")["label_name"]), (None, LABEL))
        self.assertEqual((self.row("m-1")["label_note"], self.row("m-2")["label_note"]), ("gone", ""))
        self.assertEqual((result["detail"]["gone"], result["detail"]["labelled"]), (1, 1))

    def test_one_thread_gmail_refuses_never_holds_back_the_rows_behind_it(self):
        self.seeded(3)
        self.gmail.modify_answers.append(lambda: httpx.Response(400, json={"error": {"code": 400, "message": "Invalid value"}}))
        result = self.run_pass()
        self.assertEqual((result["state"], result["detail"]["failed"], result["detail"]["labelled"]), ("ok", 1, 2))
        first = self.row("m-00")
        self.assertEqual((first["label_name"], first["labeled_at"], first["label_note"]), (LABEL, None, "failed"))
        self.assertIsNotNone(self.row("m-01")["labeled_at"])
        self.assertIsNotNone(self.row("m-02")["labeled_at"])
        self.assertEqual(len(self.modifies()), 3)

    def test_a_thread_of_more_than_a_thousand_messages_goes_in_batches(self):
        self.thread("t-big", *[f"b-{number:04d}" for number in range(1001)])
        self.reply("b-0000", "t-big")
        self.run_pass()
        self.assertEqual([len(body["ids"]) for body in self.modifies()], [1000, 1])


class RenameTests(LabelCase):
    def test_a_new_name_relabels_and_never_takes_the_old_label_off(self):
        self.thread("t-1", "m-1", ("m-2", ["SENT"]))
        self.reply("m-1", "t-1")
        self.run_pass()
        old = self.gmail.label_named(LABEL)
        outreach_labels.set_label_name(self.conn, USER, "Jobs")
        result = self.run_pass(START + timedelta(minutes=3))
        new = self.gmail.label_named("Jobs")
        self.assertEqual((result["state"], result["detail"]["labelled"], result["detail"]["relabelled"]), ("ok", 1, 1))
        self.assertEqual(self.modifies()[-1], {"ids": ["m-1", "m-2"], "addLabelIds": [new["id"]]})
        self.assertFalse([body for body in self.modifies() if "removeLabelIds" in body])
        self.assertEqual(self.row("m-1")["label_name"], "Jobs")
        held = {message["id"]: message["labelIds"] for message in self.gmail.label_threads["t-1"]}
        self.assertIn(old["id"], held["m-1"], "the old label stays where it was put")
        self.assertIn(new["id"], held["m-1"])
        self.assertIsNotNone(self.gmail.label_named(LABEL), "and is not deleted")

    def test_the_sweep_starts_over_under_the_new_name(self):
        self.thread("t-1", "m-1")
        self.reply("m-1", "t-1")
        self.run_pass()
        self.assertEqual(json.loads(self.setting(outreach_labels.SWEEP_SETTING))["label"], LABEL)
        outreach_labels.set_label_name(self.conn, USER, "Jobs")
        later = START + timedelta(minutes=30)
        self.run_pass(later)
        kept = json.loads(self.setting(outreach_labels.SWEEP_SETTING))
        self.assertEqual(kept, {"label": "Jobs", "after": int(later.timestamp())})
        self.assertFalse([query for query in self.gmail.searches if query.startswith("after:")], "the first pass under a name reads no listing")


class SweepTests(LabelCase):
    def labelled_thread(self):
        self.thread("t-1", "m-1")
        self.reply("m-1", "t-1")
        self.run_pass()
        self.gmail.requests.clear()

    def sweep_query(self, after):
        """What the sweep asks Gmail for: mail since the last pass, without drafts, delivery notices and what already has the label."""
        return (f"after:{int(after.timestamp()) - outreach_labels.SWEEP_OVERLAP_SECONDS} -in:chats -in:drafts "
                f"-from:({DAEMONS}) -label:{LABEL}")

    def test_the_first_pass_only_records_where_to_start(self):
        self.thread("t-1", "m-1")
        self.reply("m-1", "t-1")
        self.run_pass()
        self.assertEqual(json.loads(self.setting(outreach_labels.SWEEP_SETTING)), {"label": LABEL, "after": int(START.timestamp())})
        self.assertEqual(self.gmail.searches, [])

    def test_a_message_that_joins_a_labelled_thread_later_is_labelled(self):
        self.labelled_thread()
        label_id = self.gmail.label_named(LABEL)["id"]
        # The thank-you the app sent, and a draft the student started, in the same thread.
        self.gmail.label_threads["t-1"] += [{"id": "m-thanks", "labelIds": ["SENT"]}, {"id": "d-9", "labelIds": ["DRAFT"]}]
        self.gmail.inbox_replies.append("m-thanks")
        self.gmail.threads["m-thanks"] = "t-1"
        later = START + timedelta(minutes=10)
        result = self.run_pass(later)
        self.assertEqual(result["state"], "ok")
        self.assertEqual(self.gmail.searches, [self.sweep_query(START)])
        self.assertEqual(self.modifies()[-1], {"ids": ["m-thanks"], "addLabelIds": [label_id]}, "never the draft")
        self.assertEqual(json.loads(self.setting(outreach_labels.SWEEP_SETTING))["after"], int(later.timestamp()))

    def test_a_delivery_notice_joining_a_labelled_thread_is_not_listed_so_the_thread_is_not_read_for_it(self):
        self.labelled_thread()
        self.gmail.thread_gets.clear()
        self.gmail.label_threads["t-1"].append(failure_notice("dsn-1"))
        self.gmail.inbox_replies.append("dsn-1")
        self.gmail.threads["dsn-1"] = "t-1"
        self.run_pass(START + timedelta(minutes=10))
        self.assertEqual(self.gmail.searches[0], self.sweep_query(START))
        self.assertIn(f"-from:({DAEMONS})", self.gmail.searches[0])
        self.assertEqual(self.gmail.thread_gets, [])
        self.assertEqual(len(self.modifies()), 1)

    def test_a_notice_from_any_daemon_address_is_not_listed_either(self):
        self.labelled_thread()
        self.gmail.thread_gets.clear()
        self.gmail.label_threads["t-1"].append({"id": "dsn-2", "labelIds": ["INBOX"], "payload": {"headers": [
            {"name": "From", "value": "Mail Delivery <mail-daemon@bovi.example>"}, {"name": "Subject", "value": "Undelivered"}]}})
        self.gmail.inbox_replies.append("dsn-2")
        self.gmail.threads["dsn-2"] = "t-1"
        self.run_pass(START + timedelta(minutes=10))
        self.assertEqual(self.gmail.thread_gets, [], "listed, the thread would have been read for a notice that is never labelled")
        self.assertEqual(len(self.modifies()), 1)

    def test_a_thread_the_app_never_labelled_is_left_alone(self):
        self.labelled_thread()
        self.thread("t-other", "m-other")
        self.gmail.inbox_replies.append("m-other")
        self.gmail.threads["m-other"] = "t-other"
        self.run_pass(START + timedelta(minutes=10))
        self.assertEqual(len(self.modifies()), 1)
        self.assertEqual(self.gmail.searches, [self.sweep_query(START)], "the sweep listed what came in")
        self.assertEqual(self.gmail.thread_gets, [("t-1", *THREAD_READ)], "only the first pass read a thread; t-other never")

    def test_a_throttled_pass_does_not_move_the_start_forward(self):
        self.labelled_thread()
        before = self.setting(outreach_labels.SWEEP_SETTING)
        self.gmail.read_response = rate_limited
        self.assertEqual(self.run_pass(START + timedelta(minutes=10)), {"state": "throttled"})
        self.assertEqual(self.setting(outreach_labels.SWEEP_SETTING), before)

    def test_a_listing_too_long_for_one_pass_says_so_and_still_moves_on(self):
        self.labelled_thread()
        self.gmail.page_size = 1
        self.gmail.inbox_replies.extend(f"x-{number}" for number in range(outreach_labels.SWEEP_PAGES + 2))
        later = START + timedelta(minutes=10)
        result = self.run_pass(later)
        self.assertTrue(result["detail"].get("sweep_truncated"))
        self.assertEqual(len([query for query in self.gmail.searches if query.startswith("after:")]), outreach_labels.SWEEP_PAGES)
        self.assertEqual(
            json.loads(self.setting(outreach_labels.SWEEP_SETTING)), {"label": LABEL, "after": int(later.timestamp()), "recheck": ""},
            "moved on, and every labelled thread is to be read again",
        )

    def test_what_a_too_long_listing_missed_is_found_by_reading_the_labelled_threads_again(self):
        self.labelled_thread()
        self.gmail.page_size = 1
        self.gmail.inbox_replies.extend(f"x-{number}" for number in range(outreach_labels.SWEEP_PAGES + 2))
        # A message that joined the labelled thread, past the pages the listing reached.
        self.gmail.label_threads["t-1"].append({"id": "m-late", "labelIds": ["SENT"]})
        self.run_pass(START + timedelta(minutes=10))
        self.assertNotIn("m-late", [message for body in self.modifies() for message in body["ids"]])
        self.gmail.page_size = None
        later = START + timedelta(minutes=13)
        result = self.run_pass(later)
        self.assertEqual(self.modifies()[-1]["ids"], ["m-late"])
        self.assertFalse(result["detail"].get("sweep_truncated"))
        self.assertEqual(json.loads(self.setting(outreach_labels.SWEEP_SETTING)), {"label": LABEL, "after": int(later.timestamp())})

    def test_the_reading_again_goes_on_where_it_stopped_when_a_pass_runs_out_of_threads(self):
        self.seeded(3)
        self.run_pass()
        for number in range(3):
            self.gmail.label_threads[f"m-thread-{number:02d}"].append({"id": f"late-{number}", "labelIds": ["SENT"]})
        self.gmail.page_size = 1
        self.gmail.inbox_replies.extend(f"x-{number}" for number in range(outreach_labels.SWEEP_PAGES + 2))
        self.run_pass(START + timedelta(minutes=10))
        self.gmail.page_size = None
        with mock.patch.object(outreach_labels, "PER_PASS", 2):
            second = self.run_pass(START + timedelta(minutes=13))
            self.assertTrue(second["detail"]["more"])
            self.assertEqual(json.loads(self.setting(outreach_labels.SWEEP_SETTING))["recheck"], "m-thread-01")
            self.run_pass(START + timedelta(minutes=16))
        labelled = {message for body in self.modifies() for message in body["ids"]}
        self.assertTrue({f"late-{number}" for number in range(3)} <= labelled)
        self.assertNotIn("recheck", json.loads(self.setting(outreach_labels.SWEEP_SETTING)))

    def test_a_pass_cut_short_by_its_thread_budget_carries_on_where_it_stopped(self):
        self.seeded(30)
        self.run_pass()
        self.run_pass(START + timedelta(minutes=3))
        self.assertEqual(sum(1 for number in range(30) if self.row(f"m-{number:02d}")["label_name"] == LABEL), 30)
        # Every labelled thread gets one more message, more of them than one pass may read.
        for number in range(30):
            self.gmail.label_threads[f"m-thread-{number:02d}"].append({"id": f"n-{number:02d}", "labelIds": ["SENT"]})
            self.gmail.inbox_replies.append(f"n-{number:02d}")
            self.gmail.threads[f"n-{number:02d}"] = f"m-thread-{number:02d}"
        for extra in (1, 2):
            self.run_pass(START + timedelta(minutes=3 + 3 * extra))
        labelled = {message for body in self.modifies() for message in body["ids"]}
        self.assertTrue({f"n-{number:02d}" for number in range(30)} <= labelled, "every one within two more passes")
        self.assertEqual(
            json.loads(self.setting(outreach_labels.SWEEP_SETTING))["after"], int((START + timedelta(minutes=9)).timestamp()),
            "and the start moved once the listing was through",
        )

    def test_a_first_pass_that_stops_early_still_leaves_a_start_that_predates_what_it_labelled(self):
        self.seeded(2)
        self.gmail.modify_answers.extend([lambda: httpx.Response(204), lambda: rate_limited(status=429)])
        self.assertEqual(self.run_pass()["state"], "throttled")
        self.assertEqual(json.loads(self.setting(outreach_labels.SWEEP_SETTING)), {"label": LABEL, "after": int(START.timestamp())})
        # The thank-you lands in the thread the stopped pass labelled, before the next pass starts.
        self.gmail.label_threads["m-thread-00"].append({"id": "m-thanks", "labelIds": ["SENT"]})
        self.gmail.inbox_replies.append("m-thanks")
        self.gmail.threads["m-thanks"] = "m-thread-00"
        outreach_gmail._BACKOFF.clear()
        with self.conn:
            self.conn.execute("UPDATE connector_accounts SET backoff_until=NULL, last_error=''")
        result = self.run_pass(START + timedelta(minutes=10))
        self.assertEqual(result["state"], "ok")
        self.assertIn("m-thanks", [message for body in self.modifies() for message in body["ids"]])


class StopTests(LabelCase):
    def test_a_rate_limit_on_the_label_call_stops_the_pass_and_holds_gmail_back(self):
        self.seeded(3)
        self.gmail.modify_answers.append(lambda: rate_limited(status=429))
        result = self.run_pass()
        self.assertEqual(result["state"], "throttled")
        self.assertEqual(len(self.modifies()), 1, "the rows behind it were not tried")
        self.assertIsNotNone(outreach_gmail.backoff_until(USER))
        self.assertEqual([self.row(f"m-{number:02d}")["label_name"] for number in range(3)], ["", "", ""])
        self.assertEqual(self.run_pass(), {"state": "throttled"})
        self.assertEqual(len(self.modifies()), 1)

    def test_a_hold_that_starts_mid_pass_stops_before_the_next_thread(self):
        self.seeded(3)

        def refused_and_held():
            # Something else on this student's Gmail was told to slow down while this call was in flight.
            outreach_gmail._BACKOFF[USER] = (datetime.now(timezone.utc) + timedelta(minutes=5), 1)
            return httpx.Response(400, json={"error": {"code": 400, "message": "Invalid value"}})

        self.gmail.modify_answers.append(refused_and_held)
        result = self.run_pass()
        self.assertEqual(result["state"], "throttled")
        self.assertEqual(len(self.modifies()), 1, "the threads behind it were not tried")

    def test_a_hold_that_starts_on_the_label_listing_stops_before_the_label_is_created(self):
        self.seeded(1)
        real = outreach_labels._Labeller._refuse
        calls = []

        def refuse_then_hold(labeller, response):
            real(labeller, response)
            if not calls:
                calls.append(1)
                outreach_gmail._BACKOFF[USER] = (datetime.now(timezone.utc) + timedelta(minutes=5), 1)

        with mock.patch.object(outreach_labels._Labeller, "_refuse", refuse_then_hold):
            self.assertEqual(self.run_pass()["state"], "throttled")
        self.assertEqual(self.gmail.label_creates, [], "checked before the label is created")
        self.assertEqual(self.modifies(), [])

    def test_a_hold_between_reading_a_thread_and_changing_it_stops_before_the_call(self):
        self.seeded(1)
        real = outreach_labels._Labeller._refuse
        calls = []

        def refuse_then_hold(labeller, response):
            real(labeller, response)
            if not calls and "/threads/" in response.request.url.path:
                calls.append(1)
                outreach_gmail._BACKOFF[USER] = (datetime.now(timezone.utc) + timedelta(minutes=5), 1)

        with mock.patch.object(outreach_labels._Labeller, "_refuse", refuse_then_hold):
            self.assertEqual(self.run_pass()["state"], "throttled")
        self.assertTrue(calls, "the hold began on the thread read")
        self.assertEqual(self.modifies(), [], "checked before every call that changes anything")

    def test_rows_settled_before_a_stop_stay_settled(self):
        self.seeded(3)
        self.gmail.modify_answers.extend([lambda: httpx.Response(204), lambda: rate_limited(status=429)])
        result = self.run_pass()
        self.assertEqual(result["state"], "throttled")
        self.assertEqual([self.row(f"m-{number:02d}")["label_name"] for number in range(3)], [LABEL, "", ""])
        self.assertEqual(result["detail"]["labelled"], 1)

    def test_a_refused_permission_asks_for_a_reconnect_of_the_permission(self):
        self.seeded(2)
        self.gmail.modify_answers.append(insufficient)
        self.assertEqual(self.run_pass()["state"], "needs_label_permission")
        self.assertEqual(len(self.modifies()), 1)
        self.assertEqual(self.row("m-00")["label_name"], "")

    def test_an_invalid_label_is_found_again_once_and_the_call_retried(self):
        real = self.gmail.add_label(LABEL)
        outreach_labels._IDS[(USER, ACCOUNT.casefold(), LABEL)] = "Label_gone"
        self.seeded(1)
        result = self.run_pass()
        self.assertEqual(result["detail"]["labelled"], 1)
        self.assertEqual([body["addLabelIds"] for body in self.modifies()], [["Label_gone"], [real["id"]]])
        self.assertEqual(outreach_labels._IDS[(USER, ACCOUNT.casefold(), LABEL)], real["id"])

    def test_a_label_still_invalid_after_the_retry_is_a_failed_thread_not_a_loop(self):
        self.gmail.add_label(LABEL)
        self.seeded(1)
        self.gmail.modify_answers.extend([invalid_label, invalid_label])
        result = self.run_pass()
        self.assertEqual((result["state"], result["detail"]["failed"], result["detail"]["labelled"]), ("ok", 1, 0))
        self.assertEqual(len(self.modifies()), 2)

    def test_a_name_gmail_refuses_to_create_is_label_refused(self):
        self.seeded(1)
        self.gmail.create_answers.append(lambda: httpx.Response(400, json={"error": {"code": 400, "message": "Invalid label name"}}))
        self.assertEqual(self.run_pass()["state"], "label_refused")
        self.assertEqual(self.modifies(), [])
        self.assertEqual(self.row("m-00")["label_name"], "")

    def test_a_name_gmail_says_exists_but_does_not_list_is_label_refused(self):
        self.seeded(1)
        self.gmail.create_answers.append(lambda: httpx.Response(409, json={"error": {"code": 409, "message": "Label name exists or conflicts"}}))
        self.assertEqual(self.run_pass()["state"], "label_refused")
        self.assertEqual(self.gmail.label_gets, 2, "listed again once, and still not there")

    def test_a_name_gmail_says_exists_is_found_by_listing_again(self):
        self.seeded(1)

        def made_meanwhile():
            self.gmail.add_label(LABEL)
            return httpx.Response(409, json={"error": {"code": 409, "message": "Label name exists or conflicts"}})

        self.gmail.create_answers.append(made_meanwhile)
        self.assertEqual(self.run_pass()["detail"]["labelled"], 1)

    def test_a_rate_limited_label_create_is_throttled(self):
        self.seeded(1)
        self.gmail.create_answers.append(lambda: rate_limited(status=429))
        self.assertEqual(self.run_pass()["state"], "throttled")

    def test_a_refused_label_create_permission_asks_for_the_permission(self):
        self.seeded(1)
        self.gmail.create_answers.append(insufficient)
        self.assertEqual(self.run_pass()["state"], "needs_label_permission")

    def test_an_unreachable_gmail_is_unreachable(self):
        self.seeded(1)

        def down():
            raise httpx.ConnectError("no route")

        self.gmail.thread_answers.append(down)
        self.assertEqual(self.run_pass()["state"], "unreachable")
        self.assertFalse(self.conn.in_transaction)

    def test_a_revoked_connection_asks_for_a_reconnect(self):
        self.seeded(1)
        self.gmail.expired_tokens.add("valid-token")
        self.gmail.refresh_ok = False
        self.assertEqual(self.run_pass()["state"], "needs_reconnect")


class WatcherTests(LabelCase):
    def watcher(self):
        return InboxWatcher(self.platform_path, client_factory=self.factory, decisions_for=lambda conn, user_id: None)

    def health(self):
        with closing(connect_product(self.platform_path)) as conn:
            return {row["component"]: dict(row) for row in conn.execute("SELECT * FROM automation_health WHERE user_id=?", (USER,)).fetchall()}

    def test_the_label_step_runs_right_after_the_replies_and_records_its_health(self):
        self.seeded(1)
        outreach_inbox._LAST_CAPTURE.clear()
        recorded = []
        real = automation.record_health

        def record(conn, user_id, component, **kwargs):
            recorded.append(component)
            return real(conn, user_id, component, **kwargs)

        with mock.patch.object(automation, "record_health", record):
            self.watcher().run_once()
        steps = [component for component in recorded if component.startswith("inbox.") and component != "inbox.connection"]
        self.assertEqual(steps[:4], ["inbox.sends", "inbox.deliveries", "inbox.replies", outreach_labels.HEALTH_COMPONENT])
        self.assertEqual(outreach_labels.HEALTH_COMPONENT, "inbox.labels")
        health = self.health()["inbox.labels"]
        self.assertEqual(health["last_error"], "")
        self.assertIsNotNone(health["last_ok_at"])
        self.assertEqual(json.loads(health["detail_json"])["labelled"], 1)
        self.assertEqual(self.row("m-00")["label_name"], LABEL)

    def test_a_label_step_that_stops_says_why_without_an_address(self):
        expected = {
            "needs_label_permission": "Reconnect Gmail and tick the permission Google lists as reading, composing and sending, "
                                      "so the app can label your outreach threads (the emails you send and their replies)",
            "wrong_account": "Gmail is connected as a different account from your outreach address; reconnect with that address",
            "label_refused": "Gmail would not create a label with that name; choose another in Outreach settings",
        }
        for state, text in expected.items():
            self.assertEqual(inbox_watcher.STEP_ERRORS[state], text)
            self.assertNotIn("@", text)
        self.seeded(1)
        with self.conn:
            self.conn.execute("UPDATE connector_accounts SET scopes_json=?", (json.dumps(SCOPES),))
        self.watcher().run_once()
        self.assertEqual(self.health()["inbox.labels"]["last_error"], expected["needs_label_permission"])

    def test_a_label_step_that_raises_does_not_stop_the_others(self):
        outreach_inbox._LAST_CAPTURE.clear()  # another test's capture, this soon before, would skip the replies step
        with mock.patch.object(outreach_labels, "label_replies", side_effect=RuntimeError("boom for greg@bovi.example")):
            self.watcher().run_once()
        health = self.health()
        self.assertEqual(health["inbox.labels"]["last_error"], "RuntimeError: boom for [address]")
        self.assertIsNotNone(health["inbox.replies"]["last_ok_at"])


class EndpointTests(LabelCase):
    def setUp(self):
        super().setUp()
        root = Path(self.tempdir.name)
        app = create_app(
            db_path=self.platform_path, access_token="label-owner", static_dir=STATIC_DIR,
            resume_storage=root / "resumes", capture_storage=root / "captures", interview_storage=root / "interviews",
            outreach_gmail_client_factory=self.factory,
        )
        self.client = TestClient(app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def test_the_setting_is_read_with_the_mailbox_and_permission(self):
        body = self.client.get("/api/v1/outreach/gmail-label", headers=AUTH).json()
        self.assertEqual(body, {
            "value": LABEL, "default": LABEL, "search": LABEL,
            "mailbox": {"connected": True, "connected_as": ACCOUNT, "expected": ACCOUNT}, "permission": True,
        })
        with self.conn:
            self.conn.execute("UPDATE connector_accounts SET scopes_json=?, account_email=''", (json.dumps(SCOPES),))
        body = self.client.get("/api/v1/outreach/gmail-label", headers=AUTH).json()
        self.assertEqual((body["permission"], body["mailbox"]["connected_as"]), (False, ""))
        self.assertEqual(self.gmail.requests, [], "reading the setting never asks Gmail")

    def test_the_setting_is_saved_and_read_back(self):
        saved = self.client.put("/api/v1/outreach/gmail-label", headers=AUTH, json={"value": "Job Search/2026"})
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual((saved.json()["value"], saved.json()["search"]), ("Job Search/2026", "job-search-2026"))
        self.assertEqual(self.client.get("/api/v1/outreach/gmail-label", headers=AUTH).json()["value"], "Job Search/2026")

    def test_null_goes_back_to_the_default_and_empty_turns_it_off_and_stays_off(self):
        self.client.put("/api/v1/outreach/gmail-label", headers=AUTH, json={"value": "Jobs"})
        for payload in ({"value": None}, {}):
            self.client.put("/api/v1/outreach/gmail-label", headers=AUTH, json={"value": "Jobs"})
            back = self.client.put("/api/v1/outreach/gmail-label", headers=AUTH, json=payload).json()
            self.assertEqual((back["value"], back["search"]), (LABEL, LABEL))
        off = self.client.put("/api/v1/outreach/gmail-label", headers=AUTH, json={"value": ""})
        self.assertEqual((off.status_code, off.json()["value"], off.json()["search"]), (200, "", ""))
        again = self.client.get("/api/v1/outreach/gmail-label", headers=AUTH).json()
        self.assertEqual((again["value"], again["default"]), ("", LABEL), "off is remembered, not read as the default")

    def test_a_name_gmail_would_refuse_is_a_422_with_the_sentence(self):
        for bad in ("Inbox", "a//b", "/x", "x" * 101):
            with self.subTest(bad):
                response = self.client.put("/api/v1/outreach/gmail-label", headers=AUTH, json={"value": bad})
                self.assertEqual(response.status_code, 422, response.text)
        response = self.client.put("/api/v1/outreach/gmail-label", headers=AUTH, json={"value": "Inbox"})
        self.assertEqual(response.json()["detail"], "Gmail keeps the name Inbox for itself; choose another label name")
        self.assertEqual(self.client.get("/api/v1/outreach/gmail-label", headers=AUTH).json()["value"], LABEL)

    def test_it_needs_a_signed_in_student(self):
        self.assertEqual(self.client.get("/api/v1/outreach/gmail-label").status_code, 401)
        self.assertEqual(self.client.put("/api/v1/outreach/gmail-label", json={"value": "x"}).status_code, 401)

    def test_it_never_fails_without_gmail(self):
        with self.conn:
            self.conn.execute("DELETE FROM connector_accounts")
        with mock.patch.dict("os.environ", {"PIPELINE_CONNECTION_KEY": "", "PIPELINE_OUTREACH_ACCOUNT": ""}):
            body = self.client.get("/api/v1/outreach/gmail-label", headers=AUTH)
            self.assertEqual(body.status_code, 200, body.text)
            self.assertEqual(body.json()["mailbox"], {"connected": False, "connected_as": "", "expected": ""})
            self.assertFalse(body.json()["permission"])
            self.assertEqual(self.client.put("/api/v1/outreach/gmail-label", headers=AUTH, json={"value": "x"}).status_code, 200)


class SentThreadTests(LabelCase):
    """The emails the student sent are outreach threads too, whether or not anyone answered."""

    def test_an_email_the_app_sent_is_labelled_with_no_reply_and_a_bounce_notice_and_a_draft_are_left_out(self):
        self.target("t-1")
        self.event("t-1", outreach_gmail.SENT_EVENT, {"thread_id": "th-1", "message_id": "s-1", "to": "greg@bovi.example"})
        self.thread("th-1", ("s-1", ["SENT"]), ("d-1", ["DRAFT"]), ("s-2", ["SENT"]))
        self.gmail.label_threads["th-1"].append(failure_notice("dsn-1", failed="greg@bovi.example"))
        result = self.run_pass()
        self.assertEqual(result["state"], "ok")
        label_id = self.gmail.label_named(LABEL)["id"]
        self.assertEqual(self.modifies(), [{"ids": ["s-1", "s-2"], "addLabelIds": [label_id]}],
                         "exactly addLabelIds; never the draft, never the delivery failure notice")
        self.assertEqual(self.gmail.thread_gets, [("th-1", *THREAD_READ)])
        row = self.sent_rows()["th-1"]
        self.assertEqual((row["source"], row["target_id"], row["label_name"], row["labeled_at"], row["label_note"]),
                         ("sent", "t-1", LABEL, START.isoformat(timespec="seconds"), ""))
        self.assertEqual((result["detail"]["sent_labelled"], result["detail"]["labelled"]), (1, 0))
        self.assertFalse([body for body in self.modifies() if "removeLabelIds" in body])

    def test_a_thread_already_settled_is_not_read_again(self):
        self.target("t-1")
        self.event("t-1", outreach_gmail.SENT_EVENT, {"thread_id": "th-1"})
        self.thread("th-1", ("s-1", ["SENT"]))
        self.run_pass()
        self.run_pass(START + timedelta(minutes=3))
        self.assertEqual(len(self.gmail.thread_gets), 1)
        self.assertEqual(len(self.modifies()), 1)

    def test_a_thank_you_the_app_sent_is_a_sent_thread_too(self):
        self.target("t-1", status="declined")
        self.event("t-1", outreach_gmail.THANK_YOU_SENT_EVENT, {"thread_id": "th-ty", "message_id": "s-ty"})
        self.thread("th-ty", ("s-ty", ["SENT"]))
        self.run_pass()
        self.assertEqual(self.modifies()[0]["ids"], ["s-ty"])
        self.assertEqual(self.sent_rows()["th-ty"]["source"], "sent")

    def test_a_follow_up_in_the_same_thread_as_a_reply_is_read_once(self):
        self.target("t-1")
        self.event("t-1", outreach_gmail.SENT_EVENT, {"thread_id": "th-1"})
        self.thread("th-1", ("s-1", ["SENT"]), "m-1")
        self.reply("m-1", "th-1")
        result = self.run_pass()
        self.assertEqual(self.gmail.thread_gets, [("th-1", *THREAD_READ)], "one read, replies first")
        self.assertEqual((result["detail"]["labelled"], result["detail"]["sent_labelled"]), (1, 1))
        self.assertEqual(self.sent_rows()["th-1"]["label_name"], LABEL)

    def test_an_event_without_a_readable_thread_is_skipped(self):
        self.target("t-1", status="drafted")
        for detail in ("not json", "{}", '{"thread_id": ""}', '{"thread_id": null}', "[1, 2]", ""):
            self.event("t-1", outreach_gmail.SENT_EVENT, detail)
        self.event("t-1", outreach_gmail.THANK_YOU_SENT_EVENT, "{oops")
        self.assertEqual(self.run_pass(), {"state": "ok", "detail": {
            "label": LABEL, "labelled": 0, "gone": 0, "failed": 0, "relabelled": 0, "sent_labelled": 0, "searched": 1,
            "found": 0, "more": False}})
        self.assertEqual(self.sent_rows(), {})
        self.assertEqual(self.gmail.thread_gets, [])

    def test_a_thread_gmail_no_longer_has_is_set_aside_as_gone_and_never_claimed_labelled(self):
        self.target("t-1")
        self.event("t-1", outreach_gmail.SENT_EVENT, {"thread_id": "th-1"})
        self.gmail.gone_threads.add("th-1")
        result = self.run_pass()
        row = self.sent_rows()["th-1"]
        self.assertEqual((row["label_name"], row["labeled_at"], row["label_note"]), (LABEL, None, "gone"))
        self.assertEqual(result["detail"]["gone"], 1)
        self.assertEqual(self.modifies(), [])
        self.run_pass(START + timedelta(minutes=3))
        self.assertEqual(len(self.gmail.thread_gets), 1, "not read again")

    def test_a_thread_gmail_refuses_is_set_aside_as_failed_and_does_not_hold_back_the_next(self):
        self.target("t-1")
        self.event("t-1", outreach_gmail.SENT_EVENT, {"thread_id": "th-1"}, created="2026-09-02T09:00:00+00:00")
        self.event("t-1", outreach_gmail.SENT_EVENT, {"thread_id": "th-2"}, created="2026-09-02T09:01:00+00:00")
        self.thread("th-1", ("s-1", ["SENT"]))
        self.thread("th-2", ("s-2", ["SENT"]))
        self.gmail.modify_answers.append(lambda: httpx.Response(400, json={"error": {"code": 400, "message": "Bad ids"}}))
        result = self.run_pass()
        rows = self.sent_rows()
        self.assertEqual((rows["th-1"]["label_note"], rows["th-1"]["labeled_at"]), ("failed", None))
        self.assertEqual((rows["th-2"]["label_note"], rows["th-2"]["labeled_at"] is not None), ("", True))
        self.assertEqual((result["detail"]["failed"], result["detail"]["sent_labelled"]), (1, 1))

    def test_a_bounce_notice_in_a_reply_thread_is_left_out_too(self):
        self.thread("t-1", "m-1", ("s-1", ["SENT"]))
        self.gmail.label_threads["t-1"].append(failure_notice("dsn-1", subject="Undeliverable: hello"))
        self.reply("m-1", "t-1")
        self.run_pass()
        label_id = self.gmail.label_named(LABEL)["id"]
        self.assertEqual(self.modifies(), [{"ids": ["m-1", "s-1"], "addLabelIds": [label_id]}])
        self.assertEqual(self.gmail.thread_gets, [("t-1", *THREAD_READ)])
        self.assertEqual(self.row("m-1")["label_name"], LABEL)

    def test_a_thread_of_nothing_but_a_notice_needs_no_call_and_is_settled(self):
        self.target("t-1")
        self.event("t-1", outreach_gmail.SENT_EVENT, {"thread_id": "th-1"})
        self.gmail.label_threads["th-1"] = [failure_notice("dsn-1")]
        result = self.run_pass()
        self.assertEqual(self.modifies(), [])
        self.assertEqual(result["detail"]["sent_labelled"], 1)

    def test_a_message_that_joins_a_sent_thread_later_is_labelled(self):
        self.target("t-1")
        self.event("t-1", outreach_gmail.SENT_EVENT, {"thread_id": "th-1"})
        self.thread("th-1", ("s-1", ["SENT"]))
        self.run_pass()
        label_id = self.gmail.label_named(LABEL)["id"]
        self.gmail.label_threads["th-1"].append({"id": "s-2", "labelIds": ["SENT"]})
        self.gmail.inbox_replies.append("s-2")
        self.gmail.threads["s-2"] = "th-1"
        self.run_pass(START + timedelta(minutes=10))
        self.assertEqual(self.modifies()[-1], {"ids": ["s-2"], "addLabelIds": [label_id]})

    def test_the_budget_is_shared_with_replies_and_replies_come_first(self):
        self.target("t-1")
        self.seeded(20)
        for number in range(10):
            self.event("t-1", outreach_gmail.SENT_EVENT, {"thread_id": f"sent-thread-{number:02d}"})
            self.thread(f"sent-thread-{number:02d}", (f"sent-{number:02d}", ["SENT"]))
        first = self.run_pass()
        self.assertEqual(len(self.gmail.thread_gets), outreach_labels.PER_PASS)
        self.assertEqual((first["detail"]["labelled"], first["detail"]["sent_labelled"], first["detail"]["more"]), (20, 5, True))
        self.assertEqual(sum(1 for row in self.sent_rows().values() if row["label_name"] == LABEL), 5)
        second = self.run_pass(START + timedelta(minutes=3))
        self.assertEqual((second["detail"]["sent_labelled"], second["detail"]["more"]), (5, False))
        self.assertEqual(sum(1 for row in self.sent_rows().values() if row["label_name"] == LABEL), 10)

    def test_a_new_name_relabels_the_sent_threads_and_searches_every_company_again(self):
        self.target("t-1", contact_email="greg@bovi.example")
        self.event("t-1", outreach_gmail.SENT_EVENT, {"thread_id": "th-1"})
        self.thread("th-1", ("s-1", ["SENT"]))
        self.run_pass()
        self.assertEqual(len(self.history_searches()), 1)
        outreach_labels.set_label_name(self.conn, USER, "Jobs")
        result = self.run_pass(START + timedelta(minutes=3))
        new = self.gmail.label_named("Jobs")
        self.assertEqual(self.modifies()[-1], {"ids": ["s-1"], "addLabelIds": [new["id"]]})
        self.assertFalse([body for body in self.modifies() if "removeLabelIds" in body])
        self.assertEqual(self.sent_rows()["th-1"]["label_name"], "Jobs")
        self.assertEqual(result["detail"]["sent_labelled"], 1)
        self.assertEqual(len(self.history_searches()), 2, "the mail sent while no sweep ran under the new name is searched for")

    def test_only_a_name_that_differs_from_the_last_one_makes_the_companies_be_searched_again(self):
        self.target("t-1", contact_email="greg@bovi.example")
        self.run_pass()
        self.assertEqual(self.searched(), {"t-1": 0})
        outreach_labels.set_label_name(self.conn, USER, LABEL)
        outreach_labels.set_label_name(self.conn, USER, "  " + LABEL + " ")
        self.assertEqual(self.searched(), {"t-1": 0}, "the same name")
        outreach_labels.set_label_name(self.conn, USER, "")
        self.assertEqual(self.searched(), {"t-1": 0}, "turning labelling off searches nothing")
        outreach_labels.set_label_name(self.conn, USER, None)
        self.assertEqual(self.searched(), {}, "labelling starts again: the mail sent while it was off is searched for")
        self.run_pass(START + timedelta(minutes=3))
        self.assertEqual(self.searched(), {"t-1": 0})
        outreach_labels.set_label_name(self.conn, USER, "Jobs")
        self.assertEqual(self.searched(), {})

    def test_labelling_off_and_paused_do_nothing_and_record_nothing(self):
        self.target("t-1")
        self.event("t-1", outreach_gmail.SENT_EVENT, {"thread_id": "th-1"})
        outreach_labels.set_label_name(self.conn, USER, "")
        self.assertEqual(self.run_pass()["detail"], {"labels": "off"})
        outreach_labels.set_label_name(self.conn, USER, None)
        self.pause()
        self.assertEqual(self.run_pass()["detail"], {"labels": "paused"})
        self.assertEqual(self.gmail.requests, [])
        self.assertEqual(self.sent_rows(), {})

    def test_no_call_to_gmail_is_made_inside_a_transaction_by_a_history_search_a_sent_row_or_the_sent_sweep(self):
        """PostgreSQL opens a transaction on any statement, a SELECT included; SQLite does not, so the connection is made to."""
        self.target("t-1", contact_email="greg@bovi.example", email_subject="Robotics internship plan")
        self.event("t-1", outreach_gmail.SENT_EVENT, {"thread_id": "th-1"})
        self.thread("th-1", ("s-1", ["SENT"]))
        SentSweepTests.sent_message(self, "h-1", "th-h", subject="Hello")
        first, asked = self.run_pass_in_postgres_style()
        self.assertEqual(first["state"], "ok")
        self.assertEqual(self.searched(), {"t-1": 1}, "the history search ran, and read what it found")
        self.assertTrue([call for call, _open in asked if call.startswith("GET h-1")])
        self.assertEqual([call for call, opened in asked if opened], [])
        # The next pass lists Sent: one message that is not outreach (read, and passed over) and one that is.
        SentSweepTests.sent_message(self, "n-0", "th-other", to="friend@elsewhere.example", subject="Lunch")
        SentSweepTests.sent_message(self, "n-1", "th-new")
        second, asked = self.run_pass_in_postgres_style(START + timedelta(minutes=10))
        self.assertEqual((second["state"], second["detail"]["sent_labelled"]), ("ok", 1))
        self.assertTrue([call for call, _open in asked if call.startswith("GET messages in:sent")], "the Sent listing ran")
        self.assertTrue([call for call, _open in asked if call.startswith("GET n-0")], "the message that is not outreach was read")
        self.assertEqual([call for call, opened in asked if opened], [])

    def test_a_delay_notice_and_a_notice_from_a_mail_daemon_are_left_out_of_the_label_too(self):
        self.target("t-1")
        self.event("t-1", outreach_gmail.SENT_EVENT, {"thread_id": "th-1"})
        self.thread("th-1", ("s-1", ["SENT"]))
        self.gmail.label_threads["th-1"].append(failure_notice("dsn-1", subject="Delivery Status Notification (Delay)"))
        self.gmail.label_threads["th-1"].append({"id": "dsn-2", "labelIds": ["INBOX"], "payload": {"headers": [
            {"name": "From", "value": "Postmaster <postmaster@bovi.example>"}, {"name": "Subject", "value": "Warning: delayed"}]}})
        self.run_pass()
        label_id = self.gmail.label_named(LABEL)["id"]
        self.assertEqual(self.modifies(), [{"ids": ["s-1"], "addLabelIds": [label_id]}])


class SentPermissionTests(LabelCase):
    scopes = SCOPES

    def test_sent_work_and_an_unsearched_company_ask_for_the_permission_and_call_nothing(self):
        self.assertEqual(self.run_pass(), {"state": "ok", "detail": {"labelled": 0}}, "nothing to label")
        self.target("t-1", status="drafted")
        self.assertEqual(self.run_pass(), {"state": "ok", "detail": {"labelled": 0}}, "a company not yet sent to is no work")
        self.event("t-1", outreach_gmail.SENT_EVENT, {"thread_id": "th-1"})
        self.assertEqual(self.run_pass(), {"state": "needs_label_permission"}, "a sent thread waits for the permission")
        self.assertEqual(self.gmail.requests, [])
        self.assertEqual(self.sent_rows()["th-1"]["label_name"], "", "still waiting")

    def test_a_company_that_has_gone_out_and_was_never_searched_is_work(self):
        self.target("t-1", status="sent", contact_email="greg@bovi.example")
        self.assertEqual(self.run_pass(), {"state": "needs_label_permission"})
        self.assertEqual(self.gmail.requests, [])
        self.assertEqual(self.searched(), {})


class HistorySearchTests(LabelCase):
    """One search of Sent for each company that has gone out: the student often sent from Gmail, not the app."""

    def brace_terms(self, *addresses, subjects=()):
        terms = [f"{prefix}:{address}" for address in addresses for prefix in ("to", "cc", "bcc")]
        return " ".join([*terms, *(f'subject:"{subject}"' for subject in subjects)])

    def test_the_query_covers_every_address_and_subject_of_the_company_since_thirty_days_before_it_was_added(self):
        self.target(
            "t-1", contact_email="Greg@Bovi.example", contact_cc=f"Ann Lee <ann@bovi.example>, {ACCOUNT}",
            bounced_addresses_json=json.dumps(["old@bovi.example", "not an address", "a b@c.example"]),
            email_subject='Robotics "internship" (fall) plan', follow_up_subject="Following up on the robotics plan",
            created_at="2026-09-01T12:00:00+00:00",
        )
        self.event("t-1", outreach_gmail.DRAFT_EVENT, {"to": "draft@bovi.example, Other <other@bovi.example>", "cc": "cc@bovi.example"})
        self.event("t-1", outreach_gmail.SENT_EVENT, {"to": "greg@bovi.example", "thread_id": ""})
        self.run_pass()
        after = int(datetime(2026, 9, 1, 12, tzinfo=timezone.utc).timestamp()) - 30 * 86400
        addresses = ("greg@bovi.example", "ann@bovi.example", "old@bovi.example", "draft@bovi.example",
                     "other@bovi.example", "cc@bovi.example")
        subjects = ("Robotics internship fall plan", "Following up on the robotics plan")
        self.assertEqual(self.history_searches(), [f"in:sent after:{after} {{{self.brace_terms(*addresses, subjects=subjects)}}}"])
        self.assertEqual(self.gmail.search_params[0]["maxResults"], "100")
        self.assertNotIn(ACCOUNT, self.history_searches()[0], "the student's own address is never a term")

    def test_a_short_subject_is_not_a_term_and_a_long_one_is_cut(self):
        self.target("t-1", contact_email="greg@bovi.example", email_subject="Hello there", follow_up_subject="Re: " + "word " * 40)
        self.run_pass()
        query = self.history_searches()[0]
        self.assertNotIn("Hello there", query)
        phrase = re.search(r'subject:"([^"]*)"', query).group(1)
        self.assertLessEqual(len(phrase), outreach_labels.SUBJECT_PHRASE)
        self.assertTrue(phrase.startswith("Re: word word"))
        self.assertTrue(phrase.endswith(" word"), "cut between words: a phrase search matches whole words")
        self.assertEqual(set(phrase.split()) - {"Re:", "word"}, set())

    def test_a_subject_with_no_space_to_cut_at_is_cut_at_the_limit(self):
        self.target("t-1", contact_email="greg@bovi.example", email_subject="x" * 130)
        self.run_pass()
        self.assertEqual(re.search(r'subject:"([^"]*)"', self.history_searches()[0]).group(1), "x" * outreach_labels.SUBJECT_PHRASE)

    def test_the_search_starts_thirty_days_before_the_earlier_of_the_day_it_was_added_and_its_sent_date(self):
        def after(created, sent):
            self.gmail.searches.clear()
            outreach_labels._IDS.clear()
            with self.conn:
                self.conn.execute("DELETE FROM outreach_label_searches")
                self.conn.execute("DELETE FROM outreach_targets")
            self.target("t-1", contact_email="greg@bovi.example", created_at=created, sent_at=sent)
            self.run_pass()
            return int(re.search(r"after:(\d+)", self.history_searches()[-1]).group(1)) + 30 * 86400
        stamp = lambda day: int(datetime(2026, 9, day, 12, tzinfo=timezone.utc).timestamp())
        self.assertEqual(after("2026-09-10T12:00:00+00:00", "2026-09-01T12:00:00+00:00"), stamp(1), "added later than it was sent")
        self.assertEqual(after("2026-09-01T12:00:00+00:00", "2026-09-10T12:00:00+00:00"), stamp(1), "added first")
        self.assertEqual(after("2026-09-03T12:00:00+00:00", None), stamp(3), "never sent through the app")
        self.assertEqual(after("sometime", "2026-09-05T12:00:00+00:00"), stamp(5), "an unreadable date is passed over")

    def test_an_address_only_copied_at_another_domain_is_not_a_term_but_one_at_the_companys_own_domains_is(self):
        self.target("t-1", contact_email="greg@bovi.example", contact_cc="mentor@other.example, hr@mail.bovi.example, pal@gmail.com")
        self.event("t-1", outreach_gmail.SENT_EVENT, {"to": "greg@bovi.example", "cc": "ref@other.example, ann@bovi.example", "thread_id": ""})
        self.run_pass()
        query = self.history_searches()[0]
        for wanted in ("greg@bovi.example", "hr@mail.bovi.example", "ann@bovi.example"):
            self.assertIn(f"to:{wanted}", query)
        for unwanted in ("mentor@other.example", "ref@other.example", "pal@gmail.com"):
            self.assertNotIn(unwanted, query)

    def test_a_company_written_to_at_a_free_mail_address_keeps_it_but_not_other_free_mail_copied(self):
        self.target("t-1", contact_email="greg@gmail.com", contact_cc="other@gmail.com")
        self.run_pass()
        query = self.history_searches()[0]
        self.assertIn("to:greg@gmail.com", query)
        self.assertNotIn("other@gmail.com", query)

    def test_a_company_that_only_has_copied_addresses_at_no_domain_of_its_own_has_nothing_to_search_by(self):
        self.target("t-1", contact_email="", contact_cc="mentor@other.example")
        self.run_pass()
        self.assertEqual(self.history_searches(), [])
        self.assertEqual(self.searched(), {"t-1": 0})

    def test_a_hit_that_is_not_outreach_is_not_kept_when_the_company_is_searched_by_subject_too(self):
        self.target("t-1", contact_email="greg@bovi.example", email_subject="Robotics internship plan")
        for message_id, thread_id, to, subject in (
            ("h-1", "th-1", "Greg <greg@bovi.example>", "Hello"),
            ("h-2", "th-2", "friend@elsewhere.example", "Re: Robotics internship plan"),
            ("h-3", "th-3", "friend@elsewhere.example", "Robotics internship plan for the club"),
        ):
            SentSweepTests.sent_message(self, message_id, thread_id, to=to, subject=subject)
        self.gmail.sent_search.append("h-4")
        self.gmail.threads["h-4"] = "th-4"
        result = self.run_pass()
        self.assertEqual(sorted(self.sent_rows()), ["th-1", "th-2"], "by address, and by the subject as the sweep reads it; not the others")
        self.assertEqual((result["detail"]["found"], self.searched()), (2, {"t-1": 2}))
        self.assertEqual(sorted(body["ids"][0] for body in self.modifies()), ["h-1", "h-2"])

    def test_a_company_with_nothing_to_search_by_is_marked_searched_without_a_call(self):
        self.target("t-1", email_subject="Hi", created_at="2026-09-01T12:00:00+00:00")
        result = self.run_pass()
        self.assertEqual(self.history_searches(), [])
        self.assertEqual(self.searched(), {"t-1": 0})
        self.assertEqual((result["detail"]["searched"], result["detail"]["found"]), (1, 0))
        self.assertEqual(self.gmail.requests, [], "no Gmail call at all")

    def test_a_created_at_that_cannot_be_read_leaves_the_date_out(self):
        self.target("t-1", contact_email="greg@bovi.example", created_at="sometime")
        self.run_pass()
        self.assertEqual(self.history_searches(), [f"in:sent {{{self.brace_terms('greg@bovi.example')}}}"])

    def addressed(self, *message_ids, to="greg@bovi.example"):
        """The recipients Gmail gives when a search hit is checked."""
        for message_id in message_ids:
            self.gmail.metadata[message_id] = {"id": message_id, "payload": {"headers": [{"name": "To", "value": to}]}}

    def test_what_it_finds_is_recorded_and_labelled_and_the_company_is_searched_once(self):
        self.target("t-1", contact_email="greg@bovi.example")
        self.gmail.sent_search.extend(["s-1", "s-2", "s-3"])
        for message_id, thread_id in (("s-1", "th-9"), ("s-2", "th-9"), ("s-3", "th-8")):
            self.gmail.threads[message_id] = thread_id
        self.addressed("s-1", "s-2", "s-3")
        self.thread("th-9", ("s-1", ["SENT"]), ("s-2", ["SENT"]))
        self.thread("th-8", ("s-3", ["SENT"]))
        result = self.run_pass()
        label_id = self.gmail.label_named(LABEL)["id"]
        self.assertEqual(sorted(body["ids"] for body in self.modifies()), [["s-1", "s-2"], ["s-3"]])
        self.assertEqual({row["thread_id"]: (row["source"], row["target_id"]) for row in self.sent_rows().values()},
                         {"th-9": ("search", "t-1"), "th-8": ("search", "t-1")})
        self.assertEqual(self.searched(), {"t-1": 2})
        self.assertEqual((result["detail"]["searched"], result["detail"]["found"], result["detail"]["sent_labelled"]), (1, 2, 2))
        self.assertTrue(all(body["addLabelIds"] == [label_id] for body in self.modifies()))
        self.run_pass(START + timedelta(minutes=3))
        self.assertEqual(len(self.history_searches()), 1, "searched once only")

    def test_no_more_than_the_cap_of_threads_are_kept_for_one_company_and_the_search_says_it_was_cut_short(self):
        self.target("t-1", contact_email="greg@bovi.example")
        for number in range(outreach_labels.SEARCH_THREADS + 5):
            self.gmail.sent_search.append(f"s-{number:03d}")
            self.thread(f"th-{number:03d}", (f"s-{number:03d}", ["SENT"]))
            self.gmail.threads[f"s-{number:03d}"] = f"th-{number:03d}"
            self.addressed(f"s-{number:03d}")
        with self.assertLogs(outreach_labels.LOGGER, "WARNING") as logged:
            result = self.run_pass()
        self.assertEqual(outreach_labels.SEARCH_THREADS, 100)
        self.assertEqual(len(self.sent_rows()), outreach_labels.SEARCH_THREADS)
        self.assertEqual(result["detail"]["found"], outreach_labels.SEARCH_THREADS)
        self.assertTrue(result["detail"]["search_truncated"])
        self.assertEqual(self.searched(), {"t-1": outreach_labels.SEARCH_THREADS}, "still recorded as searched")
        self.assertIn("cut short", logged.output[0])
        self.assertNotIn("greg@bovi.example", logged.output[0])

    def test_a_search_is_paged_a_hundred_at_a_time_and_a_short_one_is_not_flagged(self):
        self.target("t-1", contact_email="greg@bovi.example")
        self.gmail.page_size = 2
        for number in range(5):
            self.gmail.sent_search.append(f"s-{number}")
            self.thread(f"th-{number}", (f"s-{number}", ["SENT"]))
            self.gmail.threads[f"s-{number}"] = f"th-{number}"
            self.addressed(f"s-{number}")
        result = self.run_pass()
        self.assertEqual(sorted(self.sent_rows()), [f"th-{number}" for number in range(5)], "all three pages were read")
        self.assertEqual(len(self.history_searches()), 3)
        self.assertEqual([params.get("pageToken") for params in self.gmail.search_params if "{" in params["q"]], [None, "2", "4"])
        self.assertEqual({params["maxResults"] for params in self.gmail.search_params if "{" in params["q"]}, {"100"})
        self.assertNotIn("search_truncated", result["detail"])

    def test_a_search_with_pages_left_after_the_page_cap_is_recorded_but_says_it_was_cut_short(self):
        self.target("t-1", contact_email="greg@bovi.example")
        self.gmail.page_size = 1
        for number in range(outreach_labels.SEARCH_PAGES + 2):
            self.gmail.sent_search.append(f"s-{number}")
            self.thread(f"th-{number}", (f"s-{number}", ["SENT"]))
            self.gmail.threads[f"s-{number}"] = f"th-{number}"
            self.addressed(f"s-{number}")
        with self.assertLogs(outreach_labels.LOGGER, "WARNING"):
            result = self.run_pass()
        self.assertEqual(len(self.history_searches()), outreach_labels.SEARCH_PAGES)
        self.assertEqual(len(self.sent_rows()), outreach_labels.SEARCH_PAGES)
        self.assertTrue(result["detail"]["search_truncated"])
        self.assertEqual(self.searched(), {"t-1": outreach_labels.SEARCH_PAGES})

    def test_a_hit_to_a_longer_address_gmail_matched_by_its_words_is_not_kept(self):
        self.target("t-1", contact_email="ann@bovi.example")
        self.gmail.sent_search.extend(["s-own", "s-other"])
        self.gmail.threads.update({"s-own": "th-own", "s-other": "th-other"})
        self.thread("th-own", ("s-own", ["SENT"]))
        self.thread("th-other", ("s-other", ["SENT"]))
        self.addressed("s-own", to="Ann <ann@bovi.example>")
        self.addressed("s-other", to="jo.ann@bovi.example")
        self.run_pass()
        self.assertEqual(set(self.sent_rows()), {"th-own"})
        self.assertEqual(self.searched(), {"t-1": 1})

    def test_an_email_sent_before_the_company_was_added_is_found(self):
        self.target("t-1", contact_email="greg@bovi.example", created_at="2026-09-21T12:00:00+00:00")
        # Sent 20 days before it was added (found), and 40 days before (outside the window).
        SentSweepTests.sent_message(self, "early-1", "th-early", sent=datetime(2026, 9, 1, 12, tzinfo=timezone.utc))
        SentSweepTests.sent_message(self, "early-2", "th-earlier", sent=datetime(2026, 8, 12, 12, tzinfo=timezone.utc))
        self.run_pass()
        self.assertEqual(sorted(self.sent_rows()), ["th-early"])
        self.assertEqual(self.searched(), {"t-1": 1})

    def test_a_company_changed_after_its_search_is_searched_once_more_and_an_unchanged_one_is_not(self):
        self.target("t-1", contact_email="greg@bovi.example")
        self.target("t-2", contact_email="ann@bovi.example")
        self.run_pass()
        self.assertEqual(len(self.history_searches()), 2)
        self.run_pass(START + timedelta(minutes=3))
        self.assertEqual(len(self.history_searches()), 2, "nothing changed: nothing searched")
        changed = (START + timedelta(minutes=5)).isoformat(timespec="microseconds")
        with self.conn:
            # An edit that leaves what the company is searched by alone (research notes, say) searches nothing again.
            self.conn.execute("UPDATE outreach_targets SET updated_at=? WHERE id='t-2'", (changed,))
            self.conn.execute("UPDATE outreach_targets SET contact_email=?, updated_at=? WHERE id='t-1'", ("gail@bovi.example", changed))
        second = self.run_pass(START + timedelta(minutes=10))
        self.assertEqual(second["detail"]["searched"], 1)
        searches = self.history_searches()
        self.assertEqual(len(searches), 3)
        self.assertIn("to:gail@bovi.example", searches[-1])
        self.assertNotIn("ann@bovi.example", searches[-1], "only the company that changed")
        self.run_pass(START + timedelta(minutes=13))
        self.assertEqual(len(self.history_searches()), 3, "the new search is not repeated")
        self.assertEqual(self.searched(), {"t-1": 0, "t-2": 0})

    def test_a_subject_two_companies_share_is_not_a_term_so_the_other_companys_mail_cannot_push_the_first_out(self):
        subject = "Robotics internship plan"
        self.target("t-1", contact_email="greg@bovi.example", email_subject=subject, created_at="2026-09-01T12:00:00+00:00")
        self.target("t-2", contact_email="ann@bovi.example", email_subject=f"RE: {subject.upper()}", created_at="2026-09-02T12:00:00+00:00")
        # Gmail lists newest first: 25 emails to the other company, then this company's own older one.
        for number in range(25):
            SentSweepTests.sent_message(self, f"a-{number:02d}", f"th-a-{number:02d}", to="ann@bovi.example", subject=subject)
        SentSweepTests.sent_message(self, "g-1", "th-g", to="greg@bovi.example", subject=subject)
        self.run_pass()
        first = next(query for query in self.history_searches() if "greg@bovi.example" in query)
        self.assertNotIn("subject:", first)
        self.assertNotIn("subject:", self.history_searches()[1])
        own = {thread for thread, row in self.sent_rows().items() if row["target_id"] == "t-1"}
        self.assertEqual(own, {"th-g"}, "found by its address")
        self.assertEqual(self.searched()["t-1"], 1)

    def test_a_subject_only_one_company_has_is_still_a_term(self):
        self.target("t-1", contact_email="greg@bovi.example", email_subject="Robotics internship plan")
        self.target("t-2", contact_email="ann@bovi.example", email_subject="Another distinct subject")
        self.run_pass()
        self.assertIn('subject:"Robotics internship plan"', self.history_searches()[0])
        self.assertIn('subject:"Another distinct subject"', self.history_searches()[1])

    def test_a_company_at_the_students_own_domain_never_takes_a_classmate_copied_there_as_its_address(self):
        own_domain = ACCOUNT.split("@")[1]
        self.target("t-1", contact_email=f"prof@{own_domain}", contact_cc=f"classmate@{own_domain}")
        self.event("t-1", outreach_gmail.SENT_EVENT, {"to": f"prof@{own_domain}", "cc": f"pal@{own_domain}", "thread_id": ""})
        self.run_pass()
        query = self.history_searches()[0]
        self.assertIn(f"to:prof@{own_domain}", query, "the address written to is the company's")
        self.assertNotIn("classmate@", query)
        self.assertNotIn("pal@", query)
        marks = outreach_labels._outreach_marks(self.conn, USER, {ACCOUNT})
        self.assertEqual(marks["t-1"]["addresses"], [f"prof@{own_domain}"])

    def test_ten_companies_are_searched_a_pass_oldest_first(self):
        for number in range(12):
            self.target(f"t-{number:02d}", contact_email=f"c{number:02d}@bovi.example", created_at=f"2026-09-01T10:{number:02d}:00+00:00")
        first = self.run_pass()
        self.assertEqual(first["detail"]["searched"], outreach_labels.SEARCHES_PER_PASS)
        self.assertEqual(sorted(self.searched()), [f"t-{number:02d}" for number in range(10)])
        second = self.run_pass(START + timedelta(minutes=3))
        self.assertEqual(second["detail"]["searched"], 2)
        self.assertEqual(len(self.searched()), 12)
        self.assertEqual(len(self.history_searches()), 12)

    def test_a_company_not_yet_sent_to_is_not_searched_unless_the_app_sent_to_it(self):
        self.target("t-new", status="not_started", contact_email="a@bovi.example")
        self.target("t-draft", status="drafted", contact_email="b@bovi.example")
        self.target("t-paused", status="paused", contact_email="c@bovi.example")
        self.target("t-sent", status="paused", contact_email="d@bovi.example")
        self.event("t-sent", outreach_gmail.SENT_EVENT, {"thread_id": "th-1"})
        self.thread("th-1", ("s-1", ["SENT"]))
        for status in ("sent", "followed_up", "replied", "call_scheduled", "offer", "declined", "no_response"):
            self.target(f"gone-{status}", status=status, contact_email=f"{status}@bovi.example")
        self.run_pass()
        self.assertEqual(sorted(self.searched()), sorted(["t-sent", *(f"gone-{status}" for status in (
            "sent", "followed_up", "replied", "call_scheduled", "offer", "declined", "no_response"))]))

    def test_an_answer_that_is_not_ok_leaves_the_company_unsearched_for_the_next_pass(self):
        self.target("t-1", contact_email="greg@bovi.example")
        self.gmail.thread_status = 400
        self.assertEqual(self.run_pass()["state"], "unreachable")
        self.assertEqual(self.searched(), {})
        self.gmail.thread_status = None
        self.assertEqual(self.run_pass(START + timedelta(minutes=3))["state"], "ok")
        self.assertEqual(self.searched(), {"t-1": 0})
        asked = [request for request in self.gmail.requests if "{" in request.url.params.get("q", "")]
        self.assertEqual(len(asked), 2, "the failed search was asked again")

    def test_a_rate_limited_search_is_throttled_and_the_company_stays_unsearched(self):
        self.target("t-1", contact_email="greg@bovi.example")
        self.gmail.read_response = rate_limited
        self.assertEqual(self.run_pass(), {"state": "throttled"})
        self.assertEqual(self.searched(), {})

    def test_the_search_asks_only_for_the_list_of_messages(self):
        self.target("t-1", contact_email="greg@bovi.example")
        self.run_pass()
        self.assertEqual([path for path in self.paths() if path != "GET /profile"], ["GET /messages"])


class SentSweepTests(LabelCase):
    """Sent mail since the last pass that went to an outreach address, or carries an outreach subject."""

    def started(self):
        self.target("t-1", contact_email="greg@bovi.example", email_subject="Robotics internship plan")
        self.run_pass()
        self.assertEqual(self.searched(), {"t-1": 0})
        self.gmail.requests.clear()
        self.gmail.searches.clear()

    def sent_message(self, message_id, thread_id, *, to="greg@bovi.example", subject="Hello", cc="", bcc="", labels=("SENT",), sent=None):
        headers = [{"name": "To", "value": to}, {"name": "Subject", "value": subject}]
        if cc:
            headers.append({"name": "Cc", "value": cc})
        if bcc:
            headers.append({"name": "Bcc", "value": bcc})
        self.gmail.sent_search.append(message_id)
        self.gmail.threads[message_id] = thread_id
        self.gmail.metadata[message_id] = {"id": message_id, "threadId": thread_id, "payload": {"headers": headers}}
        if sent is not None:
            self.gmail.metadata[message_id]["internalDate"] = str(int(sent.timestamp()) * 1000)
        self.thread(thread_id, (message_id, list(labels)))

    def sweep_query(self, after=START):
        return f"in:sent after:{int(after.timestamp()) - outreach_labels.SWEEP_OVERLAP_SECONDS} -label:{LABEL}"

    def test_a_sent_email_to_an_outreach_address_is_found_and_labelled(self):
        self.started()
        self.sent_message("n-1", "th-new", to="Greg Bovi <GREG@bovi.example>")
        later = START + timedelta(minutes=10)
        result = self.run_pass(later)
        self.assertEqual(result["state"], "ok")
        self.assertEqual(self.gmail.searches, [self.sweep_query()], "the sent listing asks only for what lacks the label")
        label_id = self.gmail.label_named(LABEL)["id"]
        self.assertEqual(self.modifies(), [{"ids": ["n-1"], "addLabelIds": [label_id]}])
        row = self.sent_rows()["th-new"]
        self.assertEqual((row["source"], row["target_id"], row["label_name"], row["label_note"]), ("sweep", "t-1", LABEL, ""))
        self.assertEqual(row["labeled_at"], later.isoformat(timespec="seconds"))
        self.assertEqual(result["detail"]["sent_labelled"], 1)
        metadata = [request for request in self.gmail.requests if request.url.path.endswith("/messages/n-1")]
        self.assertEqual([request.url.params.get_list("metadataHeaders") for request in metadata], [["To", "Cc", "Bcc", "Subject"]])
        self.assertEqual([request.url.params["format"] for request in metadata], ["metadata"])
        self.assertEqual(json.loads(self.setting(outreach_labels.SWEEP_SETTING)), {"label": LABEL, "after": int(later.timestamp())})

    def test_an_address_in_cc_or_bcc_counts_too(self):
        self.started()
        self.sent_message("n-1", "th-cc", to="someone@elsewhere.example", cc="Team <greg@bovi.example>")
        self.sent_message("n-2", "th-bcc", to="someone@elsewhere.example", subject="Lunch", bcc="greg@bovi.example")
        self.sent_message("n-3", "th-none", to="someone@elsewhere.example", subject="Lunch", bcc="pal@elsewhere.example")
        self.run_pass(START + timedelta(minutes=10))
        self.assertEqual(sorted(body["ids"][0] for body in self.modifies()), ["n-1", "n-2"])
        self.assertEqual(sorted(self.sent_rows()), ["th-bcc", "th-cc"])

    def test_an_address_only_copied_at_another_domain_does_not_make_mail_outreach(self):
        self.target("t-1", contact_email="greg@bovi.example", contact_cc="mentor@other.example, ann@bovi.example")
        self.run_pass()
        self.sent_message("n-1", "th-mentor", to="mentor@other.example", subject="Lunch")
        self.sent_message("n-2", "th-ann", to="ann@bovi.example", subject="Lunch")
        self.run_pass(START + timedelta(minutes=10))
        self.assertEqual(sorted(self.sent_rows()), ["th-ann"])

    def test_a_sent_email_with_an_outreach_subject_is_found_even_with_re_prefixes_and_other_spacing(self):
        self.started()
        self.sent_message("n-1", "th-subject", to="someone@elsewhere.example", subject="RE:  re: Fwd: Robotics   Internship plan")
        self.run_pass(START + timedelta(minutes=10))
        self.assertEqual(self.sent_rows()["th-subject"]["target_id"], "t-1")
        self.assertEqual(self.modifies()[-1]["ids"], ["n-1"])

    def test_a_subject_two_companies_share_does_not_make_mail_outreach_but_a_unique_one_does(self):
        self.target("t-1", contact_email="greg@bovi.example", email_subject="Robotics internship plan")
        self.target("t-2", contact_email="ann@other.example", email_subject="re: robotics  internship PLAN")
        self.target("t-3", contact_email="joe@third.example", email_subject="A subject only one company uses")
        self.run_pass()
        self.sent_message("n-1", "th-shared", to="friend@elsewhere.example", subject="Robotics internship plan")
        self.sent_message("n-2", "th-unique", to="friend@elsewhere.example", subject="A subject only one company uses")
        self.sent_message("n-3", "th-address", to="ann@other.example", subject="Lunch")
        self.run_pass(START + timedelta(minutes=10))
        self.assertEqual({thread: row["target_id"] for thread, row in self.sent_rows().items()},
                         {"th-unique": "t-3", "th-address": "t-2"})

    def test_a_classmate_copied_at_the_students_own_domain_does_not_make_mail_outreach(self):
        own_domain = ACCOUNT.split("@")[1]
        self.target("t-1", contact_email=f"prof@{own_domain}", contact_cc=f"classmate@{own_domain}")
        self.run_pass()
        self.sent_message("n-1", "th-classmate", to=f"classmate@{own_domain}", subject="Lunch")
        self.sent_message("n-2", "th-prof", to=f"prof@{own_domain}", subject="Lunch")
        self.run_pass(START + timedelta(minutes=10))
        self.assertEqual(sorted(self.sent_rows()), ["th-prof"])

    def test_a_sent_email_that_is_not_outreach_is_left_alone(self):
        self.started()
        self.sent_message("n-1", "th-other", to="friend@elsewhere.example", subject="Lunch tomorrow, maybe robotics")
        self.sent_message("n-2", "th-short", to="friend@elsewhere.example", subject="Hello there")
        result = self.run_pass(START + timedelta(minutes=10))
        self.assertEqual(self.modifies(), [])
        self.assertEqual(self.sent_rows(), {})
        self.assertEqual(self.gmail.label_creates, [])
        self.assertEqual(result["detail"]["sent_labelled"], 0)
        self.assertEqual(json.loads(self.setting(outreach_labels.SWEEP_SETTING))["after"], int((START + timedelta(minutes=10)).timestamp()))

    def test_a_message_that_already_has_the_label_is_not_listed_or_read(self):
        self.started()
        label = self.gmail.add_label(LABEL)
        self.sent_message("n-1", "th-done", labels=("SENT", label["id"]))
        self.run_pass(START + timedelta(minutes=10))
        self.assertEqual(self.gmail.searches, [self.sweep_query()])
        self.assertFalse([request for request in self.gmail.requests if "/messages/n-1" in request.url.path])
        self.assertEqual(self.modifies(), [])

    def test_a_thread_already_known_is_not_read_again(self):
        self.started()
        self.event("t-1", outreach_gmail.SENT_EVENT, {"thread_id": "th-known"})
        self.thread("th-known", ("s-old", ["SENT"]))
        self.run_pass(START + timedelta(minutes=5))
        self.gmail.requests.clear()
        self.sent_message("n-1", "th-known")
        self.run_pass(START + timedelta(minutes=10))
        self.assertFalse([request for request in self.gmail.requests if "/messages/n-1" in request.url.path])

    def test_only_a_company_with_something_to_go_by_starts_the_sent_listing(self):
        self.target("t-1", email_subject="Hi")
        self.run_pass()
        self.gmail.requests.clear()
        self.run_pass(START + timedelta(minutes=10))
        self.assertEqual(self.gmail.requests, [], "no address and no subject long enough: nothing to look for")

    def test_the_students_own_address_is_never_what_marks_outreach(self):
        self.target("t-1", contact_email=ACCOUNT, email_subject="Hi")
        self.run_pass()
        self.gmail.requests.clear()
        self.sent_message("n-1", "th-self", to=ACCOUNT)
        self.run_pass(START + timedelta(minutes=10))
        self.assertEqual(self.gmail.requests, [])

    def test_a_sent_listing_too_long_for_one_pass_searches_every_company_again_and_still_moves_on(self):
        self.started()
        self.gmail.page_size = 1
        for number in range(outreach_labels.SWEEP_PAGES + 2):
            self.sent_message(f"u-{number}", f"th-u-{number}", to="friend@elsewhere.example", subject=f"Unrelated {number}")
        later = START + timedelta(minutes=10)
        result = self.run_pass(later)
        self.assertTrue(result["detail"].get("sent_sweep_truncated"))
        self.assertFalse(result["detail"].get("sweep_truncated"), "the labelled threads' listing was not the long one")
        self.assertEqual(len(self.gmail.searches), outreach_labels.SWEEP_PAGES)
        self.assertEqual(self.searched(), {}, "every company is to be searched again")
        self.assertEqual(json.loads(self.setting(outreach_labels.SWEEP_SETTING)), {"label": LABEL, "after": int(later.timestamp())})
        self.gmail.page_size = None
        self.gmail.sent_search.clear()
        again = self.run_pass(START + timedelta(minutes=13))
        self.assertEqual((again["detail"]["searched"], self.searched()), (1, {"t-1": 0}))

    def test_a_sent_listing_that_fits_leaves_the_searches_alone(self):
        self.started()
        self.run_pass(START + timedelta(minutes=10))
        self.assertEqual(self.searched(), {"t-1": 0})

    def test_a_pass_cut_short_by_its_thread_budget_keeps_the_start_and_carries_on(self):
        self.started()
        self.sent_message("n-1", "th-1")
        self.sent_message("n-2", "th-2")
        with mock.patch.object(outreach_labels, "PER_PASS", 1):
            first = self.run_pass(START + timedelta(minutes=10))
            self.assertTrue(first["detail"]["more"])
            self.assertEqual(json.loads(self.setting(outreach_labels.SWEEP_SETTING)), {"label": LABEL, "after": int(START.timestamp())})
            self.assertEqual(self.sent_rows()["th-2"]["label_name"], "", "noted, waiting for the next pass")
            second = self.run_pass(START + timedelta(minutes=13))
        self.assertFalse(second["detail"]["more"])
        self.assertEqual(sorted(body["ids"][0] for body in self.modifies()), ["n-1", "n-2"])
        self.assertEqual(json.loads(self.setting(outreach_labels.SWEEP_SETTING))["after"], int((START + timedelta(minutes=13)).timestamp()))

    def test_a_throttled_listing_does_not_move_the_start_forward(self):
        self.started()
        before = self.setting(outreach_labels.SWEEP_SETTING)
        self.gmail.read_response = rate_limited
        self.assertEqual(self.run_pass(START + timedelta(minutes=10)), {"state": "throttled"})
        self.assertEqual(self.setting(outreach_labels.SWEEP_SETTING), before)

    def test_a_message_gmail_no_longer_has_is_passed_over(self):
        self.started()
        self.gmail.sent_search.append("gone-1")
        self.gmail.threads["gone-1"] = "th-gone"
        result = self.run_pass(START + timedelta(minutes=10))
        self.assertEqual(result["state"], "ok")
        self.assertEqual(self.sent_rows(), {})

    def test_the_labelled_threads_listing_and_the_sent_listing_both_run(self):
        self.started()
        self.thread("t-1x", "m-1")
        self.reply("m-1", "t-1x")
        self.gmail.inbox_replies.append("m-late")
        self.gmail.threads["m-late"] = "t-1x"
        self.run_pass(START + timedelta(minutes=3))
        self.gmail.searches.clear()
        self.run_pass(START + timedelta(minutes=10))
        self.assertEqual(len(self.gmail.searches), 2)
        self.assertTrue(self.gmail.searches[0].startswith("after:"))
        self.assertTrue(self.gmail.searches[1].startswith("in:sent after:"))


class MarksSharedTests(LabelCase):
    """One pass builds the companies' marks once per set of own addresses, and everything it does is as if each step built them itself."""

    PLAY = "test_a_pass_shares_one_build_between_steps_with_no_wait_on_gmail_between_them"

    def counting(self):
        real = outreach_labels._outreach_marks
        calls = []

        def counted(conn, user_id, own):
            calls.append(set(own))
            return real(conn, user_id, own)

        return mock.patch.object(outreach_labels, "_outreach_marks", side_effect=counted), calls

    def play(self, *, account_in_env=True):
        """Four passes that reach every step that reads the marks: the history search, the sent rows and the sent sweep."""
        later = lambda minutes: START + timedelta(minutes=minutes)
        env = mock.patch.dict("os.environ")
        env.start()
        self.addCleanup(env.stop)
        if not account_in_env:
            os.environ.pop("PIPELINE_OUTREACH_ACCOUNT", None)
        self.target("t-1", contact_email="greg@bovi.example", email_subject="Robotics internship plan")
        self.target("t-2", contact_email="ann@orbit.example", contact_cc="mentor@elsewhere.example, ann@orbit.example",
                    email_subject="Orbit systems internship")
        self.target("t-3", status="drafted", contact_email="drafty@zed.example")
        self.event("t-1", outreach_gmail.SENT_EVENT, {"thread_id": "th-1", "to": "greg@bovi.example"})
        self.event("t-2", outreach_gmail.DRAFT_EVENT, {"to": "ann@orbit.example", "cc": "cc@orbit.example"})
        self.thread("th-1", ("s-1", ["SENT"]))
        self.seeded(2)
        HistorySearchTests.addressed(self, "h-1")
        self.gmail.sent_search.append("h-1")
        self.gmail.threads["h-1"] = "th-h"
        self.thread("th-h", ("h-1", ["SENT"]))
        results = [self.run_pass(later(0))]
        SentSweepTests.sent_message(self, "n-1", "th-new")
        SentSweepTests.sent_message(self, "n-2", "th-ann", to="ann@orbit.example")
        SentSweepTests.sent_message(self, "n-3", "th-x", to="friend@elsewhere.example", subject="Lunch")
        results.append(self.run_pass(later(10)))
        with self.conn:
            self.conn.execute("UPDATE outreach_targets SET contact_email='greg2@bovi.example' WHERE id='t-1'")
        results.append(self.run_pass(later(13)))
        results.append(self.run_pass(later(16)))
        searches = self.conn.execute("SELECT * FROM outreach_label_searches ORDER BY target_id").fetchall()
        replies = self.conn.execute(
            "SELECT gmail_id, thread_id, label_name, labeled_at, label_note FROM outreach_inbox_messages ORDER BY gmail_id").fetchall()
        self.conn.rollback()
        return {
            "results": results, "requests": self.paths(), "searches": list(self.gmail.searches), "modifies": self.modifies(),
            "threads": self.sent_rows(), "searched": [dict(row) for row in searches], "replies": [dict(row) for row in replies],
            "sweep": self.setting(outreach_labels.SWEEP_SETTING),
        }

    def played(self, rebuilding=False, **kwargs):
        """The scenario in a fresh fixture; with ``rebuilding``, every step builds the marks itself, as each did before they were shared."""
        case = type(self)(self.PLAY)
        case.setUp()
        try:
            if not rebuilding:
                return case.play(**kwargs)
            with mock.patch.object(
                outreach_labels._Marks, "get", lambda marks, own: outreach_labels._outreach_marks(marks.conn, marks.user_id, own),
            ):
                return case.play(**kwargs)
        finally:
            case.doCleanups()

    def test_the_requests_results_and_rows_are_those_of_a_pass_that_builds_the_marks_in_every_step(self):
        for kwargs in ({}, {"account_in_env": False}):
            with self.subTest(**kwargs):
                kept = self.played(**kwargs)
                self.assertTrue(kept["searches"] and kept["modifies"] and kept["searched"], "the scenario must reach the search and the sweep")
                self.assertEqual(kept, self.played(rebuilding=True, **kwargs))

    def test_a_pass_shares_one_build_between_steps_with_no_wait_on_gmail_between_them(self):
        self.target("t-1", contact_email="greg@bovi.example", email_subject="Robotics internship plan")
        patch, calls = self.counting()
        with patch:
            self.run_pass()
        # The work check builds; the history search follows a wait on Gmail (the replies), so it builds its own, once
        # for its candidates and its search.
        self.assertEqual(calls, [{ACCOUNT}, {ACCOUNT}])
        # The next pass has a sweep to start: the sweep follows the history searches, so it builds once more.
        calls.clear()
        SentSweepTests.sent_message(self, "n-1", "th-new")
        with patch:
            self.run_pass(START + timedelta(minutes=10))
        self.assertEqual(calls, [{ACCOUNT}] * 3)
        calls.clear()
        with patch:
            self.run_pass(START + timedelta(minutes=13))
        self.assertEqual(calls, [{ACCOUNT}] * 3)

    def test_a_company_deleted_while_the_pass_waits_on_gmail_is_not_labelled_by_the_sweep(self):
        """The marks the sweep matches sent mail against are read after the waits that precede it, as before they were shared."""
        self.target("t-1", contact_email="greg@bovi.example", email_subject="Robotics internship plan")
        self.target("t-2", contact_email="ann@orbit.example", email_subject="Orbit systems internship")
        self.run_pass()  # starts the sweep; both companies searched
        # A new address for the first company gives the history search something to ask Gmail (a wait before the
        # sweep), and mail to the second company waits in Sent for the sweep to find.
        with self.conn:
            self.conn.execute("UPDATE outreach_targets SET contact_email='greg2@bovi.example' WHERE id='t-1'")
        SentSweepTests.sent_message(self, "n-2", "th-ann", to="ann@orbit.example", subject="Lunch")
        deleted = []
        real_handler = self.gmail.handler

        def deleting_while_waiting(request):
            if not deleted and "greg2" in request.url.params.get("q", ""):
                # The student deletes company t-2 in the app while this pass is waiting on Gmail.
                with closing(connect_product(self.platform_path)) as other, other:
                    other.execute("DELETE FROM outreach_targets WHERE id='t-2'")
                deleted.append(request.url.params["q"])
            return real_handler(request)

        self.gmail.handler = deleting_while_waiting
        self.run_pass(START + timedelta(minutes=10))
        self.assertTrue(deleted, "the scenario must delete a company while the pass waits on Gmail")
        self.assertNotIn("th-ann", self.sent_rows())
        self.assertNotIn("n-2", [message for body in self.modifies() for message in body["ids"]])

    def test_without_the_account_in_the_environment_each_distinct_set_is_built_once_as_before(self):
        self.target("t-1", contact_email="greg@bovi.example", email_subject="Robotics internship plan")
        patch, calls = self.counting()
        with mock.patch.dict("os.environ"), patch:
            os.environ.pop("PIPELINE_OUTREACH_ACCOUNT")
            self.run_pass()
        self.assertEqual(sorted(calls, key=sorted), [set(), {ACCOUNT}], "the work check names no address, the other steps the mailbox's")

    def test_labels_off_paused_or_nothing_sent_build_no_marks(self):
        patch, calls = self.counting()
        with patch:
            self.run_pass()  # nothing sent yet
            self.target("t-1", contact_email="greg@bovi.example", email_subject="Robotics internship plan")
            outreach_labels.set_label_name(self.conn, USER, "")
            self.run_pass(START + timedelta(minutes=3))
            outreach_labels.set_label_name(self.conn, USER, None)
            self.pause()
            self.run_pass(START + timedelta(minutes=6))
        self.assertEqual(calls, [])


class MigrationTests(unittest.TestCase):
    def test_a_half_applied_0043_is_repaired_by_running_it_again(self):
        migrations = Path(__file__).resolve().parent.parent / "migrations"
        with tempfile.TemporaryDirectory() as directory:
            conn = connect_product(Path(directory) / "platform.db")
            try:
                conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)")
                for migration in sorted(migrations.glob("[0-9][0-9][0-9][0-9]_*.sql")):
                    if migration.name >= "0043":
                        break
                    sql = migration.read_text(encoding="utf-8")
                    step = schema._MIGRATION_STEPS.get(migration.name)
                    step(conn, sql) if step else conn.executescript(sql)
                    conn.execute("INSERT INTO schema_migrations(name, applied_at) VALUES(?, ?)", (migration.name, utc_now()))
                conn.commit()
                for table, column in (("outreach_inbox_messages", "label_name"), ("connector_accounts", "account_email")):
                    self.assertFalse(has_column(conn, table, column), "not there before 0043")
                # The crash: one column added, the rest (and the marker) not.
                conn.execute("ALTER TABLE outreach_inbox_messages ADD COLUMN label_name TEXT NOT NULL DEFAULT ''")
                conn.commit()
                schema.ensure_product_schema(conn)
                for table, column in (("outreach_inbox_messages", "label_name"), ("outreach_inbox_messages", "labeled_at"),
                                      ("outreach_inbox_messages", "label_note"), ("connector_accounts", "account_email")):
                    self.assertTrue(has_column(conn, table, column), f"{table}.{column}")
                self.assertIsNotNone(conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_outreach_inbox_messages_labels'").fetchone())
                self.assertIsNotNone(conn.execute("SELECT 1 FROM schema_migrations WHERE name='0043_gmail_reply_labels.sql'").fetchone())
                # Running the step itself again is harmless.
                schema._apply_gmail_reply_labels(conn, (migrations / "0043_gmail_reply_labels.sql").read_text(encoding="utf-8"))
            finally:
                conn.close()

    def test_a_reply_captured_before_0043_reads_as_unlabelled(self):
        with tempfile.TemporaryDirectory() as directory:
            _, path = build_and_migrate(Path(directory))
            with closing(connect_product(path)) as conn:
                with conn:
                    conn.execute(
                        "INSERT INTO outreach_inbox_messages(user_id, gmail_id, target_id, kind, sender, received_at, recorded_at) "
                        "VALUES(?, 'old', 't-1', 'reply', 'greg@bovi.example', '2026-09-20T10:00:00+00:00', ?)", (USER, utc_now()),
                    )
                row = conn.execute("SELECT label_name, labeled_at, label_note FROM outreach_inbox_messages WHERE gmail_id='old'").fetchone()
                self.assertEqual((row["label_name"], row["labeled_at"], row["label_note"]), ("", None, ""))

    def test_0044_adds_the_sent_thread_tables_and_running_it_again_changes_nothing(self):
        migrations = Path(__file__).resolve().parent.parent / "migrations"
        wanted = {
            "outreach_label_threads": ["user_id", "thread_id", "target_id", "source", "label_name", "labeled_at", "label_note", "found_at"],
            "outreach_label_searches": ["user_id", "target_id", "searched_at", "found", "query"],
        }
        with tempfile.TemporaryDirectory() as directory:
            _, path = build_and_migrate(Path(directory))
            with closing(connect_product(path)) as conn:
                for table, columns in wanted.items():
                    self.assertEqual([row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()], columns)
                self.assertIsNotNone(conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_outreach_label_threads_label'").fetchone())
                self.assertIsNotNone(conn.execute("SELECT 1 FROM schema_migrations WHERE name='0044_outreach_sent_labels.sql'").fetchone())
                with conn:
                    conn.execute("INSERT INTO outreach_label_threads(user_id, thread_id, source, found_at) VALUES(?, 'th-1', 'sent', ?)", (USER, utc_now()))
                    conn.execute("INSERT INTO outreach_label_searches(user_id, target_id, searched_at) VALUES(?, 'gone-target', ?)", (USER, utc_now()))
                row = conn.execute("SELECT target_id, label_name, labeled_at, label_note FROM outreach_label_threads").fetchone()
                self.assertEqual((row["target_id"], row["label_name"], row["labeled_at"], row["label_note"]), ("", "", None, ""))
                self.assertEqual(conn.execute("SELECT found FROM outreach_label_searches").fetchone()["found"], 0)
                # No foreign key: a company deleted later leaves its threads (and their label) alone.
                self.assertEqual(conn.execute("PRAGMA foreign_key_list(outreach_label_threads)").fetchall(), [])
                self.assertEqual(conn.execute("PRAGMA foreign_key_list(outreach_label_searches)").fetchall(), [])
                # The file is plain SQL, so running it again (a crash before its marker) is harmless.
                conn.executescript((migrations / "0044_outreach_sent_labels.sql").read_text(encoding="utf-8"))
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM outreach_label_threads").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM outreach_label_searches").fetchone()[0], 1)
                schema.ensure_product_schema(conn)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM schema_migrations WHERE name='0044_outreach_sent_labels.sql'").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
