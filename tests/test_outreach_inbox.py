"""Replies to outreach read from Gmail: logged once, a quiet company moved to Replied, the rest suggested."""

import json
import sys
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from email import policy
from email.parser import BytesParser
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx

from opportunity_app import automation, automation_health, inbox_watcher, outreach_inbox
from opportunity_app.mail import gmail_connection, message as mail_message
from opportunity_app.inbox_watcher import InboxWatcher
from opportunity_app.mail.message import reply_text, strip_quoted
from opportunity_app.core.database import connect_product
from opportunity_app.core.timestamps import utc_now

from helpers_gmail import (
    ACCOUNT,
    INBOX_AUTH,
    INBOX_USER,
    AlwaysInTransaction,
    ReplyCaptureFixture,
    mail,
    now_ms,
    rate_limited,
)

AUTH = INBOX_AUTH
USER = INBOX_USER


class QuotedTextTests(unittest.TestCase):
    def test_the_quoted_email_is_cut_off(self):
        gmail = "Sounds good, Thursday works.\n\nOn Fri, Sep 26, 2026 at 10:02 AM Sam Rivera <sam@school.example>\nwrote:\n> Hi Greg,\n> A short note."
        self.assertEqual(strip_quoted(gmail), "Sounds good, Thursday works.")
        outlook = "Thanks, passing this on.\n\nFrom: Sam Rivera <sam@school.example>\nSent: Friday, September 26, 2026 10:02 AM\nTo: Greg"
        self.assertEqual(strip_quoted(outlook), "Thanks, passing this on.")
        self.assertEqual(strip_quoted("Yes!\n> Would you consider me"), "Yes!")

    def test_an_html_only_reply_is_read_as_text(self):
        raw = (
            f"From: Greg <greg@bovi.example>\nTo: {ACCOUNT}\nSubject: Re: hi\nMIME-Version: 1.0\n"
            "Content-Type: text/html; charset=UTF-8\n\n<div>Let&#39;s talk<br>Tuesday?</div><blockquote>old</blockquote>\n"
        ).encode()
        self.assertEqual(reply_text(BytesParser(policy=policy.default).parsebytes(raw)), "Let's talk\nTuesday?")


