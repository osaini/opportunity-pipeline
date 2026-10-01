"""The shared leaf modules stay leaves, and what they hold behaves as the copies it replaced did.

A leaf imports only what its allowlist names (the standard library, and other
leaves where stated): no database, no Gmail, no web framework. The check parses
each leaf with ast, function-level imports included, so a lazy import cannot
sneak a heavy module in.

Each workstream of the duplicate-removal refactor adds its own section below.
"""

from __future__ import annotations

import ast
import sys
import unittest
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from pathlib import Path

try:
    import realdata_guard
except ImportError:  # imported as tests.<module>, with tests/ not on sys.path
    from tests import realdata_guard
# Guards this module's own run against opening data/*.db (AGENTS.md hard rule 1); see tests/realdata_guard.py.
realdata_guard.install()

APP = Path(__file__).resolve().parents[1] / "opportunity_app"


def imported_modules(path: Path) -> set[str]:
    """Every module a file imports, at any depth. A relative import is named "." + its module ('.mail_message')."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                names = [alias.name for alias in node.names] if not node.module else [node.module]
                found |= {"." + name.split(".")[0] for name in names}
            else:
                found.add((node.module or "").split(".")[0])
    return found


def outside_allowlist(path: Path, allowed_local: set[str] = frozenset()) -> set[str]:
    """What a leaf imports beyond the standard library and the leaves it may use ('.name' for a sibling module)."""
    return {
        name for name in imported_modules(path)
        if not (name in sys.stdlib_module_names or name == "__future__" or name in allowed_local)
    }


# ===== Workstream M: the mail leaves ====================================================


def parsed(raw: str):
    return BytesParser(policy=policy.default).parsebytes(raw.encode("utf-8"))


class MailMessageLeafTests(unittest.TestCase):
    def test_mail_message_imports_only_the_standard_library(self):
        self.assertEqual(outside_allowlist(APP / "mail_message.py"), set())

    def test_the_header_map_keeps_the_last_of_a_repeated_header_and_folds_names(self):
        from opportunity_app.mail_message import header_map

        message = {"payload": {"headers": [
            {"name": "Subject", "value": "first"}, {"name": "SUBJECT", "value": "second"}, {"name": "To", "value": "a@b.example"},
        ]}}
        self.assertEqual(header_map(message), {"subject": "second", "to": "a@b.example"})
        self.assertEqual(header_map({}), {})
        self.assertEqual(header_map({"payload": {"headers": None}}), {})

    def test_the_guarded_header_map_skips_what_is_not_a_header_and_the_plain_one_does_not(self):
        from opportunity_app.mail_message import header_map

        message = {"payload": {"headers": [None, {"name": "From", "value": "a@b.example"}, "junk"]}}
        with self.assertRaises(AttributeError):
            header_map(message)
        self.assertEqual(header_map(message, guarded=True), {"from": "a@b.example"})

    def test_base64url_gets_its_padding_back_and_refuses_what_is_not_base64(self):
        from opportunity_app.mail_message import decode_base64url

        self.assertEqual(decode_base64url("aGVsbG8"), b"hello")
        self.assertEqual(decode_base64url("aGVsbG8="), b"hello")
        self.assertEqual(decode_base64url(""), b"")
        with self.assertRaises(ValueError):
            decode_base64url("not base64 ☃")

    def test_the_two_html_readers_keep_their_different_text_retention(self):
        from opportunity_app.mail_message import html_text_reply, html_text_spaced

        markup = "<p>Hi <b>there</b></p><style>x{}</style><blockquote>quoted words</blockquote>after"
        self.assertEqual(html_text_spaced(markup), " Hi  there \n quoted words after")
        self.assertEqual(html_text_reply(markup), "Hi there\n")

    def test_the_two_bulk_checks_differ_only_in_the_auto_submitted_rule(self):
        from opportunity_app.mail_message import has_list_headers, is_bulk_or_generated

        generated = parsed("From: a@b.example\nAuto-Submitted: auto-generated\n\nbody\n")
        listed = parsed("From: a@b.example\nList-Id: <news.b.example>\n\nbody\n")
        bulk = parsed("From: a@b.example\nPrecedence: Bulk\n\nbody\n")
        replied = parsed("From: a@b.example\nAuto-Submitted: auto-replied\n\nbody\n")
        self.assertTrue(is_bulk_or_generated(generated))
        self.assertFalse(has_list_headers(generated))
        for message in (listed, bulk):
            self.assertTrue(is_bulk_or_generated(message))
            self.assertTrue(has_list_headers(message))
        self.assertFalse(is_bulk_or_generated(replied))
        self.assertFalse(has_list_headers(replied))

    def test_the_two_received_times_fall_back_differently(self):
        from opportunity_app.mail_message import received_or_epoch, received_or_none

        dated = parsed("From: a@b.example\nDate: Tue, 01 Sep 2026 10:00:00 +0000\n\nbody\n")
        undated = parsed("From: a@b.example\n\nbody\n")
        self.assertEqual(received_or_epoch({}), datetime(1970, 1, 1, tzinfo=timezone.utc))
        self.assertEqual(received_or_epoch({"internalDate": "1000"}), datetime(1970, 1, 1, 0, 0, 1, tzinfo=timezone.utc))
        self.assertEqual(received_or_none({"internalDate": "1000"}, undated), datetime(1970, 1, 1, 0, 0, 1, tzinfo=timezone.utc))
        self.assertEqual(received_or_none({}, dated), datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc))
        self.assertIsNone(received_or_none({}, undated))

    def test_a_link_is_cut_to_its_host_and_a_query_is_never_kept(self):
        from opportunity_app.mail_message import clean_url, host_of, strip_queries

        self.assertEqual(clean_url("https://Acme.com/a?x=1&amp;y=2)."), "https://Acme.com/a?x=1&y=2")
        self.assertEqual(host_of("https://Careers.Acme.com./jobs?id=1"), "careers.acme.com")
        self.assertEqual(host_of("not a url"), "")
        self.assertEqual(host_of(None), "")
        self.assertEqual(strip_queries("failed for https://acme.com/a/b?token=secret#frag and more"), "failed for https://acme.com/a/b and more")

    def test_link_hosts_fail_closed_where_hosts_in_does_not(self):
        from opportunity_app.mail_message import LINK_HOST_LIMIT, hosts_in, link_hosts_or_none

        many = " ".join(f"https://site{number}.example/" for number in range(LINK_HOST_LIMIT + 1))
        crowded = parsed(f"From: a@b.example\nContent-Type: text/plain\n\n{many}\n")
        self.assertIsNone(link_hosts_or_none(crowded))
        self.assertEqual(len(hosts_in(many)), LINK_HOST_LIMIT + 1)
        self.assertEqual(link_hosts_or_none(parsed("From: a@b.example\n\nsee https://www.acme.com/x?y=1.\n")), ["www.acme.com"])

    def test_a_mailbox_folds_its_tag_and_a_gmail_address_its_dots(self):
        from opportunity_app.mail_message import mailbox_key

        self.assertEqual(mailbox_key(" Dana.Reyes+jobs@Gmail.com "), "danareyes@gmail.com")
        self.assertEqual(mailbox_key("d.reyes@googlemail.com"), "dreyes@gmail.com")
        self.assertEqual(mailbox_key("dana.reyes+x@school.example"), "dana.reyes@school.example")
        self.assertEqual(mailbox_key("+tag@school.example"), "+tag@school.example")
        self.assertEqual(mailbox_key("no address"), "no address")


class GmailClientLeafTests(unittest.TestCase):
    def test_gmail_client_imports_only_the_standard_library_and_httpx(self):
        self.assertEqual(outside_allowlist(APP / "gmail_client.py", {"httpx"}), set())

    def test_a_connection_is_connected_not_connected_or_needing_a_reconnect(self):
        from opportunity_app.gmail_client import connection_state

        self.assertEqual(connection_state(None), "not_connected")
        self.assertEqual(connection_state({"status": "disconnected"}), "not_connected")
        self.assertEqual(connection_state({"status": "connected"}), "connected")
        self.assertEqual(connection_state({"status": "error"}), "needs_reconnect")
        self.assertEqual(connection_state({"status": "anything else"}), "needs_reconnect")

    def test_granted_scopes_read_a_list_and_nothing_else(self):
        from opportunity_app.gmail_client import MODIFY_SCOPE, READ_SCOPE, can_read_mail, granted_scopes

        self.assertEqual(granted_scopes(f'["{READ_SCOPE}", 7]'), [READ_SCOPE, "7"])
        for unreadable in (None, "", "not json", "null", '"a string"', '{"a": 1}', 5):
            with self.subTest(value=unreadable):
                self.assertEqual(granted_scopes(unreadable), [])
        self.assertTrue(can_read_mail([READ_SCOPE]))
        self.assertTrue(can_read_mail([MODIFY_SCOPE]))
        self.assertFalse(can_read_mail(["https://www.googleapis.com/auth/gmail.compose"]))

    def test_a_throttle_is_a_429_or_a_403_that_names_a_rate_limit(self):
        import httpx

        from opportunity_app.gmail_client import error_reasons, is_throttle

        def answer(status, body=None):
            return httpx.Response(status, json=body) if body is not None else httpx.Response(status, text="not json")

        named = {"error": {"status": "RESOURCE_EXHAUSTED", "errors": [{"reason": "rateLimitExceeded"}, "junk", {"reason": 3}]}}
        self.assertTrue(is_throttle(answer(429)))
        self.assertTrue(is_throttle(answer(403, named)))
        self.assertTrue(is_throttle(answer(403, {"error": {"errors": [{"reason": "userRateLimitExceeded"}]}})))
        self.assertFalse(is_throttle(answer(403, {"error": {"status": "PERMISSION_DENIED", "errors": [{"reason": "forbidden"}]}})))
        self.assertFalse(is_throttle(answer(403)))
        self.assertFalse(is_throttle(answer(403, {"error": "denied"})))
        self.assertFalse(is_throttle(answer(500, named)))
        self.assertEqual(error_reasons(answer(403, named)), ["RESOURCE_EXHAUSTED", "rateLimitExceeded", 3])
        self.assertEqual(error_reasons(answer(403, [1])), [])
        self.assertEqual(error_reasons(answer(403)), [])

    def test_a_throttle_is_read_as_could_not_reach_gmail_and_never_as_a_refused_connection(self):
        import httpx

        from opportunity_app.gmail_client import GmailAuthError, GmailNeedsReadScope, GmailThrottled

        self.assertIsInstance(GmailThrottled("slow down"), httpx.HTTPError)
        self.assertNotIsInstance(GmailAuthError("refused"), httpx.HTTPError)
        self.assertNotIsInstance(GmailNeedsReadScope(), (httpx.HTTPError, GmailAuthError))

    def test_a_look_schedule_spaces_looks_and_forgets_a_failed_one(self):
        import threading
        from datetime import timedelta

        from opportunity_app.gmail_client import LookSchedule

        last: dict = {}
        schedule = LookSchedule(
            last, threading.Lock(), interval=lambda age: timedelta(minutes=10) if age < timedelta(hours=1) else timedelta(hours=1),
            key=lambda item: item["id"], started=lambda item: item["at"],
        )
        now = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
        fresh, old = {"id": "a", "at": now - timedelta(minutes=5)}, {"id": "b", "at": now - timedelta(days=2)}
        self.assertEqual(schedule.take_due("u", [fresh, old], now), [fresh, old])
        self.assertEqual(schedule.take_due("u", [fresh, old], now + timedelta(minutes=9)), [])
        self.assertEqual(schedule.take_due("u", [fresh, old], now + timedelta(minutes=11)), [fresh])
        self.assertEqual(schedule.take_due("u", [fresh, old], now + timedelta(hours=2)), [fresh, old])
        self.assertEqual(schedule.take_due("someone else", [fresh], now), [fresh], "kept per student")
        schedule.forget("u", [fresh])
        self.assertEqual(schedule.take_due("u", [fresh, old], now + timedelta(hours=2, minutes=1)), [fresh])
        self.assertIs(schedule.last, last)

    def test_the_send_watchers_look_key_is_forgotten_as_text_and_remembered_as_stored(self):
        # Kept as it was (gmail-11): remembered under the id as stored, forgotten under str() of it. Harmless while
        # a draft id is always text; this pins that nobody evens the two out without meaning to.
        import threading
        from datetime import timedelta

        from opportunity_app.gmail_client import LookSchedule

        schedule = LookSchedule(
            {}, threading.Lock(), interval=lambda age: timedelta(hours=1), key=lambda item: item["id"], started=lambda item: item["at"],
            forget_key=str,
        )
        now = datetime(2026, 9, 1, tzinfo=timezone.utc)
        item = {"id": 5, "at": now}
        schedule.take_due("u", [item], now)
        schedule.forget("u", [item])
        self.assertEqual(schedule.take_due("u", [item], now), [], "the int key was not forgotten: forget looked for '5'")
        text = {"id": "d-1", "at": now}
        schedule.take_due("u", [text], now)
        schedule.forget("u", [text])
        self.assertEqual(schedule.take_due("u", [text], now), [text])

    def test_each_watcher_keeps_its_own_look_state_under_its_own_name(self):
        from opportunity_app import outreach_delivery, outreach_gmail_sends

        self.assertIs(outreach_delivery._LOOKS.last, outreach_delivery._LAST_LOOK)
        self.assertIs(outreach_gmail_sends._LOOKS.last, outreach_gmail_sends._LAST_LOOK)
        self.assertIsNot(outreach_delivery._LAST_LOOK, outreach_gmail_sends._LAST_LOOK)
        self.assertIs(outreach_delivery._LOOKS.lock, outreach_delivery._LOOK_LOCK, "which also guards _READ_NOTICES")


class OutreachIdentityTests(unittest.TestCase):
    def test_company_identity_does_not_load_the_mail_readers(self):
        heavy = {".outreach_inbox", ".application_inbox", ".outreach_labels", ".outreach_delivery", ".automation", ".api"}
        self.assertEqual(imported_modules(APP / "outreach_identity.py") & heavy, set())

    def test_the_interviewer_and_research_take_identity_from_it_not_from_the_mail_reader(self):
        for module in ("outreach_interviewer.py", "outreach_research.py"):
            with self.subTest(module=module):
                self.assertNotIn(".outreach_inbox", imported_modules(APP / module))
                self.assertIn(".outreach_identity", imported_modules(APP / module))


if __name__ == "__main__":
    unittest.main()
