"""The Gmail label on every thread where someone at a company replied, against a mocked Gmail."""

import json
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

from opportunity_app import STATIC_DIR, automation, outreach, outreach_gmail, outreach_inbox, outreach_labels, schema
from opportunity_app.api import create_app
from opportunity_app.outreach_inbox import InboxWatcher, decide_possible_reply
from opportunity_app.schema import connect_product, utc_now

from helpers_platform import build_and_migrate
from test_outreach_gmail import ACCOUNT, LABEL_SCOPES, MODIFY, SCOPES, FakeGmail, forget_gmail_backoff, rate_limited

AUTH = {"Authorization": "Bearer label-owner"}
USER = "local-user"
LABEL = "opportunities"
START = datetime(2026, 9, 29, 15, 0, tzinfo=timezone.utc)
INSUFFICIENT = {"error": {"code": 403, "message": "Request had insufficient authentication scopes.",
                          "errors": [{"reason": "insufficientPermissions", "domain": "global"}]}}


def insufficient():
    return httpx.Response(403, json=INSUFFICIENT)


def invalid_label():
    return httpx.Response(400, json={"error": {"code": 400, "message": "Invalid label: Label_gone"}})


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

    def row(self, gmail_id):
        row = self.conn.execute("SELECT * FROM outreach_inbox_messages WHERE user_id=? AND gmail_id=?", (USER, gmail_id)).fetchone()
        self.conn.rollback()
        return dict(row)

    def paths(self):
        return [f"{request.method} {request.url.path.split('/users/me')[-1]}" for request in self.gmail.requests]

    def modifies(self):
        return [call for call in self.gmail.batch_modifies]

    def setting(self, key):
        value = automation._setting(self.conn, USER, key)
        self.conn.rollback()
        return value

    def pause(self):
        with self.conn:
            automation._put_setting(self.conn, USER, automation.PAUSED_KEY, "on", utc_now())


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

        class OpensOnAnyStatement:
            def __init__(self, conn):
                self.conn, self.open = conn, False

            @property
            def in_transaction(self):
                return self.open or self.conn.in_transaction

            def execute(self, *args):
                self.open = True
                return self.conn.execute(*args)

            def rollback(self):
                self.open = False
                self.conn.rollback()

            def __enter__(self):
                return self

            def __exit__(self, kind, *_rest):
                self.open = False
                self.conn.commit() if kind is None else self.conn.rollback()

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
        self.assertEqual(self.gmail.thread_gets, [("t-1", "minimal", [])], "the thread is read once")

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
        """What the sweep asks Gmail for: mail since the last pass, without drafts and without what already has the label."""
        return f"after:{int(after.timestamp()) - outreach_labels.SWEEP_OVERLAP_SECONDS} -in:chats -in:drafts -label:{LABEL}"

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

    def test_a_thread_the_app_never_labelled_is_left_alone(self):
        self.labelled_thread()
        self.thread("t-other", "m-other")
        self.gmail.inbox_replies.append("m-other")
        self.gmail.threads["m-other"] = "t-other"
        self.run_pass(START + timedelta(minutes=10))
        self.assertEqual(len(self.modifies()), 1)
        self.assertEqual(self.gmail.searches, [self.sweep_query(START)], "the sweep listed what came in")
        self.assertEqual(self.gmail.thread_gets, [("t-1", "minimal", [])], "only the first pass read a thread; t-other never")

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
                                      "so the app can label reply threads",
            "wrong_account": "Gmail is connected as a different account from your outreach address; reconnect with that address",
            "label_refused": "Gmail would not create a label with that name; choose another in Outreach settings",
        }
        for state, text in expected.items():
            self.assertEqual(outreach_inbox.STEP_ERRORS[state], text)
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
                    self.assertFalse(schema._has_column(conn, table, column), "not there before 0043")
                # The crash: one column added, the rest (and the marker) not.
                conn.execute("ALTER TABLE outreach_inbox_messages ADD COLUMN label_name TEXT NOT NULL DEFAULT ''")
                conn.commit()
                schema.ensure_product_schema(conn)
                for table, column in (("outreach_inbox_messages", "label_name"), ("outreach_inbox_messages", "labeled_at"),
                                      ("outreach_inbox_messages", "label_note"), ("connector_accounts", "account_email")):
                    self.assertTrue(schema._has_column(conn, table, column), f"{table}.{column}")
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


if __name__ == "__main__":
    unittest.main()