class ReplyCaptureTests(ReplyCaptureFixture, unittest.TestCase):
    def test_a_reply_is_logged_and_the_company_moves_to_replied(self):
        target = self.sent_target()
        self.assertTrue(target["follow_up_at"])
        self.arrive("reply-1", mail(
            "Happy to hop on a call. When are you free next week?\n\n"
            "On Fri, Sep 26, 2026 at 10:02 AM Sam <sam@school.example> wrote:\n> Hi Greg,"
        ))
        result = self.check()
        self.assertEqual([item["target_id"] for item in result["replies"]], [target["id"]])
        after = self.target(target)
        self.assertEqual((after["status"], after["follow_up_at"]), ("replied", None), "someone wrote back: no follow-up")
        self.assertEqual(self.replies(target), ["Happy to hop on a call. When are you free next week?"])
        self.assertEqual(after["reply_suggestion"]["status"], "call_scheduled")
        self.assertEqual(after["reply_suggestion"]["from"], "greg@bovi.example")
        self.assertEqual(after["suggestion"]["status"], "call_scheduled", "offered, not applied")
        self.assertIsNotNone(after["call_prep_job"], "call prep starts from the captured reply")
        self.assertIn("from:(bovi.example OR greg@bovi.example)", [q for q in self.gmail.searches if q.startswith("from:(") and "mailer" not in q][0])

    def test_the_same_reply_is_logged_once(self):
        target = self.sent_target()
        self.arrive("reply-1", mail("Thanks, got it."))
        self.check()
        self.assertEqual(self.check()["replies"], [])
        self.assertEqual(len(self.replies(target)), 1)
        fetched = [r for r in self.gmail.requests if r.url.path.endswith("/messages/reply-1")]
        self.assertEqual(len(fetched), 1, "a message already read is not fetched again")

    def test_an_out_of_office_reply_changes_nothing(self):
        target = self.sent_target()
        self.arrive("auto-1", mail("I'm away until October 6.", subject="Automatic reply: Robotics internship question",
                                   headers="Auto-Submitted: auto-replied\n"))
        result = self.check()
        self.assertEqual((result["replies"], len(result["automatic"])), ([], 1))
        after = self.target(target)
        self.assertEqual(after["status"], "sent")
        self.assertEqual(after["reply_count"], 0)
        self.assertIn("auto_reply", [event["event_type"] for event in after["events"]])

    def test_a_colleague_answering_counts_but_a_newsletter_or_fresh_email_does_not(self):
        target = self.sent_target()
        # From the contact's own address, as a shared info@ inbox sends newsletters.
        self.arrive("news-1", mail("Our September update", sender="Bovi <greg@bovi.example>", subject="Bovi news",
                                   headers="List-Unsubscribe: <mailto:unsub@bovi.example>\n"))
        self.arrive("sales-1", mail("Want a demo?", sender="Sales <sales@bovi.example>", subject="Bovi demo"))
        self.assertEqual(self.check()["replies"], [])
        self.assertEqual(self.target(target)["status"], "sent")
        self.arrive("ceo-1", mail("Greg forwarded your note. Let's talk.", sender="Ana Bovi <ana@bovi.example>"))
        self.assertEqual(len(self.check()["replies"]), 1)
        self.assertEqual(self.target(target)["status"], "replied")

    def test_a_contact_on_a_shared_email_domain_is_matched_only_by_address(self):
        target = self.sent_target(contact_email="greg.bovi@gmail.com", website="https://bovi.example")
        self.arrive("stranger-1", mail("Re: something else", sender="Someone <someone@gmail.com>"))
        self.assertEqual(self.check()["replies"], [])
        self.assertFalse([q for q in self.gmail.searches if "gmail.com OR" in q or q.startswith("from:(gmail.com")])
        self.arrive("greg-1", mail("Yes, let's chat.", sender="Greg <greg.bovi@gmail.com>"))
        self.assertEqual(len(self.check()["replies"]), 1)
        self.assertEqual(self.target(target)["status"], "replied")

    def test_mail_from_before_the_send_is_not_a_reply(self):
        target = self.sent_target()
        self.arrive("old-1", mail("Re: an older thread"), received=now_ms(-timedelta(days=3)))
        self.assertEqual(self.check()["replies"], [])
        self.assertEqual(self.target(target)["status"], "sent")

    def test_a_company_marked_sent_by_hand_is_watched_too(self):
        created = self.client.post("/api/v1/outreach", headers=AUTH, json={
            "company": "Kiva", "contact_email": "ana@kiva.example", "status": "sent",
        }).json()
        self.arrive("kiva-1", mail("We'd love to talk.", sender="Ana <ana@kiva.example>"))
        self.assertEqual(len(self.check()["replies"]), 1)
        self.assertEqual(self.target(created)["status"], "replied")

    def test_a_decline_is_suggested_and_answering_it_clears_the_suggestion(self):
        target = self.sent_target()
        self.arrive("no-1", mail("Thanks for reaching out, but we're not hiring interns this term."))
        self.check()
        after = self.target(target)
        self.assertEqual((after["status"], after["reply_suggestion"]["status"]), ("replied", "declined"))
        declined = self.client.patch(f"/api/v1/outreach/{target['id']}", headers=AUTH, json={"status": "declined"}).json()
        self.assertIsNone(declined["reply_suggestion"])

    def test_a_suggestion_can_be_dismissed(self):
        target = self.sent_target()
        self.arrive("call-1", mail("Can we set up a call? What's your availability?"))
        self.check()
        dismissed = self.client.delete(f"/api/v1/outreach/{target['id']}/reply-suggestion", headers=AUTH)
        self.assertEqual(dismissed.status_code, 200, dismissed.text)
        self.assertIsNone(dismissed.json()["reply_suggestion"])
        self.assertEqual(dismissed.json()["status"], "replied")

    def test_a_reply_about_a_failed_delivery_is_still_a_reply(self):
        target = self.sent_target()
        self.arrive("person-1", mail("Your first message was undeliverable to our old inbox, but I got this one. Let's talk."))
        self.check()
        after = self.target(target)
        self.assertEqual(after["status"], "replied")
        self.assertIsNone(after["reply_suggestion"])

    def test_without_the_read_scope_it_asks_for_a_reconnect(self):
        self.sent_target()
        self.gmail.thread_status = 403
        self.assertEqual(self.check()["state"], "needs_reconnect")

    def test_mail_from_just_before_the_send_is_not_a_reply(self):
        target = self.sent_target()
        self.arrive("early-1", mail("Re: our earlier chat"), received=now_ms(-timedelta(minutes=1)))
        self.assertEqual(self.check()["replies"], [])
        self.assertEqual(self.target(target)["status"], "sent")

    def test_anyone_at_the_companys_own_domain_counts_even_if_the_contact_is_elsewhere(self):
        target = self.sent_target(contact_email="greg@bovi-mail.example")
        self.arrive("ana-1", mail("Greg passed this on. Let's talk.", sender="Ana <ana@eng.bovi.example>"))
        self.assertEqual(len(self.check()["replies"]), 1)
        self.assertEqual(self.target(target)["status"], "replied")

    def test_a_search_that_fails_is_reported_not_taken_as_nothing(self):
        self.sent_target()
        self.gmail.thread_status = 400
        self.assertEqual(self.check()["state"], "unreachable")
        # A Gmail server error is a passing fault: reads wait, as for a rate limit, and it is still not "nothing".
        self.gmail.thread_status = 503
        self.assertEqual(self.check()["state"], "throttled")
        self.assertIsNotNone(gmail_connection.backoff_until(USER))

    def test_every_page_of_results_is_read(self):
        target = self.sent_target()
        self.gmail.page_size = 1
        for number in range(3):
            self.arrive(f"news-{number}", mail("Our update", subject="Bovi news", headers="List-Unsubscribe: <mailto:x@bovi.example>\n"))
        self.arrive("reply-late", mail("Yes, let's talk."))
        self.assertEqual(len(self.check()["replies"]), 1, "the reply on the last page is found")
        self.assertEqual(self.target(target)["status"], "replied")

    def test_two_checks_reading_the_same_reply_log_it_once(self):
        from opportunity_app.outreach_inbox import _record_reply

        target = self.sent_target()
        with closing(connect_product(self.platform_path)) as conn:
            current = self.target(target)
            first = _record_reply(conn, current, user_id=USER, gmail_id="same-1", sender="greg@bovi.example",
                                  received=utc_now(), text="Sure.", decisions=None)
            second = _record_reply(conn, current, user_id=USER, gmail_id="same-1", sender="greg@bovi.example",
                                   received=utc_now(), text="Sure.", decisions=None)
        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertEqual(len(self.replies(target)), 1)

    # --- Replies from someone other than the address written to --------------------------

    def inbox_row(self, gmail_id):
        with closing(connect_product(self.platform_path)) as conn:
            row = conn.execute("SELECT * FROM outreach_inbox_messages WHERE gmail_id=?", (gmail_id,)).fetchone()
            return dict(row) if row else None

    def notices(self):
        with closing(connect_product(self.platform_path)) as conn:
            return [(n["title"], n["body"]) for n in automation.list_notices(conn, USER)]

    def decide(self, target, gmail_id, decision):
        return self.client.post(f"/api/v1/outreach/{target['id']}/possible-replies/{gmail_id}", headers=AUTH,
                                json={"decision": decision})

    def events(self, target, kind):
        return [event["detail"] for event in self.target(target)["events"] if event["event_type"] == kind]

    def test_a_fresh_email_from_a_named_person_at_the_company_is_a_reply(self):
        # Written to the shared inbox; answered by an engineer in a new email with a new subject.
        target = self.sent_target(contact_email="info@bovi.example")
        self.arrive("lead-1", mail(
            "Hi Sam,\nThis is Dana, I'm the lead engineer at Bovi - let's set up a quick call this week. "
            "When are you available? I will send a video call invite.",
            sender="Dana Lee <dana@bovi.example>", subject="BOVI: Interview",
        ))
        result = self.check()
        self.assertEqual([(item["company"], item["from"]) for item in result["replies"]], [("Bovi", "dana@bovi.example")])
        self.assertEqual(result["possible"], [])
        after = self.target(target)
        self.assertEqual(after["status"], "replied")
        self.assertTrue(self.replies(target)[0].startswith("Hi Sam,\nThis is Dana"))
        self.assertEqual(after["reply_suggestion"]["status"], "call_scheduled")
        row = self.inbox_row("lead-1")
        self.assertEqual((row["kind"], row["via"], row["reason"], row["rules"]), ("reply", "domain", "domain_person", outreach_inbox.RULES))
        [found] = self.events(target, "reply_found")
        self.assertIn("dana@bovi.example wrote to you from the company's own domain", found)
        self.assertIn("You wrote to info@bovi.example", found)
        self.assertIn(("Bovi replied", "Their reply is logged in Outreach."), self.notices(),
                      "said outside the page too, with no address or words in the notice")

    def test_weaker_evidence_from_the_companys_domain_waits_as_a_possible_reply(self):
        cases = [
            ("unverified-1", mail("Let's talk.", sender="Dana Lee <dana@bovi.example>", subject="Hi", verified=False), "not_verified"),
            ("bcc-1", mail("Our quarterly update, friends.", sender="Dana Lee <dana@bovi.example>", subject="Update",
                           to="friends@bovi.example"), "not_addressed"),
            ("name-1", mail("Saw you reached out. Evaluating arms for your lab?", sender="Bovi Sales Team <jake@bovi.example>",
                            subject="Quick question"), "name_mismatch"),
            ("careers-1", mail("Could you send your availability?", sender="Bovi Careers <careers@bovi.example>",
                               subject="Next steps"), "shared_address"),
            ("ats-1", mail("Thanks for applying. See https://boards.greenhouse.io/bovi/jobs/1", sender="Dana Lee <dana@bovi.example>",
                           subject="Your application"), "job_mail"),
        ]
        target = self.sent_target()
        for gmail_id, raw, _reason in cases:
            self.arrive(gmail_id, raw)
        result = self.check()
        self.assertEqual(result["replies"], [])
        after = self.target(target)
        self.assertEqual((after["status"], after["reply_count"], after["possible_reply_count"]), ("sent", 0, len(cases)),
                         "none counted until the student says; all shown")
        self.assertEqual({item["gmail_id"]: item["reason"] for item in after["possible_replies"]},
                         {gmail_id: reason for gmail_id, _raw, reason in cases})
        self.assertIsNone(after["suggestion"])

    def test_a_possible_reply_is_shown_and_logged_when_the_student_says_so(self):
        target = self.sent_target()
        self.arrive("careers-2", mail("Thanks for your note. Could you send over your availability?",
                                      sender="Bovi Careers <careers@bovi.example>", subject="Next steps"))
        result = self.check()
        self.assertEqual([(item["company"], item["from"]) for item in result["possible"]], [("Bovi", "careers@bovi.example")])
        after = self.target(target)
        [waiting] = after["possible_replies"]
        self.assertEqual((waiting["gmail_id"], waiting["from"], waiting["subject"], waiting["in_spam"]),
                         ("careers-2", "careers@bovi.example", "Next steps", False))
        self.assertIn("could you send over your availability", waiting["preview"].casefold())
        self.assertEqual(waiting["reason_text"], "careers@ is a shared inbox, not one person")
        self.assertEqual(waiting["gmail_url"], f"https://mail.google.com/mail/?authuser={ACCOUNT.replace('@', '%40')}#all/careers-2")
        self.assertIn("possible_reply", [event["event_type"] for event in after["events"]])
        self.assertIn(("Bovi may have replied", "Open Outreach to check it."), self.notices())
        decided = self.decide(target, "careers-2", "reply")
        self.assertEqual(decided.status_code, 200, decided.text)
        body = decided.json()
        self.assertEqual((body["status"], body["reply_count"], body["possible_reply_count"], body["possible_replies"]),
                         ("replied", 1, 0, []))
        self.assertEqual(self.replies(target), ["Thanks for your note. Could you send over your availability?"])
        row = self.inbox_row("careers-2")
        self.assertEqual((row["kind"], row["text"]), ("reply", ""), "the words live in the history now, not twice")
        self.assertIsNotNone(row["decided_at"])
        self.assertEqual(self.decide(target, "careers-2", "reply").status_code, 409)
        self.assertEqual(self.decide(target, "careers-2", "not_reply").status_code, 409)
        self.assertNotIn(("Bovi replied", "Their reply is logged in Outreach."), self.notices(),
                         "no notice about what the student just did")

    def test_a_possible_reply_can_be_set_aside_and_its_words_are_not_kept(self):
        target = self.sent_target()
        self.arrive("sales-2", mail("Want a demo of Bovi Cloud?", sender="Sales <sales@bovi.example>", subject="Bovi demo"))
        self.check()
        dismissed = self.decide(target, "sales-2", "not_reply")
        self.assertEqual(dismissed.status_code, 200, dismissed.text)
        self.assertEqual((dismissed.json()["status"], dismissed.json()["possible_replies"]), ("sent", []))
        self.assertIn("possible_reply_dismissed", [event["event_type"] for event in dismissed.json()["events"]])
        row = self.inbox_row("sales-2")
        self.assertEqual((row["kind"], row["text"]), ("dismissed", ""))
        self.assertEqual(self.check()["possible"], [], "never asked about again")
        self.assertEqual(self.decide(target, "sales-2", "reply").status_code, 409)
        self.assertEqual(self.decide(target, "nope-1", "reply").status_code, 404)
        self.assertEqual(self.client.post(f"/api/v1/outreach/{target['id']}/possible-replies/sales-2", headers=AUTH,
                                          json={"decision": "maybe"}).status_code, 422)

    def test_someone_at_the_company_answering_in_the_thread_counts_from_any_address(self):
        target = self.sent_target(contact_email="info@bovi.example")
        self.arrive_in_thread("ana-4", mail("Passing this to our engineers. Free Thursday?", sender="Ana <ana@eng.bovi.example>",
                                            verified=False))
        result = self.check()
        self.assertEqual([(item["company"], item["from"]) for item in result["replies"]], [("Bovi", "ana@eng.bovi.example")])
        self.assertEqual(self.target(target)["status"], "replied")
        row = self.inbox_row("ana-4")
        self.assertEqual((row["via"], row["reason"], row["thread_id"]), ("thread", "thread", "thread-1"))

    def test_someone_outside_the_company_in_the_thread_is_only_a_possible_reply(self):
        # A founder answering from a personal address, or a mentor the student forwarded the email to.
        target = self.sent_target(contact_email="info@bovi.example")
        self.arrive_in_thread("dana-2", mail("Saw your note to our info inbox. Free Thursday?", sender="Dana <dana.bovi@gmail.com>"))
        result = self.check()
        self.assertEqual(result["replies"], [])
        self.assertEqual([item["from"] for item in result["possible"]], ["dana.bovi@gmail.com"])
        self.assertEqual(self.target(target)["possible_replies"][0]["reason"], "thread_outsider")
        self.assertEqual(self.inbox_row("dana-2")["via"], "thread")

    def test_a_delivery_report_in_the_thread_is_never_a_reply(self):
        # An Exchange server's undeliverable report, from an address at the company that is not a daemon's.
        target = self.sent_target(contact_cc="info@bovi.example")
        report = (
            "From: Microsoft Outlook <MicrosoftExchange329e71ec88ae4615@bovi.example>\n"
            f"To: {ACCOUNT}\nSubject: Undeliverable: Robotics internship question\nMIME-Version: 1.0\n"
            'Content-Type: multipart/report; report-type=delivery-status; boundary="b"\n\n'
            "--b\nContent-Type: text/plain\n\nDelivery has failed to these recipients: greg@bovi.example\n--b--\n"
        ).encode()
        self.arrive_in_thread("ndr-1", report)
        result = self.check()
        self.assertEqual((result["replies"], result["possible"]), ([], []))
        after = self.target(target)
        self.assertEqual((after["reply_count"], after["possible_reply_count"]), (0, 0))
        self.assertNotEqual(after["status"], "replied")

    def test_a_help_desk_receipt_in_the_thread_is_automatic(self):
        target = self.sent_target(contact_email="info@bovi.example")
        self.arrive_in_thread("desk-1", mail("We received your request #4412. This is an automated message; do not reply to this email.",
                                             sender="Bovi Support <support@bovi.zendesk.example>",
                                             subject="Re: Robotics internship question"))
        result = self.check()
        self.assertEqual((result["replies"], result["possible"], len(result["automatic"])), ([], [], 1))
        self.assertEqual(self.target(target)["status"], "sent")

    def test_a_message_found_for_a_company_has_its_stored_row_read_once_before_it_is_judged(self):
        """Read already? and judged by older rules? come from one lookup; a set-aside note re-reads the row only inside its write."""
        self.sent_target()
        self.arrive("reply-q", mail("Yes, let's talk."))
        self.arrive("auto-q", mail("Out of office until Monday.", subject="Automatic reply: Robotics internship question",
                                   headers="Auto-Submitted: auto-replied\n"))
        lookups = []
        with closing(connect_product(self.platform_path)) as conn:
            conn.set_trace_callback(lookups.append)
            outreach_inbox.capture_replies(conn, user_id=USER, client_factory=self.factory)

        def asked(gmail_id, select):
            return len([sql for sql in lookups if sql.startswith(select) and "FROM outreach_inbox_messages WHERE user_id=" in sql
                        and f"gmail_id='{gmail_id}'" in sql])

        for gmail_id in ("reply-q", "auto-q"):
            # Once when it is first read, and once more when a second listing names it and finds it read already.
            self.assertEqual(asked(gmail_id, "SELECT kind, rules FROM"), 2, gmail_id)
            self.assertEqual(asked(gmail_id, "SELECT 1 FROM"), 0, f"{gmail_id}: judged-before is read off the stored row, not asked again")
        self.assertEqual(asked("reply-q", "SELECT kind FROM"), 0)
        self.assertEqual(asked("auto-q", "SELECT kind FROM"), 1, "inside the write that sets it aside")
        self.assertEqual(self.inbox_row("reply-q")["kind"], "reply")
        self.assertEqual(self.inbox_row("auto-q")["kind"], "automatic")

    def test_the_look_before_an_automatic_send_reads_the_companys_threads_now(self):
        target = self.sent_target()
        self.check()
        read = lambda: len([get for get in self.gmail.thread_gets if get[0] == "thread-1" and "from" in get[2]])
        before = read()
        self.arrive_in_thread("late-1", mail("Yes, let's talk.", sender="Ana <ana@bovi.example>"))
        with closing(connect_product(self.platform_path)) as conn:
            found = outreach_inbox.capture_replies(conn, user_id=USER, client_factory=self.factory, force_target=target["id"])
        self.assertEqual(read(), before + 1, "its thread was read directly")
        self.assertEqual([item["from"] for item in found["replies"]], ["ana@bovi.example"])
        self.gmail.gone_threads.add("thread-1")
        with closing(connect_product(self.platform_path)) as conn:
            gone = outreach_inbox.capture_replies(conn, user_id=USER, client_factory=self.factory, force_target=target["id"])
        self.assertEqual(gone["state"], "ok", "a thread deleted for good holds nothing up")

    def test_a_thread_that_cannot_be_read_is_not_taken_as_no_reply(self):
        target = self.sent_target()
        self.gmail.thread_answers.append(lambda: httpx.Response(500))
        with closing(connect_product(self.platform_path)) as conn:
            look = outreach_inbox.capture_replies(conn, user_id=USER, client_factory=self.factory, force_target=target["id"])
        self.assertNotEqual(look["state"], "ok")

    def test_a_skipped_check_says_so(self):
        self.sent_target()
        self.check()
        with closing(connect_product(self.platform_path)) as conn:
            skipped = outreach_inbox.capture_replies(conn, user_id=USER, client_factory=self.factory)
        self.assertTrue(skipped.get("skipped"), "not recorded as a check that ran")

    def test_a_colleague_at_the_contacts_own_domain_counts_when_the_site_is_elsewhere(self):
        # The website on one domain, the contact's mail on another with the same name.
        target = self.sent_target(contact_email="greg@bovi.us.example", website="https://bovi.us.example")
        self.arrive("ana-2", mail("Greg is out this week. Can we talk Monday?", sender="Ana Diaz <ana@bovi.us.example>",
                                  subject="Your internship note"))
        self.assertEqual(len(self.check()["replies"]), 1)
        self.assertEqual(self.target(target)["status"], "replied")

    def test_a_contact_domain_unrelated_to_the_site_is_watched_by_address_only(self):
        self.sent_target(contact_email="kate@bigfund.example", website="https://bovi.example")
        self.arrive("fund-1", mail("Join our portfolio mixer.", sender="Events <sam@bigfund.example>", subject="Mixer"))
        self.assertEqual(self.check()["possible"], [])
        query = [q for q in self.gmail.searches if q.startswith("from:(") and "mailer" not in q][0]
        self.assertIn("from:(bovi.example OR kate@bigfund.example)", query)

    def test_the_students_own_domain_and_platform_pages_never_stand_for_a_company(self):
        self.sent_target(company="Campus Lab", contact_email="prof@school.example", website="https://www.linkedin.com/company/bovi")
        self.check()
        query = [q for q in self.gmail.searches if q.startswith("from:(") and "mailer" not in q][0]
        self.assertIn("from:(prof@school.example)", query, "only the address: not all of the student's school, nor LinkedIn")

    def test_a_big_companys_own_domain_is_kept(self):
        self.sent_target(company="Bigco", contact_email="jane@bigco.example", website="https://careers.bigco.example")
        self.check()
        query = [q for q in self.gmail.searches if q.startswith("from:(") and "mailer" not in q][0]
        self.assertIn("from:(bigco.example OR jane@bigco.example)", query, "careers. is a section of the site")

    def test_a_reply_gmail_put_in_spam_is_found_and_shown_but_not_counted(self):
        target = self.sent_target()
        self.arrive("spam-1", mail("Yes, let's talk.", sender="Ana Diaz <ana@bovi.example>", subject="Internship"), labels=["SPAM"])
        self.arrive("spam-2", mail("Claim your prize", sender="Prizes <no-reply@bovi.example>", subject="Prize", verified=False),
                    labels=["SPAM"])
        self.arrive("spam-3", mail("Greg forwarded your note. Free Tuesday?", sender="Priya Nair <priya@bovi.example>",
                                   subject="Tuesday", verified=False), labels=["SPAM"])
        self.arrive("trash-1", mail("Yes, let's talk.", sender="Ana Diaz <ana@bovi.example>", subject="Internship"), labels=["TRASH"])
        result = self.check()
        self.assertEqual(result["replies"], [])
        waiting = {item["gmail_id"]: item for item in self.target(target)["possible_replies"]}
        self.assertEqual(sorted(waiting), ["spam-1", "spam-3"])
        self.assertEqual((waiting["spam-1"]["in_spam"], waiting["spam-1"]["reason"]), (True, "spam"))
        self.assertIn("#spam/spam-1", waiting["spam-1"]["gmail_url"])
        self.assertEqual((waiting["spam-3"]["in_spam"], waiting["spam-3"]["reason"]), (True, "not_verified"),
                         "a person at the company whose mail Gmail doubted is still shown, marked as in Spam")
        self.assertEqual(self.inbox_row("spam-2")["reason"], "spam_unverified", "an unverified machine sender in Spam is set aside, with why")
        self.assertIsNone(self.inbox_row("trash-1"), "Trash is never searched")
        search = [params for params in self.gmail.search_params if params.get("q", "").startswith("from:(") and "mailer" not in params["q"]][0]
        self.assertEqual(search.get("includeSpamTrash"), "true")
        self.assertIn("-in:trash", search["q"])

    def test_mail_from_a_mailing_tool_is_never_silently_dropped(self):
        target = self.sent_target()
        self.arrive("tool-1", mail("Hi Sam, happy to chat Tuesday.", subject="Chat?",
                                   headers="List-Unsubscribe: <mailto:unsub@bovi.example>\nPrecedence: bulk\n"))
        self.arrive("news-2", mail("Our September update", sender="Bovi News <news@bovi.example>", subject="Bovi news",
                                   headers="List-Id: <news.bovi.example>\nList-Unsubscribe: <mailto:x@bovi.example>\n"))
        result = self.check()
        self.assertEqual([(item["from"], item["reason"]) for item in result["possible"]], [("greg@bovi.example", "mailing_tool")],
                         "the person written to, through a tool: shown; a newsletter: set aside")
        self.assertEqual(self.inbox_row("news-2")["reason"], "list")
        self.assertEqual(result["automatic"], [], "a bulk precedence is not an out-of-office")
        self.assertEqual(self.target(target)["status"], "sent")

    def test_a_quick_human_answer_is_not_mistaken_for_a_receipt(self):
        target = self.sent_target()
        self.arrive("quick-1", mail("Thanks for reaching out! Are you free for a call Thursday?", subject="Hi Sam"),
                    received=now_ms(timedelta(minutes=3)))
        self.assertEqual(len(self.check()["replies"]), 1)
        self.assertEqual(self.target(target)["status"], "replied")

    def test_a_sender_is_matched_against_every_company_not_one_search_batch(self):
        # 21 companies take two searches. Jane writes from Bovi's domain, but she is the contact of
        # the company in the second batch: the address outranks the domain.
        bovi = self.sent_target(contact_email="info@bovi.example")
        for number in range(19):
            self.sent_target(company=f"Filler {number}", contact_email=f"hi@filler{number}.example", website=f"https://filler{number}.example")
        kiva = self.sent_target(company="Kiva", contact_email="jane@bovi.example", website="https://kiva.example")
        self.arrive("jane-1", mail("Yes, let's talk.", sender="Jane <jane@bovi.example>", subject="Hi"))
        result = self.check()
        self.assertGreaterEqual(len([q for q in self.gmail.searches if q.startswith("from:(") and "mailer" not in q]), 2)
        self.assertEqual([item["company"] for item in result["replies"]], ["Kiva"])
        self.assertEqual((self.target(kiva)["status"], self.target(bovi)["status"]), ("replied", "sent"))

    def test_two_companies_on_one_domain_hold_both_until_the_student_says(self):
        first = self.sent_target(contact_email="greg@bovi.example")
        second = self.sent_target(company="Bovi Labs", contact_email="lab@bovi.example")
        self.arrive("ana-3", mail("Let's talk.", sender="Ana Diaz <ana@bovi.example>", subject="Your note"))
        result = self.check()
        self.assertEqual(result["replies"], [])
        [possible] = result["possible"]
        self.assertEqual(possible["target_id"], second["id"], "named for the one written to last")
        for target in (first, second):
            after = self.target(target)
            self.assertEqual((after["possible_reply_count"], after["possible_replies"][0]["reason"]), (1, "ambiguous"),
                             "every company it could be from waits")
        self.assertIn(("A company you wrote to may have replied", "Open Outreach to check it."), self.notices())
        decided = self.decide(first, "ana-3", "reply")
        self.assertEqual(decided.status_code, 200, decided.text)
        self.assertEqual(decided.json()["status"], "replied", "logged on the company the student picked")
        self.assertEqual((self.target(second)["possible_reply_count"], self.target(second)["status"]), (0, "sent"))

    def test_mail_naming_the_company_from_anywhere_is_a_possible_reply(self):
        target = self.sent_target(company="Bovi Robotics")
        self.arrive("personal-1", mail("Hi Sam, I run Bovi Robotics. Let's talk!", sender="Dana <dana.home@gmail.com>",
                                       subject="Your email to our team"))
        self.arrive("alert-1", mail("News: Bovi Robotics raises a seed round", sender="Alerts <alerts@news.example>",
                                    subject="Bovi Robotics in the news", headers="List-Unsubscribe: <mailto:x@news.example>\n"))
        result = self.check()
        self.assertEqual([(item["from"], item["reason"]) for item in result["possible"]], [("dana.home@gmail.com", "mentions_company")])
        self.assertEqual(self.target(target)["possible_reply_count"], 1)
        self.assertEqual(self.inbox_row("alert-1")["reason"], "no_company")

    def test_a_reply_to_an_email_sent_in_gmail_counts_from_when_gmail_sent_it(self):
        target = self.sent_target()
        with closing(connect_product(self.platform_path)) as conn:
            # These rules started yesterday (mail from before them is never counted without the student).
            conn.execute("UPDATE schema_migrations SET applied_at=? WHERE name=?",
                         ((datetime.now(timezone.utc) - timedelta(days=1)).isoformat(), outreach_inbox.MIGRATION))
            # Recorded two hours after it went, as when the laptop was closed at send time.
            sent = datetime.now(timezone.utc) - timedelta(hours=2)
            conn.execute("UPDATE outreach_events SET created_at=?, detail=? WHERE target_id=? AND event_type='gmail_sent'", (
                datetime.now(timezone.utc).isoformat(timespec="microseconds"),
                json.dumps({"kind": "initial", "to": "greg@bovi.example", "thread_id": "thread-1", "message_id": "sent-1",
                            "sent_ms": int(sent.timestamp() * 1000)}), target["id"]))
            conn.commit()
        self.arrive("early-2", mail("Great, let's talk."), received=now_ms(-timedelta(hours=1)))
        self.assertEqual(len(self.check()["replies"]), 1)

    def test_a_company_stays_watched_after_a_bounce(self):
        target = self.sent_target()
        with closing(connect_product(self.platform_path)) as conn:
            conn.execute("UPDATE outreach_targets SET status='drafted', sent_at=NULL WHERE id=?", (target["id"],))
            conn.commit()
            self.assertIn(target["id"], outreach_inbox.watched_ids(conn, USER))
        self.arrive("fwd-1", mail("Greg forwarded your note before he left. Let's talk.", sender="Bo Chen <bo@bovi.example>",
                                  subject="Internship"))
        self.assertEqual(len(self.check()["replies"]), 1)
        self.assertEqual(self.target(target)["status"], "replied")

    def test_an_older_reply_found_late_never_replaces_a_newer_ones_suggestion(self):
        target = self.sent_target()
        self.arrive("new-1", mail("Thanks, but we're not hiring interns this term."), received=now_ms(timedelta(hours=2)))
        self.check()
        self.assertEqual(self.target(target)["reply_suggestion"]["status"], "declined")
        # Found later in the thread: an earlier message asking for a call.
        self.arrive_in_thread("old-1", mail("Can we set up a call?", sender="Ana <ana@bovi.example>"),
                              received=now_ms(timedelta(hours=1)))
        self.assertEqual(len(self.check()["replies"]), 1)
        self.assertEqual(self.target(target)["reply_suggestion"]["status"], "declined", "the newest reply's reading stands")

    def test_a_reply_already_pasted_is_not_logged_twice(self):
        target = self.sent_target()
        self.client.post(f"/api/v1/outreach/{target['id']}/reply", headers=AUTH, json={"text": "Sure, let's talk Tuesday."})
        self.arrive("same-2", mail("Sure, let's talk Tuesday."))
        self.assertEqual(len(self.check()["replies"]), 1)
        self.assertEqual(self.replies(target), ["Sure, let's talk Tuesday."])

    def test_a_follow_up_keeps_an_old_company_watched(self):
        target = self.sent_target()
        with closing(connect_product(self.platform_path)) as conn:
            old = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat(timespec="microseconds")
            conn.execute("UPDATE outreach_events SET created_at=? WHERE target_id=? AND event_type='gmail_sent'", (old, target["id"]))
            conn.commit()
            self.assertNotIn(target["id"], outreach_inbox.watched_ids(conn, USER))
            conn.execute(
                "INSERT INTO outreach_events(id, target_id, user_id, event_type, detail, created_at) VALUES('ev-fu', ?, ?, 'gmail_sent', ?, ?)",
                (target["id"], USER, json.dumps({"kind": "follow_up", "to": "greg@bovi.example", "thread_id": "thread-9"}),
                 (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(timespec="microseconds")),
            )
            conn.commit()
            self.assertIn(target["id"], outreach_inbox.watched_ids(conn, USER), "the follow-up restarts the wait")

    def test_rows_the_old_rules_set_aside_are_read_again_and_shown(self):
        target = self.sent_target(contact_email="info@bovi.example")
        received = now_ms(timedelta(minutes=5))
        self.arrive("lead-2", mail("Let's set up a call this week.", sender="Dana Lee <dana@bovi.example>", subject="Interview"),
                    received=received)
        with closing(connect_product(self.platform_path)) as conn:
            # As the old rule left it, even written after the upgrade by a server still running the old code.
            conn.execute("INSERT INTO outreach_inbox_messages(user_id, gmail_id, target_id, kind, sender, received_at, recorded_at) "
                         "VALUES(?, 'lead-2', ?, 'ignored', 'dana@bovi.example', ?, ?)", (USER, target["id"], utc_now(), utc_now()))
            # And from before these rules started: the app had not counted it then.
            conn.execute("UPDATE schema_migrations SET applied_at=? WHERE name=?",
                         ((datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(), outreach_inbox.MIGRATION))
            conn.commit()
        result = self.check()
        self.assertEqual(result["replies"], [], "never counted without the student")
        self.assertEqual([(item["from"], item["reason"]) for item in result["possible"]], [("dana@bovi.example", "found_late")])
        self.assertEqual(self.inbox_row("lead-2")["rules"], outreach_inbox.RULES)
        self.assertIn(("1 earlier email may be a reply", "The app had not counted them. Open Outreach to check each one."),
                      self.notices())
        self.assertNotIn(("Bovi may have replied", "Open Outreach to check it."), self.notices(), "one notice for the lot")

    def test_a_pasted_reply_settles_the_possible_reply_it_matches(self):
        target = self.sent_target()
        self.arrive("careers-3", mail("Could you send over your availability?", sender="Bovi Careers <careers@bovi.example>",
                                      subject="Next steps"))
        self.check()
        logged = self.client.post(f"/api/v1/outreach/{target['id']}/reply", headers=AUTH,
                                  json={"text": "Could you send over your availability?"})
        self.assertEqual(logged.status_code, 200, logged.text)
        self.assertEqual(logged.json()["target"]["possible_replies"], [])
        self.assertEqual(self.inbox_row("careers-3")["kind"], "reply")

    def test_the_job_mail_reader_takes_back_what_outreach_no_longer_holds(self):
        from opportunity_app import application_inbox

        target = self.sent_target()
        with closing(connect_product(self.platform_path)) as conn:
            now = utc_now()
            for gmail_id, kind, via, rules in (("r-1", "reply", "address", 2), ("d-1", "reply", "domain", 2), ("p-1", "possible", "domain", 2),
                                               ("i-1", "ignored", "domain", 2), ("old-1", "ignored", "", 0), ("x-1", "dismissed", "domain", 2)):
                conn.execute("INSERT INTO outreach_inbox_messages(user_id, gmail_id, target_id, kind, sender, received_at, recorded_at, via, rules, reason) "
                             "VALUES(?, ?, ?, ?, 'a@bovi.example', ?, ?, ?, ?, 'x')", (USER, gmail_id, target["id"], kind, now, now, via, rules))
                conn.execute("INSERT INTO application_mail_messages(user_id, gmail_id, state, recorded_at, received_at) VALUES(?, ?, 'outreach', ?, ?)",
                             (USER, gmail_id, now, now))
            conn.execute("INSERT INTO application_mail_sync(user_id, history_id, pending_ids_json, enabled_at, updated_at) VALUES(?, 'h-1', '[]', 'e-1', ?)",
                         (USER, now))
            conn.execute("INSERT INTO user_settings(user_id, key, value, updated_at) VALUES(?, ?, 'on', ?) "
                         "ON CONFLICT(user_id, key) DO UPDATE SET value='on'", (USER, application_inbox.FEATURE, now))
            conn.commit()
            owns = {gmail_id: application_inbox._outreach_owns(conn, USER, gmail_id) for gmail_id in ("r-1", "d-1", "p-1", "i-1", "old-1", "x-1")}
            self.assertEqual(owns, {"r-1": True, "d-1": False, "p-1": True, "i-1": False, "old-1": False, "x-1": False})
            self.assertEqual(application_inbox._reclaim(conn, USER, expect="e-1"), 3, "not the row older rules left, until it is read again")
            states = {row[0]: (row[1], row[2]) for row in conn.execute("SELECT gmail_id, state, origin FROM application_mail_messages")}
            self.assertEqual({gmail_id for gmail_id, (state, _origin) in states.items() if state == "awaiting_resume"}, {"d-1", "i-1", "x-1"},
                             "decided again, as mail read while paused is")
            self.assertEqual({states[gmail_id][1] for gmail_id in ("d-1", "i-1", "x-1")}, {application_inbox.RECLAIMED},
                             "read late: only ever proposed")
            self.assertEqual({gmail_id for gmail_id, (state, _origin) in states.items() if state == "outreach"}, {"old-1", "p-1", "r-1"})
            self.assertEqual(application_inbox._reclaim(conn, USER, expect="e-1"), 0, "taken back once")

    # --- Review regressions -------------------------------------------------------------

    def test_a_comma_in_the_senders_name_does_not_lose_the_reply(self):
        target = self.sent_target()
        self.arrive("comma-1", mail("Yes, let's talk.", sender="Lee, Greg <greg@bovi.example>", subject="Talk?"))
        self.assertEqual([item["from"] for item in self.check()["replies"]], ["greg@bovi.example"])
        self.assertEqual(self.target(target)["status"], "replied")

    def test_a_person_writing_like_a_receipt_is_still_a_reply(self):
        target = self.sent_target()
        self.arrive("copy-1", mail("I've shared a copy of your message with our CTO. Are you free Tuesday?", subject="Your note"))
        self.assertEqual(len(self.check()["replies"]), 1)
        self.assertEqual(self.target(target)["status"], "replied")

    def test_a_receipt_from_the_company_in_the_thread_is_not_a_reply(self):
        target = self.sent_target(contact_email="careers@bovi.example")
        self.arrive_in_thread("desk-2", mail("This is an automated message. We received your message and will reply soon.",
                                             sender="Bovi <noreply@bovi.example>", headers="Auto-Submitted: auto-generated\n"))
        result = self.check()
        self.assertEqual((result["replies"], result["possible"], len(result["automatic"])), ([], [], 1))
        self.assertEqual(self.target(target)["status"], "sent")

    def test_account_mail_from_a_companys_no_reply_is_set_aside(self):
        target = self.sent_target()
        self.arrive("code-1", mail("Your verification code is 482913.", sender="Bovi <no-reply@accounts.bovi.example>",
                                   subject="Security alert"))
        self.assertEqual((self.check()["possible"], self.target(target)["possible_reply_count"]), ([], 0))
        self.assertEqual(self.inbox_row("code-1")["reason"], "automated_sender")

    def test_rows_older_rules_took_as_automatic_are_read_again(self):
        target = self.sent_target()
        self.arrive("tool-2", mail("Hi Sam, happy to chat Tuesday.", subject="Chat?", headers="Precedence: bulk\n"))
        with closing(connect_product(self.platform_path)) as conn:
            conn.execute("INSERT INTO outreach_inbox_messages(user_id, gmail_id, target_id, kind, sender, received_at, recorded_at) "
                         "VALUES(?, 'tool-2', ?, 'automatic', 'greg@bovi.example', ?, ?)", (USER, target["id"], utc_now(), utc_now()))
            conn.commit()
        result = self.check()
        self.assertEqual([(item["from"], item["reason"]) for item in result["possible"]], [("greg@bovi.example", "mailing_tool")])
        self.assertEqual(self.inbox_row("tool-2")["rules"], outreach_inbox.RULES)

    def test_a_recruiter_writing_through_an_applicant_system_is_shown(self):
        # Gmail cannot search by Reply-To: the search for mail naming the company finds it.
        target = self.sent_target(company="Bovi Robotics")
        self.arrive("relay-1", mail("Bovi Robotics would like to set up an interview.", sender="Dana at Bovi <no-reply@relay.example>",
                                    subject="Interview", headers="Reply-To: dana@bovi.example\n"))
        result = self.check()
        self.assertEqual([(item["target_id"], item["reason"]) for item in result["possible"]], [(target["id"], "reply_to")])

    def test_bulk_mail_from_the_shared_inbox_written_to_is_shown(self):
        target = self.sent_target(contact_email="careers@bovi.example")
        self.arrive("group-2", mail("Interview at Bovi: pick a time.", sender="Bovi Careers <careers@bovi.example>",
                                    subject="Interview", headers="List-Id: <careers.bovi.example>\n"))
        self.assertEqual([item["reason"] for item in self.check()["possible"]], ["mailing_tool"])
        self.assertEqual(self.target(target)["possible_reply_count"], 1)

    def test_a_platforms_own_site_is_its_companys_domain(self):
        self.sent_target(company="Rippling", contact_email="jane@rippling.com", website="https://www.rippling.com")
        self.check()
        query = [q for q in self.gmail.searches if q.startswith("from:(") and "mailer" not in q][0]
        self.assertIn("from:(jane@rippling.com OR rippling.com)", query)

    def test_a_professors_other_mailbox_and_lab_are_matched(self):
        target = self.sent_target(company="Kim Lab", contact_email="jkim@cs.stateu.edu", website="")
        self.arrive("prof-1", mail("Happy to talk about the position.", sender="Jin Kim <jkim@stateu.edu>", subject="Position"))
        self.arrive("lab-1", mail("Prof. Kim asked me to set up a time.", sender="Ann Park <apark@cs.stateu.edu>", subject="Time"))
        result = self.check()
        self.assertEqual([item["from"] for item in result["replies"]], ["jkim@stateu.edu"], "the same person at another host")
        self.assertEqual([(item["from"], item["reason"]) for item in result["possible"]], [("apark@cs.stateu.edu", "weak_domain")],
                         "someone else in the lab: shown, never counted")
        self.assertEqual(self.target(target)["status"], "replied")

    def test_a_first_email_sent_by_hand_still_counts_after_an_app_follow_up(self):
        target = self.sent_target()
        with closing(connect_product(self.platform_path)) as conn:
            earlier = (datetime.now(timezone.utc) - timedelta(days=20)).date().isoformat()
            conn.execute("UPDATE outreach_targets SET sent_at=? WHERE id=?", (earlier, target["id"]))
            conn.commit()
        self.arrive("mid-1", mail("Greg asked me to follow up. Let's talk.", sender="Priya Nair <priya@bovi.example>",
                                  subject="Your note"), received=now_ms(-timedelta(days=10)))
        with closing(connect_product(self.platform_path)) as conn:
            conn.execute("UPDATE schema_migrations SET applied_at=? WHERE name=?",
                         ((datetime.now(timezone.utc) - timedelta(days=30)).isoformat(), outreach_inbox.MIGRATION))
            conn.commit()
        self.assertEqual([item["from"] for item in self.check()["replies"]], ["priya@bovi.example"])

    def test_a_message_whose_headers_cannot_be_read_never_stops_the_check(self):
        target = self.sent_target()
        self.arrive_in_thread("broken-1", mail("Talk soon.", sender="Ana <ana@bovi.example>"))
        self.arrive("reply-9", mail("Yes, let's talk."))
        with mock.patch.object(mail_message, "sender", side_effect=[IndexError("bad header"), ("Greg Lee", "greg@bovi.example")]):
            result = self.check()
        self.assertEqual(result["state"], "ok")
        self.assertEqual([item["reason"] for item in result["possible"]] + [item["from"] for item in result["replies"]],
                         ["unreadable", "greg@bovi.example"])
        self.assertEqual(self.target(target)["status"], "replied")

    def test_the_first_sweep_under_these_rules_reaches_back_to_the_oldest_thread(self):
        target = self.sent_target(contact_email="info@bovi.example")
        with closing(connect_product(self.platform_path)) as conn:
            # The old code's last good check was a minute ago; an answer from outside the company came days before.
            conn.execute("INSERT INTO automation_health(user_id, component, last_ok_at, last_error, detail_json, updated_at) "
                         "VALUES(?, 'inbox.replies', ?, '', '{}', ?) ON CONFLICT(user_id, component) DO UPDATE SET last_ok_at=excluded.last_ok_at",
                         (USER, utc_now(), utc_now()))
            old = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat(timespec="microseconds")
            conn.execute("UPDATE outreach_events SET created_at=? WHERE target_id=? AND event_type='gmail_sent'", (old, target["id"]))
            conn.commit()
        self.arrive_in_thread("dana-9", mail("Saw your note. Free Thursday?", sender="Dana <dana.bovi@gmail.com>"),
                              received=now_ms(-timedelta(days=4)))
        self.check()
        sweep = [q for q in self.gmail.searches if q.startswith("-in:sent")][0]
        self.assertLessEqual(int(sweep.rsplit("after:", 1)[1]), int((datetime.now(timezone.utc) - timedelta(days=4)).timestamp()))
        self.assertEqual(self.inbox_row("dana-9")["reason"], "thread_outsider", "read, days back")

    def test_shared_and_company_named_inboxes_are_never_a_person(self):
        target = self.sent_target(company="Bovi Robotics")
        self.arrive("rec-1", mail("Join our info session Thursday!", sender="Bovi Recruitment <recruitment@bovi.example>", subject="Info session"))
        self.arrive("own-1", mail("Welcome to Bovi Cloud.", sender="Bovi <bovi@bovi.example>", subject="Welcome"))
        result = self.check()
        self.assertEqual(result["replies"], [])
        self.assertEqual({item["gmail_id"]: item["reason"] for item in self.target(target)["possible_replies"]},
                         {"rec-1": "shared_address", "own-1": "shared_address"})

    def test_an_automatic_reply_in_another_language_in_the_thread_is_not_a_reply(self):
        target = self.sent_target()
        self.arrive_in_thread("ooo-2", mail("Ich bin bis 6. Oktober nicht im Buero.", subject="Automatische Antwort: Robotics internship question",
                                            headers="Auto-Submitted: auto-generated\n"))
        self.arrive_in_thread("desk-3", mail("Thanks, we will look into it.", sender="Greg Lee <greg@bovi.example>",
                                             subject="Re: Robotics internship question", headers="Auto-Submitted: auto-generated\n"),
                              received=now_ms(timedelta(minutes=7)))
        result = self.check()
        self.assertEqual((result["replies"], len(result["automatic"])), ([], 1))
        self.assertEqual([item["reason"] for item in result["possible"]], ["auto_generated"], "a system sent it: shown, not counted")
        self.assertEqual(self.target(target)["status"], "sent")

    def test_a_website_on_a_code_host_stands_for_nobody_there(self):
        self.sent_target(company="Bovi Robotics", contact_email="greg@bovi.example", website="https://github.com/bovi-robotics")
        self.check()
        query = [q for q in self.gmail.searches if q.startswith("from:(") and "mailer" not in q][0]
        self.assertNotIn("github.com", query)

    def test_someone_the_student_copied_is_not_the_company(self):
        target = self.sent_target(contact_cc="dana@othercorp.example")
        self.arrive_in_thread("cc-1", mail("Greg, Sam is great, hope you connect!", sender="Dana <dana@othercorp.example>"))
        self.arrive("cc-2", mail("Here is the reading list.", sender="Dana <dana@othercorp.example>", subject="Reading"),
                    received=now_ms(timedelta(minutes=9)))
        result = self.check()
        self.assertEqual(result["replies"], [])
        self.assertEqual({item["gmail_id"]: item["reason"] for item in self.target(target)["possible_replies"]},
                         {"cc-1": "thread_outsider", "cc-2": "copied_outsider"})

    def test_an_applicant_system_writing_as_the_recruiter_written_to_is_job_mail(self):
        target = self.sent_target()
        self.arrive("ats-2", mail("Unfortunately we will not move forward. https://boards.greenhouse.io/bovi/jobs/1",
                                  subject="Your application", headers="Return-Path: <bounce@us.greenhouse-mail.io>\n"))
        result = self.check()
        self.assertEqual(result["replies"], [])
        self.assertEqual([item["reason"] for item in result["possible"]], ["job_mail"])
        self.assertEqual(self.target(target)["status"], "sent")

    def test_a_sales_sequence_from_a_rep_is_only_a_possible_reply(self):
        target = self.sent_target()
        self.arrive("seq-1", mail("Hi Sam, saw you reached out. Evaluating arms for your lab? https://t.hubspotlinks.com/x",
                                  sender="Mike Chen <mike.chen@bovi.example>", subject="Quick question", headers="X-HubSpot-Sequence-Id: 7\n"))
        self.assertEqual([item["reason"] for item in self.check()["possible"]], ["mailing_tool"])
        self.assertEqual(self.target(target)["status"], "sent")

    def test_a_contact_at_regional_free_mail_stands_for_nobody_else_there(self):
        self.sent_target(company="Tiny Co", contact_email="owner@yahoo.co.uk", website="")
        self.check()
        query = [q for q in self.gmail.searches if q.startswith("from:(") and "mailer" not in q][0]
        self.assertIn("from:(owner@yahoo.co.uk)", query)

    def test_a_blind_copied_blast_from_the_address_written_to_is_not_a_reply(self):
        target = self.sent_target()
        self.arrive("bcc-1", mail("All of our summer positions are now filled.", subject="Positions", to="undisclosed-recipients:;"))
        self.assertEqual([item["reason"] for item in self.check()["possible"]], ["not_addressed"])
        self.assertEqual(self.target(target)["status"], "sent")

    def test_an_earlier_message_in_a_thread_the_email_joined_is_not_its_reply(self):
        target = self.sent_target()
        self.arrive_in_thread("old-9", mail("Earlier note from Greg.", subject="Re: Robotics internship question"),
                              received=now_ms(-timedelta(hours=2)))
        self.assertEqual((self.check()["replies"], self.target(target)["status"]), ([], "sent"))
        self.assertEqual(self.inbox_row("old-9")["reason"], "before")

    def test_a_one_word_name_must_be_written_as_the_company_writes_it(self):
        target = self.sent_target(company="Linear", contact_email="greg@linear.example", website="https://linear.example")
        self.arrive("ta-1", mail("The linear algebra midterm moved to Friday.", sender="TA <ta.person@gmail.com>", subject="Midterm"))
        self.arrive("fd-1", mail("I run product at Linear and saw your email. Chat?", sender="Pat <pat.home@gmail.com>", subject="Hi"))
        result = self.check()
        self.assertEqual([(item["from"], item["reason"]) for item in result["possible"]], [("pat.home@gmail.com", "mentions_company")])
        self.assertEqual(self.target(target)["possible_reply_count"], 1)

    def test_an_email_from_the_company_with_a_header_python_cannot_parse_is_still_read(self):
        target = self.sent_target()
        self.arrive("bad-1", mail("Yes, let's talk.", subject="Talk?", headers='Reply-To: "Greg Lee" <greg@bovi.example>, "\n'))
        result = self.check()
        self.assertEqual(result["state"], "ok")
        # The Reply-To the parser fails on is read from its raw text; the rest of the email is judged as usual.
        self.assertEqual([item["target_id"] for item in result["replies"]], [target["id"]],
                         "never set aside: it is from the address written to")
        self.assertEqual(self.inbox_row("bad-1")["reason"], "written_to")

    def test_an_email_whose_from_python_cannot_parse_is_shown_in_its_thread(self):
        target = self.sent_target()
        self.arrive_in_thread("bad-2", mail("Yes, let's talk.", subject="Re: Hello", sender='"Greg, Lee" <greg@bovi.example>, :;'))
        result = self.check()
        self.assertEqual(result["state"], "ok")
        self.assertEqual([item["target_id"] for item in result["replies"] + result["possible"]], [target["id"]],
                         "never set aside: it is in the student's thread")

    def test_the_background_watcher_captures_replies(self):
        target = self.sent_target()
        self.arrive("reply-1", mail("Sure, let's talk."))
        outreach_inbox._LAST_CAPTURE.clear()
        watcher = InboxWatcher(self.platform_path, client_factory=self.factory, decisions_for=lambda conn, user_id: None)
        watcher.run_once()
        self.assertEqual(self.target(target)["status"], "replied")
        health = self.health()
        for component in ("inbox.sends", "inbox.deliveries", "inbox.replies", "inbox.connection"):
            self.assertIsNotNone(health[component]["last_ok_at"], component)
            self.assertEqual(health[component]["last_error"], "", component)

    # --- The background watcher, one step at a time ----------------------------------

    def watcher(self):
        return InboxWatcher(self.platform_path, client_factory=self.factory, decisions_for=lambda conn, user_id: None)

    def health(self):
        with closing(connect_product(self.platform_path)) as conn:
            return {row["component"]: dict(row) for row in conn.execute(
                "SELECT * FROM automation_health WHERE user_id=?", (USER,),
            ).fetchall()}

    def test_a_step_that_raises_does_not_stop_the_others(self):
        deliveries = mock.Mock(return_value={"state": "ok", "checked": 0, "bounced": []})
        replies = mock.Mock(return_value={"state": "unreachable", "replies": [], "automatic": []})
        with mock.patch("opportunity_app.outreach_gmail_sends.capture_gmail_sends",
                        side_effect=RuntimeError("could not read the draft to greg@bovi.example")),                 mock.patch.object(inbox_watcher, "check_deliveries", deliveries),                 mock.patch.object(inbox_watcher, "capture_replies", replies):
            self.watcher().run_once()
        deliveries.assert_called_once()
        replies.assert_called_once()
        health = self.health()
        self.assertEqual(health["inbox.sends"]["last_error"], "RuntimeError: could not read the draft to [address]",
                         "the error names the failure, never an address")
        self.assertIsNone(health["inbox.sends"]["last_ok_at"])
        self.assertIsNotNone(health["inbox.deliveries"]["last_ok_at"])
        self.assertEqual(health["inbox.deliveries"]["last_error"], "")
        self.assertEqual(health["inbox.replies"]["last_error"], "Gmail could not be reached")
        self.assertIsNotNone(health["inbox.connection"]["last_ok_at"])

    def test_a_broken_connection_gets_a_notice_and_gmail_is_not_asked(self):
        self.sent_target()
        asked = len(self.gmail.requests)
        with closing(connect_product(self.platform_path)) as conn:
            conn.execute("UPDATE connector_accounts SET status='error', updated_at='2026-09-27T10:00:00+00:00'")
            conn.commit()
        self.watcher().run_once()
        first = self.health()["inbox.connection"]
        self.watcher().run_once()
        self.assertEqual(len(self.gmail.requests), asked, "nothing is asked of a connection that needs reconnecting")
        health = self.health()
        self.assertTrue(health["inbox.connection"]["last_error"].startswith("Gmail needs reconnecting (since "),
                        health["inbox.connection"]["last_error"])
        self.assertEqual(json.loads(health["inbox.connection"]["detail_json"]),
                         {"state": "error", "since": "2026-09-27T10:00:00+00:00"})
        self.assertEqual(health["inbox.connection"]["last_error_at"], first["last_error_at"],
                         "a later pass does not move when it broke")
        self.assertNotIn("inbox.replies", health)
        with closing(connect_product(self.platform_path)) as conn:
            notices = automation.list_notices(conn, USER)
        self.assertEqual([(n["event_key"], n["level"]) for n in notices], [("gmail-expired:2026-09-27T10:00:00+00:00", "problem")],
                         "one notice, however many passes")

    def test_a_break_keeps_its_earliest_since_until_the_connection_works_again(self):
        with closing(connect_product(self.platform_path)) as conn:
            conn.execute("UPDATE connector_accounts SET status='error', updated_at='2026-09-27T10:00:00+00:00'")
            conn.commit()
        self.watcher().run_once()
        with closing(connect_product(self.platform_path)) as conn:
            # Something touched the row later while it stayed broken.
            conn.execute("UPDATE connector_accounts SET updated_at='2026-09-27T11:30:00+00:00'")
            conn.commit()
        self.watcher().run_once()
        self.assertEqual(json.loads(self.health()["inbox.connection"]["detail_json"])["since"], "2026-09-27T10:00:00+00:00")
        # Reconnected, then broken again: a new break has its own since.
        with closing(connect_product(self.platform_path)) as conn:
            conn.execute("UPDATE connector_accounts SET status='connected'")
            conn.commit()
        self.watcher().run_once()
        connected = self.health()["inbox.connection"]
        self.assertEqual(json.loads(connected["detail_json"]), {"state": "connected"})
        with closing(connect_product(self.platform_path)) as conn:
            conn.execute("UPDATE connector_accounts SET status='error', updated_at='2026-09-28T09:00:00+00:00'")
            conn.commit()
        self.watcher().run_once()
        self.assertEqual(json.loads(self.health()["inbox.connection"]["detail_json"]),
                         {"state": "error", "since": "2026-09-28T09:00:00+00:00"})

    def test_a_connection_the_student_disconnected_is_not_an_error(self):
        self.sent_target()
        with closing(connect_product(self.platform_path)) as conn:
            disconnected = conn.execute("SELECT id FROM connector_accounts WHERE provider='gmail_drafts'").fetchone()[0]
        self.assertEqual(self.client.delete(f"/api/v1/connections/{disconnected}", headers=AUTH).status_code, 200)
        asked = len(self.gmail.requests)
        self.watcher().run_once()
        first = self.health()["inbox.connection"]
        self.watcher().run_once()
        self.assertEqual(len(self.gmail.requests), asked, "nothing is asked of Gmail")
        row = self.health()["inbox.connection"]
        self.assertEqual((row["last_error"], row["last_error_at"]), ("", None), "never 'needs reconnecting'")
        self.assertIsNotNone(row["last_ok_at"])
        self.assertEqual(json.loads(row["detail_json"]), {"state": "disconnected"})
        self.assertEqual(row["updated_at"], first["updated_at"], "recorded once, not on every pass")
        with closing(connect_product(self.platform_path)) as conn:
            self.assertEqual(automation.list_notices(conn, USER), [], "and no notice")

    def test_on_postgresql_the_watcher_saves_gmail_health_between_its_steps(self):
        # On PostgreSQL every connection is "in a transaction" after its first read, so a
        # rate limit seen inside a step cannot be written there; the watcher saves it after.
        target = self.sent_target()
        self.arrive("reply-1", mail("Sure, let's talk."))
        with closing(connect_product(self.platform_path)) as conn:
            conn.execute("UPDATE connector_accounts SET backoff_until=NULL, last_error='', last_ok_at=NULL")
            conn.commit()
        gmail_connection._BACKOFF.clear()
        gmail_connection._HEALTH.clear()
        self.gmail.read_response = rate_limited
        outreach_inbox._LAST_CAPTURE.clear()
        real_connect = inbox_watcher.connect_product
        with mock.patch.object(inbox_watcher, "connect_product", lambda target: AlwaysInTransaction(real_connect(target))):
            self.watcher().run_once()
        with closing(connect_product(self.platform_path)) as conn:
            row = dict(conn.execute("SELECT * FROM connector_accounts WHERE provider='gmail_drafts'").fetchone())
            self.assertTrue(row["backoff_until"], "the hold survives a restart")
            self.assertEqual(row["last_error"], "Gmail asked the app to slow down (HTTP 403)")
            self.assertEqual(automation_health.gmail_health(conn, USER)["state"], "throttled")
        # Gmail answers again: the next pass records it, and the reply is captured.
        gmail_connection._BACKOFF.clear()
        with closing(connect_product(self.platform_path)) as conn:
            conn.execute("UPDATE connector_accounts SET backoff_until=NULL")
            conn.commit()
        self.gmail.read_response = None
        outreach_inbox._LAST_CAPTURE.clear()
        with mock.patch.object(inbox_watcher, "connect_product", lambda target: AlwaysInTransaction(real_connect(target))):
            self.watcher().run_once()
        with closing(connect_product(self.platform_path)) as conn:
            row = dict(conn.execute("SELECT * FROM connector_accounts WHERE provider='gmail_drafts'").fetchone())
        self.assertIsNotNone(row["last_ok_at"])
        self.assertEqual((row["last_error"], row["backoff_until"]), ("", None))
        self.assertEqual(self.target(target)["status"], "replied")

    def test_a_gmail_slowdown_is_recorded_as_one_and_never_as_a_reconnect(self):
        target = self.sent_target()
        self.arrive("reply-1", mail("Sure, let's talk."))
        self.gmail.read_response = rate_limited
        outreach_inbox._LAST_CAPTURE.clear()
        self.watcher().run_once()
        health = self.health()
        self.assertEqual(health["inbox.deliveries"]["last_error"], "Gmail asked the app to slow down")
        self.assertEqual(health["inbox.replies"]["last_error"], "Gmail asked the app to slow down")
        self.assertIsNotNone(health["inbox.connection"]["last_ok_at"])
        self.assertEqual(self.target(target)["status"], "sent", "the reply waits for the next look")
        with closing(connect_product(self.platform_path)) as conn:
            self.assertEqual(conn.execute("SELECT status FROM connector_accounts").fetchone()[0], "connected")
            self.assertEqual(automation.list_notices(conn, USER), [])


if __name__ == "__main__":
    unittest.main()
