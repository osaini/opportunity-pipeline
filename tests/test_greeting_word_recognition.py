"""A student's own greeting_word ("Howdy") is recognised wherever the app reads a draft's greeting.

The greeting patterns used to know only hi, hello, hey, dear and "good <time>", so a student whose draft opens
"Howdy Dana," had a contact change that never readdressed the unsent drafts, a bounce resend that was always
refused, and an unnamed-contact check that let a draft greet an invented name.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from opportunity_app.outreach import location as outreach_location
from opportunity_app.outreach.automation import resend_refusal
from opportunity_app.outreach.contacts import add_manual_contact
from opportunity_app.outreach.drafting import validate_draft
from opportunity_app.outreach.greeting import greets_contact, readdress_greeting, without_greeting
from opportunity_app.outreach.targets import create_target, update_target
from opportunity_app.core.database import connect_product
from opportunity_app.core.schema import LOCAL_USER_ID

from helpers_platform import build_and_migrate

HOWDY = {"word": "Howdy", "unnamed": "{company} team"}
Y_ALL = {"word": "Hey y'all", "unnamed": "there"}


class GreetingFunctionTests(unittest.TestCase):
    def test_readdress_follows_the_students_own_word(self):
        body = "Howdy Greg,\n\nA note.\n\nSam"
        swapped = readdress_greeting(body, {"greg"}, "Dana", "Acme", style=HOWDY)
        self.assertEqual(swapped, ("Howdy Dana,\n\nA note.\n\nSam", "Howdy Greg,", "Howdy Dana,"))
        leading = readdress_greeting("Howdy Greg, a note.\n\nSam", {"greg"}, "Dana", "Acme", style=HOWDY)
        self.assertEqual(leading[0], "Howdy Dana, a note.\n\nSam")

    def test_a_multi_word_greeting_word_is_recognised(self):
        swapped = readdress_greeting("Hey y'all Greg,\n\nA note.", {"greg"}, "Dana", "Acme", style=Y_ALL)
        self.assertEqual(swapped[0], "Hey y'all Dana,\n\nA note.")

    def test_the_default_words_still_read_for_a_student_with_their_own(self):
        # A draft written before the student chose "Howdy" still opens "Hi Greg,".
        swapped = readdress_greeting("Hi Greg,\n\nA note.", {"greg"}, "Dana", "Acme", style=HOWDY)
        self.assertEqual(swapped[0], "Hi Dana,\n\nA note.")

    def test_a_greeting_word_with_regex_characters_is_taken_literally(self):
        style = {"word": "Hi.", "unnamed": "there"}
        self.assertIsNone(readdress_greeting("Hix Greg,\n\nA note.", {"greg"}, "Dana", "Acme", style=style))

    def test_without_greeting_strips_the_students_word(self):
        self.assertEqual(without_greeting("Howdy Greg,\n\nA note.", HOWDY), ["", "A note."])
        self.assertEqual(without_greeting("Howdy Greg, a note.\n\nSam", HOWDY), ["a note.", "", "Sam"])
        self.assertEqual(without_greeting("Howdy Greg,\n\nA note.", HOWDY), without_greeting("Howdy Dana,\n\nA note.", HOWDY))

    def test_greets_contact_with_the_students_word(self):
        self.assertTrue(greets_contact("Howdy Dana,\n\nA note.", "Dana Ruiz", "Acme", HOWDY))
        self.assertTrue(greets_contact("Howdy Acme team,\n\nA note.", "", "Acme", HOWDY))
        self.assertFalse(greets_contact("Howdy Greg,\n\nA note.", "Dana Ruiz", "Acme", HOWDY))


class ResendWithGreetingWordTests(unittest.TestCase):
    def test_a_bounce_resend_is_not_refused_for_a_howdy_greeting(self):
        before = {"draft_status": "approved", "email_subject": "Question", "email_body": "Howdy Greg,\n\nShort note.\n\nSam"}
        after = {"email_subject": "Question", "email_body": "Howdy Dana,\n\nShort note.\n\nSam", "company": "Bovi",
                 "contact_name": "Dana Ruiz", "contact_bounced": False, "cc_bounced": False}
        choice = {"to": {"email": "dana@bovi.test"}, "cc": None, "basis": "confirmed"}
        self.assertEqual(resend_refusal(before, after, choice, resent_before=False, style=HOWDY), "")
        # Still refused when the words changed, or the greeting is to someone else.
        edited = {**after, "email_body": "Howdy Dana,\n\nSomething else.\n\nSam"}
        self.assertIn("More than the greeting", resend_refusal(before, edited, choice, resent_before=False, style=HOWDY))
        wrong = {**after, "email_body": before["email_body"]}
        self.assertIn("not to the new contact", resend_refusal(before, wrong, choice, resent_before=False, style=HOWDY))


class UnnamedContactCheckTests(unittest.TestCase):
    def inputs(self, greeting):
        return {
            "student": {"name": "Sam"}, "company_research": {"company": "Bovi"}, "unverified_research": {},
            "source_urls": [], "primary_experience": "", "greeting": greeting,
        }

    def problems(self, body, greeting, style=None):
        raw = json.dumps({"subject": "Question", "body": body, "claims": [{"text": "x", "basis": "profile:name"}]})
        _, problems = validate_draft(raw, self.inputs(greeting), "initial", style=style)
        return [problem for problem in problems if "the contact has no name" in problem]

    def test_a_howdy_greeting_to_an_invented_name_is_caught(self):
        self.assertTrue(self.problems("Howdy Dana,\n\nA note.\n\nSam", "Howdy Bovi team,", HOWDY))

    def test_the_expected_howdy_greeting_passes(self):
        self.assertFalse(self.problems("Howdy Bovi team,\n\nA note.\n\nSam", "Howdy Bovi team,", HOWDY))

    def test_the_default_word_is_still_caught(self):
        self.assertTrue(self.problems("Hi Dana,\n\nA note.\n\nSam", "Hi Bovi team,"))


class ContactChangeWithGreetingWordTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        _, path = build_and_migrate(root)
        self.conn = connect_product(path)
        self.profile = root / "profile.json"
        patcher = mock.patch.object(outreach_location, "PROFILE_PATH", self.profile)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.profile.write_text(json.dumps({"name": "Test Student", "greeting_word": "Howdy", "unnamed_greeting": "{company} team"}), encoding="utf-8")
        outreach_location._PROFILE_DATA_CACHE.update(key=None, data={})

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_a_new_contact_readdresses_a_howdy_draft(self):
        target = create_target(self.conn, {
            "company": "Acme", "contact_name": "Greg Lee", "contact_email": "greg@acme.test", "email_body": "Howdy Greg,\n\nA note.",
        }, user_id=LOCAL_USER_ID)
        inbox = update_target(self.conn, target["id"], {"contact_name": "", "contact_email": "info@acme.test"}, user_id=LOCAL_USER_ID)
        self.assertEqual(inbox["email_body"], "Howdy Acme team,\n\nA note.")
        added = add_manual_contact(self.conn, target["id"], user_id=LOCAL_USER_ID, email="dana@acme.test", name="Dana Ruiz")
        self.assertEqual(added["email_body"], "Howdy Dana,\n\nA note.")


if __name__ == "__main__":
    unittest.main()
