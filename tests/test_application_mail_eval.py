"""The keyword rules and sender priors for application mail, measured on 60 synthetic emails labelled before the rules ran.

The labels in tests/fixtures/application_mail_eval.json were written from each
email alone (see its _about). This test pins what the rules scored on them, so
a change to the rules shows up here as a change in these numbers, and it
recomputes application_inbox.AUTO_ACT_MIN_CONFIDENCE: the lowest confidence at
which nothing is wrongly rejected or moved to interview on its own, never
below the plan's 0.85 floor.

Jev is not measured: this repository has no TypeSafe key in tests, and .env is
never read. That is why a Jev answer may act on its own only when it agrees
with these rules (application_inbox's docstring).

The rules were written alongside this set, so the numbers describe it, not
real mail.

The fixture's added_after_review emails are ordinary confirmations (and one
interview invitation) that review found the rules misreading as rejections,
interviews, scheduling requests or offers. They were chosen because the rules
got them wrong, so they stay out of the blind set's precision; each must now
get its label, and none may act on its own wrongly at the threshold.
"""

import json
import re
import sys
import unittest
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from opportunity_app.applications import inbox as application_inbox, mail_rules as application_mail_rules
from opportunity_app.mail import trust as mail_trust
from opportunity_app.mail.message import host_of

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "application_mail_eval.json"
# What the rules scored on 2026-09-27: precision per predicted label (correct / predicted).
PINNED_PRECISION = {
    "application_confirmation": (10, 10),
    "assessment": (8, 8),
    "deadline": (4, 4),
    "interview": (8, 9),
    "offer": (4, 4),
    "recruiter_reply": (4, 5),
    "rejected": (9, 10),
    "scheduling": (5, 6),
    "unknown": (2, 4),
}
AUTO_LABELS = ("rejected", "interview")
# When the set's emails arrived, so a stated date reads the same whatever day the test runs.
RECEIVED = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)

try:
    import realdata_guard
except ImportError:  # imported as tests.<module>, with tests/ not on sys.path
    from tests import realdata_guard
# Guards this module's own run against opening data/*.db (AGENTS.md hard rule 1); see tests/realdata_guard.py.
realdata_guard.install()


def examples(key="emails"):
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    rows = []
    for item in data[key]:
        domain = item["sender"].rsplit("@", 1)[1].rstrip(">").strip().lower()
        hosts = [host_of(link.rstrip(".,")) for link in re.findall(r"https?://[^\s<>\"']+", item["body"])]
        label, confidence, _prior = application_mail_rules.classify_rules(item["subject"], item["body"], domain, hosts, RECEIVED)
        rows.append({
            "id": item["id"], "truth": item["label"], "label": label, "confidence": confidence,
            # What 1.3a lets act at all, before confidence: a job system or assessment platform, or a
            # company domain the student trusted; never a newsletter or a forward.
            "may_act": (bool(mail_trust.listed(domain, mail_trust.AUTHORIZING_CATEGORIES)) or bool(item.get("trusted_employer")))
            and not item.get("bulk") and not re.match(r"^\s*(fwd?|fw)\s*:", item["subject"], re.IGNORECASE),
        })
    return rows


def false_auto_acts(rows, threshold):
    return [row["id"] for row in rows
            if row["label"] in AUTO_LABELS and row["may_act"] and row["confidence"] >= threshold and row["label"] != row["truth"]]


