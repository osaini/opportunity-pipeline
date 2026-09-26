"""Each student's own way of opening an email, used for drafts and when a contact changes."""

import json
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from opportunity_app import outreach
from opportunity_app.outreach import (
    DEFAULT_GREETING,
    create_target,
    greeting_line,
    greeting_style,
    update_target,
)
from opportunity_app.outreach_contacts import add_manual_contact
from opportunity_app.outreach_drafting import _inputs, generate_draft
from opportunity_app.profile import validate_profile_types
from opportunity_app.schema import LOCAL_USER_ID, connect_product

from helpers_platform import build_and_migrate


class GreetingStyleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        _, path = build_and_migrate(root)
        self.conn = connect_product(path)
        self.profile = root / "profile.json"
        patcher = mock.patch.object(outreach, "PROFILE_PATH", self.profile)
        patcher.start()
        self.addCleanup(patcher.stop)
        outreach._PROFILE_DATA_CACHE.update(key=None, data={})

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def owner_says(self, **style):
        self.profile.write_text(json.dumps({"name": "Test Student", **style}), encoding="utf-8")
        outreach._PROFILE_DATA_CACHE.update(key=None, data={})

    def test_without_a_preference_the_defaults_are_used(self):
        self.assertEqual(greeting_style(self.conn, LOCAL_USER_ID), DEFAULT_GREETING)
        self.assertEqual(greeting_line("Acme Robotics, Inc.", "", DEFAULT_GREETING), "Hi Acme Robotics team,")
        self.assertEqual(greeting_line("Acme", "Dr. Dana Ruiz", DEFAULT_GREETING), "Hi Dana,")

    def test_each_student_has_their_own(self):
        self.owner_says(greeting_word="Hello", unnamed_greeting="there")
        self.assertEqual(greeting_style(self.conn, LOCAL_USER_ID), {"word": "Hello", "unnamed": "there"})
        # Another student on the same install never gets the owner's words.
        with self.conn:
            self.conn.execute("INSERT INTO users(id, email, display_name, role, created_at, updated_at) VALUES('friend', 'f@example.edu', 'Friend', 'student', '2026-09-26', '2026-09-26')")
            for field, value in (("greeting_word", "Dear"), ("unnamed_greeting", "{company} hiring team")):
                self.conn.execute(
                    "INSERT INTO profile_facts(user_id, field_path, value_json, source, confirmed, created_at, updated_at) "
                    "VALUES('friend', ?, ?, 'user', 1, '2026-09-26', '2026-09-26')",
                    (field, json.dumps(value)),
                )
        friend = greeting_style(self.conn, "friend")
        self.assertEqual(friend, {"word": "Dear", "unnamed": "{company} hiring team"})
        self.assertEqual(greeting_line("Kiva Labs LLC", "", friend), "Dear Kiva Labs hiring team,")

    def test_an_unusable_preference_falls_back_and_a_save_refuses_it(self):
        self.owner_says(greeting_word="Hi {name}", unnamed_greeting="{whoever} team")
        self.assertEqual(greeting_style(self.conn, LOCAL_USER_ID), DEFAULT_GREETING)
        for bad in ({"greeting_word": "Hi 123"}, {"unnamed_greeting": "{first_name}"}, {"unnamed_greeting": "x" * 61}):
            with self.subTest(bad), self.assertRaises(ValueError):
                validate_profile_types(bad)
        validate_profile_types({"greeting_word": "Good morning", "unnamed_greeting": "{company} team"})

    def test_a_new_contact_is_greeted_in_the_students_own_style(self):
        self.owner_says(greeting_word="Hello", unnamed_greeting="there")
        target = create_target(self.conn, {
            "company": "Acme", "contact_name": "Greg Lee", "contact_email": "greg@acme.test", "email_body": "Hello Greg,\n\nA note.",
        }, user_id=LOCAL_USER_ID)
        inbox = update_target(self.conn, target["id"], {"contact_name": "", "contact_email": "info@acme.test"}, user_id=LOCAL_USER_ID)
        self.assertEqual(inbox["email_body"], "Hello there,\n\nA note.")
        added = add_manual_contact(self.conn, target["id"], user_id=LOCAL_USER_ID, email="dana@acme.test", name="Dana Ruiz")
        self.assertEqual(added["email_body"], "Hello Dana,\n\nA note.", "adding a contact by hand uses the same rewrite")

    def test_drafts_open_with_the_students_greeting(self):
        self.owner_says(greeting_word="Dear", unnamed_greeting="{company} hiring team")
        target = create_target(self.conn, {"company": "Bovi, Inc.", "contact_email": "jobs@bovi.test"}, user_id=LOCAL_USER_ID)
        self.assertEqual(_inputs(self.conn, target, LOCAL_USER_ID, "initial")["greeting"], "Dear Bovi hiring team,")
        drafted = generate_draft(self.conn, target["id"], user_id=LOCAL_USER_ID, provider_factory=None, provider="legacy")
        self.assertTrue(drafted["email_body"].startswith("Dear Bovi hiring team,\n"), drafted["email_body"])


if __name__ == "__main__":
    unittest.main()
