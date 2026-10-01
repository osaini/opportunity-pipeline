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


if __name__ == "__main__":
    unittest.main()