class ApplicationMailEvalTests(unittest.TestCase):
    def setUp(self):
        self.rows = examples()

    def test_the_set_is_sixty_emails_across_every_label(self):
        self.assertEqual(len(self.rows), 60)
        self.assertEqual(len({row["id"] for row in self.rows}), 60)
        truths = Counter(row["truth"] for row in self.rows)
        self.assertEqual(set(truths), set(PINNED_PRECISION), "every label, the tricky unknowns included")

    def test_precision_per_label_is_pinned(self):
        predicted = Counter(row["label"] for row in self.rows)
        correct = Counter(row["label"] for row in self.rows if row["label"] == row["truth"])
        measured = {label: (correct[label], predicted[label]) for label in predicted}
        self.assertEqual(measured, PINNED_PRECISION, "the rules changed: re-measure and update PINNED_PRECISION on purpose")

    def test_the_threshold_is_the_lowest_with_no_false_automatic_rejection_or_interview(self):
        confidences = sorted({row["confidence"] for row in self.rows} | {application_inbox.AUTO_ACT_FLOOR})
        lowest = next(value for value in confidences if not false_auto_acts(self.rows, value))
        self.assertEqual(application_inbox.AUTO_ACT_MIN_CONFIDENCE, max(lowest, application_inbox.AUTO_ACT_FLOOR))
        self.assertEqual(false_auto_acts(self.rows, application_inbox.AUTO_ACT_MIN_CONFIDENCE), [])

    def test_the_tricky_ones_never_act_on_their_own(self):
        by_id = {row["id"]: row for row in self.rows}
        threshold = application_inbox.AUTO_ACT_MIN_CONFIDENCE
        # A rejection of this role that asks about another: labelled rejected, but not sure enough to act alone.
        self.assertEqual(by_id["e17"]["label"], "rejected")
        self.assertLess(by_id["e17"]["confidence"], threshold)
        # A job-system newsletter full of interview words, and a classmate's forwarded rejection.
        for newsletter_or_forward in ("e56", "e58"):
            self.assertFalse(by_id[newsletter_or_forward]["may_act"])
        # Thanks for a finished test from an assessment platform is not a new assessment.
        self.assertEqual(by_id["e59"]["label"], "unknown")

    def test_what_review_found_misread_is_read_right_and_never_acts_wrongly(self):
        added = examples("added_after_review")
        self.assertEqual([(row["id"], row["label"]) for row in added if row["label"] != row["truth"]], [])
        self.assertEqual(false_auto_acts(added, application_inbox.AUTO_ACT_MIN_CONFIDENCE), [])
        # Over both sets together, the threshold is still the floor.
        both = self.rows + added
        confidences = sorted({row["confidence"] for row in both} | {application_inbox.AUTO_ACT_FLOOR})
        lowest = next(value for value in confidences if not false_auto_acts(both, value))
        self.assertEqual(application_inbox.AUTO_ACT_MIN_CONFIDENCE, max(lowest, application_inbox.AUTO_ACT_FLOOR))

    def test_the_security_code_email_is_never_read_as_a_confirmation(self):
        """Spec R4: Greenhouse's "Security code for your application to ..." waits for a code, so it confirms nothing."""
        rows = examples("security_code")
        self.assertEqual([row["id"] for row in rows], ["s01", "s02"])
        for row in rows:
            with self.subTest(email=row["id"]):
                self.assertEqual((row["truth"], row["label"]), ("unknown", "unknown"))
                self.assertLess(row["confidence"], application_inbox.AUTO_ACT_MIN_CONFIDENCE)
        for item in json.loads(FIXTURE.read_text(encoding="utf-8"))["security_code"]:
            with self.subTest(rule=item["id"]):
                domain = item["sender"].rsplit("@", 1)[1].rstrip(">").strip().lower()
                self.assertEqual(
                    application_mail_rules.classify_rules(item["subject"], item["body"], domain, [], RECEIVED)[2], "security_code",
                )
                self.assertTrue(application_mail_rules.SECURITY_CODE_SUBJECT.search(item["subject"]))
        # The blind set is untouched by it.
        self.assertEqual(len(self.rows), 60)

    def test_the_security_code_rule_is_greenhouses_wording_from_greenhouses_senders_only(self):
        """A role titled "Security Code ..." or another platform's code email keeps the label the rules always gave it."""
        from opportunity_app.apply.greenhouse import GREENHOUSE_SENDER_DOMAINS

        self.assertEqual(application_mail_rules.GREENHOUSE_MAIL_DOMAINS, GREENHOUSE_SENDER_DOMAINS, "one list of Greenhouse's domains")
        cases = [
            ("Offer: Security Code Review Intern at Acme Robotics", "We are pleased to offer you the Security Code Review Intern position.", "acme.example", "offer"),
            ("Interview invitation - Security Code Analyst", "We would like to invite you to interview for the Security Code Analyst role.", "acme.example", "interview"),
            ("Interview invitation - Security Code Analyst", "We would like to invite you to interview for the Security Code Analyst role.", "us.greenhouse-mail.io", "interview"),
            ("Your security code for the HackerRank assessment", "Use this code to start your Acme coding assessment.", "hackerrank.com", "assessment"),
        ]
        for subject, body, domain, expected in cases:
            with self.subTest(subject=subject, domain=domain):
                label, _confidence, prior = application_mail_rules.classify_rules(subject, body, domain, [], RECEIVED)
                self.assertEqual((label, prior == "security_code"), (expected, False))
        # Greenhouse's own wording from another sender is not Greenhouse's email either.
        self.assertNotEqual(
            application_mail_rules.classify_rules("Security code for your application to Acme", "Thank you for applying. We received your application.", "hire.lever.co", [], RECEIVED)[2],
            "security_code",
        )
        for domain in ("greenhouse.io", "us.greenhouse-mail.io", "GREENHOUSE-MAIL.IO."):
            self.assertTrue(application_mail_rules.is_security_code_email("Security code for your application to Acme", domain), domain)
        self.assertFalse(application_mail_rules.is_security_code_email("Security code for your application to Acme", "notgreenhouse.io"))
        # The watch and the reader keep the broad subject check: they only decline to count an email.
        self.assertTrue(application_mail_rules.SECURITY_CODE_SUBJECT.search("Your Security Code"))


if __name__ == "__main__":
    unittest.main()
