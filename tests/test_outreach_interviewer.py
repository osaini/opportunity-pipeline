"""Who the call is with, found in the mailbox, and notes from their LinkedIn read through a checked account."""

import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from opportunity_app import outreach_linkedin, quote_check
from opportunity_app.outreach_config import LINKEDIN_ENV
from opportunity_app.outreach_interviewer import NOTES_INSTRUCTIONS
from opportunity_app.outreach import create_target, get_target, log_reply, update_target
from opportunity_app.outreach_interviewer import _meeting, confirm_profile, find_interviewer, interviewer_due, pick_profile, read_interviewer
from opportunity_app.outreach_linkedin import LinkedInClient, LinkedInUnavailable, config_problem, username_from
from opportunity_app.schema import ensure_product_schema
from opportunity_app.database import connect_product
from opportunity_app.timestamps import utc_now

from helpers_platform import build_and_migrate

USER = "local-user"
GOOD_CONFIG = "Transport: stdio (uvx mcp-server-linkedin@latest --no-auto-import)\n  Env:\n    AUTO_IMPORT_FROM_BROWSER=false"
PROFILE_TEXT = (
    "Dana Ortiz\nHead of Controls at Chargebot\nAustin, Texas\n"
    "Experience\nHead of Controls\nChargebot\n2024 - Present\nLeads the controls team building the charging arm.\n"
    "Robotics Engineer\nTesla\n2019 - 2024\nBuilt motion planning for factory robots on the Model Y line.\n"
    "Education\nUniversity of Michigan\nMS, Robotics\n"
)


class FakeLinkedIn:
    """Answers the read calls the way mcp-server-linkedin does, and records them."""

    def __init__(self, *, me="test-student-123", people=None, profile=PROFILE_TEXT, sections=None):
        self.me, self.people, self.profile_text, self.sections = me, people, profile, sections
        self.calls = []

    def __call__(self, tool, arguments):
        self.calls.append((tool, arguments))
        if tool == "get_my_profile":
            return {"url": f"https://www.linkedin.com/in/{self.me}/", "sections": {"main_profile": "..."}}
        if tool == "search_people":
            people = self.people if self.people is not None else [("Dana Ortiz", "danaortiz", "Head of Controls at Chargebot")]
            text = "\n\n".join(f"{name}\n • 2nd\n\n{line}\n\nConnect" for name, _, line in people)
            return {"sections": {"search_results": text},
                    "references": {"search_results": [{"kind": "person", "url": f"/in/{user}/", "text": name} for name, user, _ in people]}}
        if tool == "get_person_profile":
            return {"url": f"https://www.linkedin.com/in/{arguments['linkedin_username']}/", "sections": self.sections or {"main_profile": self.profile_text}}
        raise AssertionError(f"unexpected tool {tool}")


def notes_reply(*notes):
    return json.dumps({"notes": list(notes)})


def judged(reply, seen=None):
    """A writer that writes ``reply`` for the notes and answers every second-read item as supported."""
    def model(instructions, content):
        if instructions == NOTES_INSTRUCTIONS:
            if seen is not None:
                seen.append(content)
            return reply
        return json.dumps({"verdicts": [{"id": item["id"], "supported": True, "why": ""} for item in json.loads(content)["items"]]})
    return model


class LinkedInGuardTests(unittest.TestCase):
    def client(self, fake, config=GOOD_CONFIG):
        return LinkedInClient(call=fake, config=lambda: config, sleep=lambda _seconds: None)

    def test_nothing_is_read_without_an_account_set(self):
        fake = FakeLinkedIn()
        with mock.patch.dict("os.environ", {LINKEDIN_ENV: ""}):
            with self.assertRaisesRegex(LinkedInUnavailable, "LinkedIn is off"):
                self.client(fake).profile("danaortiz")
        self.assertEqual(fake.calls, [])

    def test_nothing_is_read_when_signed_in_as_another_account(self):
        fake = FakeLinkedIn(me="students-real-account")
        with mock.patch.dict("os.environ", {LINKEDIN_ENV: "https://www.linkedin.com/in/test-student-123/"}):
            with self.assertRaisesRegex(LinkedInUnavailable, "signed in as students-real-account, not test-student-123"):
                self.client(fake).profile("danaortiz")
        self.assertEqual([tool for tool, _ in fake.calls], ["get_my_profile"])

    def test_nothing_is_read_when_the_server_could_import_the_browsers_sign_in(self):
        fake = FakeLinkedIn()
        with mock.patch.dict("os.environ", {LINKEDIN_ENV: "test-student-123"}):
            for config in ("Transport: stdio (uvx mcp-server-linkedin@latest)", GOOD_CONFIG + " --import-from-browser"):
                with self.assertRaisesRegex(LinkedInUnavailable, "Nothing was read from LinkedIn"):
                    self.client(fake, config).profile("danaortiz")
        self.assertEqual(fake.calls, [], "not even the account check runs")
        self.assertEqual(config_problem(GOOD_CONFIG), "")

    def test_only_reading_tools_are_ever_called(self):
        client = self.client(FakeLinkedIn())
        with self.assertRaisesRegex(LinkedInUnavailable, "only reads"):
            client._paced("send_message", {})
        with self.assertRaises(LinkedInUnavailable):
            client._paced("connect_with_person", {})

    def test_calls_are_spaced_apart(self):
        waits = []
        clock = iter([100.0, 101.0, 102.0, 103.0, 104.0, 105.0])
        client = LinkedInClient(call=FakeLinkedIn(), config=lambda: GOOD_CONFIG, sleep=waits.append, clock=lambda: next(clock), min_gap=20)
        with mock.patch.object(outreach_linkedin, "_last_call", [0.0]), \
                mock.patch.dict("os.environ", {LINKEDIN_ENV: "test-student-123"}):
            client.profile("danaortiz")
        self.assertTrue(waits and all(wait > 0 for wait in waits), "the profile read waited after the account check")

    def test_usernames_from_links(self):
        self.assertEqual(username_from("https://www.linkedin.com/in/Dana-Ortiz-1/?trk=x"), "dana-ortiz-1")
        self.assertEqual(username_from("linkedin.com/in/danaortiz"), "danaortiz")
        self.assertEqual(username_from("danaortiz"), "danaortiz")
        self.assertEqual(username_from("https://example.com/about"), "")

    def test_a_search_result_is_used_only_when_it_is_the_one_person_at_the_company(self):
        people = [
            {"username": "danaortiz", "name": "Dana Ortiz", "text": "Dana Ortiz\nHead of Controls at Chargebot\n\nDana Ortiz\nNurse at City Hospital"},
            {"username": "dana-ortiz-rn", "name": "Dana Ortiz", "text": "Dana Ortiz\nHead of Controls at Chargebot\n\nDana Ortiz\nNurse at City Hospital"},
        ]
        self.assertEqual(pick_profile(people[:1], "Dana Ortiz", "Chargebot, Inc."), ("danaortiz", []))
        username, candidates = pick_profile([{**people[0], "text": "Dana Ortiz\nNurse at City Hospital"}], "Dana Ortiz", "Chargebot")
        self.assertEqual(username, "", "the only result does not name the company")
        self.assertEqual([person["username"] for person in candidates], ["danaortiz"])


    def test_a_search_result_stops_at_an_anonymous_result_after_it(self):
        nurse = [{"username": "dana-rn", "name": "Dana Ortiz",
                  "text": "Dana Ortiz\n• 3rd\nNurse at City Hospital\nDenver\n\nLinkedIn Member\nControls Engineer at Acme Robotics\n"}]
        username, candidates = pick_profile(nurse, "Dana Ortiz", "Acme Robotics")
        self.assertEqual(username, "", "the Acme line belongs to the result after hers")
        self.assertEqual([person["username"] for person in candidates], ["dana-rn"])

    def test_two_results_with_one_name_each_get_their_own_text(self):
        text = ("Dana Ortiz\n • 3rd\n\nNurse at City Hospital\n\nConnect\n\n"
                "Dana Ortiz\n • 2nd\n\nControls Engineer at Acme Robotics\n\nConnect")
        people = [{"username": "dana-rn", "name": "Dana Ortiz", "text": text}, {"username": "dana-acme", "name": "Dana Ortiz", "text": text}]
        self.assertEqual(pick_profile(people, "Dana Ortiz", "Acme Robotics"), ("dana-acme", []))
        lost = [{**person, "text": "Someone Else\nControls Engineer at Acme Robotics"} for person in people]
        self.assertEqual(pick_profile(lost, "Dana Ortiz", "Acme Robotics")[0], "", "results that cannot be lined up with the text are not picked from")

    def test_results_that_are_not_the_person_are_not_offered_as_candidates(self):
        people = [{"username": "x", "name": "Priya Patel", "text": "Priya Patel\nNurse"}]
        self.assertEqual(pick_profile(people, "Dana Ortiz", "Acme Robotics"), ("", []))

    def test_a_meeting_time_is_never_read_out_of_an_address(self):
        self.assertEqual(_meeting("Invitation: Intro chat (student@school.edu)"), "")
        self.assertEqual(_meeting("Invitation: Intro chat"), "")
        self.assertEqual(_meeting("Invitation: Intro chat @ Tue Sep 29, 2026 10am - 10:30am (CDT) (student@school.edu)"), "Tue Sep 29, 2026 10am - 10:30am (CDT)")
        self.assertEqual(_meeting("Updated invitation with note: Intro @ Wed Sep 30, 2026 9am"), "Wed Sep 30, 2026 9am")

    def test_a_cmd_shim_is_never_run_with_a_persons_name(self):
        with tempfile.TemporaryDirectory() as folder:
            shim = Path(folder) / "mcporter.cmd"
            shim.write_text("@echo ARGS: %*\r\n", encoding="utf-8")
            with mock.patch.dict("os.environ", {outreach_linkedin.MCPORTER_ENV: str(shim)}), \
                    mock.patch.object(outreach_linkedin.subprocess, "run") as run:
                with self.assertRaisesRegex(LinkedInUnavailable, "shim"):
                    outreach_linkedin._run(["call", "linkedin-scraper.search_people", "--args", json.dumps({"keywords": "Dana & echo INJECTED & rem Ortiz"})], 30)
            run.assert_not_called()

    def test_a_shim_that_names_its_cli_js_is_run_as_node_and_that_file(self):
        with tempfile.TemporaryDirectory() as folder:
            cli = Path(folder) / "global" / "node_modules" / "mcporter" / "dist" / "cli.js"
            cli.parent.mkdir(parents=True)
            cli.write_text("// cli", encoding="utf-8")
            (Path(folder) / "bin").mkdir()
            shim = Path(folder) / "bin" / "mcporter.cmd"
            shim.write_text('@ECHO off\r\nnode "%~dp0\\..\\global\\node_modules\\mcporter\\dist\\cli.js" %*\r\n', encoding="utf-8")
            # node is found on the path here, so the test does not depend on the machine having one.
            with mock.patch.dict("os.environ", {outreach_linkedin.MCPORTER_ENV: str(shim)}), \
                    mock.patch.object(outreach_linkedin.shutil, "which", return_value="C:/tools/node.exe"):
                command = outreach_linkedin.mcporter_command()
            self.assertEqual(Path(command[-1]).resolve(), cli.resolve())
            self.assertNotEqual(Path(command[0]).suffix.casefold(), ".cmd")

    def test_a_node_cmd_is_never_the_interpreter_for_mcporter(self):
        with tempfile.TemporaryDirectory() as folder:
            cli = Path(folder) / "node_modules" / "mcporter" / "dist" / "cli.js"
            cli.parent.mkdir(parents=True)
            cli.write_text("// cli", encoding="utf-8")
            plain = Path(folder) / "mcporter"
            plain.write_text("", encoding="utf-8")
            with mock.patch.dict("os.environ", {outreach_linkedin.MCPORTER_ENV: str(plain)}):
                for found in ("C:/fnm/node.cmd", "C:/fnm/node.bat", None):
                    with mock.patch.object(outreach_linkedin.shutil, "which", return_value=found), \
                            mock.patch.object(outreach_linkedin.subprocess, "run") as run:
                        with self.assertRaisesRegex(LinkedInUnavailable, outreach_linkedin.MCPORTER_ENV):
                            outreach_linkedin._run(["call", "x", "--args", json.dumps({"keywords": "Dana & echo INJECTED"})], 30)
                    run.assert_not_called()
                with mock.patch.object(outreach_linkedin.shutil, "which", return_value="C:/tools/node.exe"):
                    self.assertEqual(outreach_linkedin.mcporter_command()[0], "C:/tools/node.exe")

    def test_a_timeout_running_mcporter_is_a_plain_error(self):
        with mock.patch.object(outreach_linkedin, "mcporter_command", return_value=["node", "cli.js"]), \
                mock.patch.object(outreach_linkedin.subprocess, "run", side_effect=subprocess.TimeoutExpired(["node", "cli.js", "Dana"], 30)):
            with self.assertRaisesRegex(RuntimeError, "did not answer within 30 seconds") as caught:
                outreach_linkedin._run(["call"], 30)
        self.assertNotIn("Dana", str(caught.exception))

    def test_a_name_searched_for_carries_no_command_characters(self):
        fake = FakeLinkedIn()
        with mock.patch.dict("os.environ", {LINKEDIN_ENV: "test-student-123"}):
            self.client(fake).search_people('Dana & echo INJECTED | rem ^Ortiz% "Acme" !')
        keywords = [arguments["keywords"] for tool, arguments in fake.calls if tool == "search_people"][0]
        self.assertFalse(outreach_linkedin.CMD_META.search(keywords), keywords)
        self.assertIn("Dana", keywords)


class InterviewerTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        _, self.platform_path = build_and_migrate(Path(self.tempdir.name))
        self.conn = connect_product(self.platform_path)
        ensure_product_schema(self.conn)
        self.target = create_target(self.conn, {
            "company": "Chargebot", "website": "https://chargebot.example", "contact_email": "hello@chargebot.example",
        }, user_id=USER)
        update_target(self.conn, self.target["id"], {"status": "call_scheduled"}, user_id=USER)
        patcher = mock.patch.dict("os.environ", {LINKEDIN_ENV: "test-student-123"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.conn.close()
        self.tempdir.cleanup()

    def inbox(self, gmail_id, sender, from_name, subject, received, kind="reply"):
        self.conn.execute(
            """INSERT INTO outreach_inbox_messages(user_id, gmail_id, target_id, kind, sender, received_at, recorded_at, from_name, subject)
               VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (USER, gmail_id, self.target["id"], kind, sender, received, utc_now(), from_name, subject),
        )
        self.conn.commit()

    def now(self):
        return get_target(self.conn, self.target["id"], user_id=USER)

    def test_whoever_sent_the_calendar_invitation_is_the_interviewer(self):
        self.inbox("1", "hello@chargebot.example", "Chargebot Team", "Re: Internship", "2026-09-28T18:00:00+00:00")
        self.inbox("2", "sam@chargebot.example", "Sam Lee", "Re: Internship", "2026-09-28T18:30:00+00:00")
        self.inbox("3", "dana@chargebot.example", "Dana Ortiz", "Invitation: Student X Chargebot @ Tue Sep 29, 2026 10am - 10:30am (CDT) (student@school.edu)",
                   "2026-09-28T18:10:00+00:00", kind="possible")
        self.inbox("4", "dana@elsewhere.example", "Dana Ortiz", "Invitation: Other", "2026-09-28T19:00:00+00:00")
        who = find_interviewer(self.conn, self.now(), USER)
        self.assertEqual((who["name"], who["email"], who["basis"]), ("Dana Ortiz", "dana@chargebot.example", "mailbox_invitation"),
                         "a possible reply counts; another domain and a shared inbox do not")
        self.assertEqual(who["meeting"], "Tue Sep 29, 2026 10am - 10:30am (CDT)")
        self.assertEqual([person["name"] for person in who["others"]], ["Sam Lee"])

    def test_without_an_invitation_the_latest_person_who_wrote(self):
        self.inbox("1", "sam@chargebot.example", "Sam Lee", "Re: Internship", "2026-09-28T18:30:00+00:00")
        self.inbox("2", "dana@chargebot.example", "Dana Ortiz", "Re: Internship", "2026-09-28T19:30:00+00:00")
        who = find_interviewer(self.conn, self.now(), USER)
        self.assertEqual((who["name"], who["basis"]), ("Dana Ortiz", "mailbox_sender"))

    def test_the_students_own_entry_wins(self):
        self.inbox("1", "sam@chargebot.example", "Sam Lee", "Invitation: call", "2026-09-28T18:30:00+00:00")
        update_target(self.conn, self.target["id"], {"interviewer_name": "Dana Ortiz"}, user_id=USER)
        self.assertEqual(find_interviewer(self.conn, self.now(), USER)["basis"], "student")

    def test_a_note_the_second_read_says_no_to_is_left_out(self):
        self.inbox("1", "dana@chargebot.example", "Dana Ortiz", "Invitation: call", "2026-09-28T18:30:00+00:00")
        merged = "Led the controls team at Tesla building the charging arm"
        notes = notes_reply(
            {"topic": "now", "text": "Leads the controls team building the charging arm", "quote": "Leads the controls team building the charging arm"},
            {"topic": "path", "text": merged, "quote": "Leads the controls team building the charging arm"},
        )
        shown = []

        def model(instructions, content):
            if instructions == NOTES_INSTRUCTIONS:
                return notes
            self.assertEqual(instructions, quote_check.JUDGE_INSTRUCTIONS)
            items = json.loads(content)["items"]
            shown.extend(items)
            return json.dumps({"verdicts": [{"id": item["id"], "supported": item["fact"] != merged, "why": "Tesla is an earlier job"} for item in items]})

        target = read_interviewer(self.conn, self.target["id"], user_id=USER,
                                  client=LinkedInClient(call=FakeLinkedIn(), config=lambda: GOOD_CONFIG, sleep=lambda _s: None), writer=model)
        self.assertEqual([note["text"] for note in target["interviewer"]["notes"]], ["Leads the controls team building the charging arm"])
        self.assertIn("a second read of the profile says it does not state this: Tesla is an earlier job",
                      [item["reason"] for item in target["interviewer"]["refused"]])
        self.assertIn("Tesla", shown[1]["passage"], "the second read sees the profile's own lines around the quote")
        self.assertEqual(shown[0]["about"], "Dana Ortiz")

    def test_notes_are_kept_only_when_the_profile_says_them(self):
        self.inbox("1", "dana@chargebot.example", "Dana Ortiz", "Invitation: call", "2026-09-28T18:30:00+00:00")
        fake = FakeLinkedIn()
        reply = notes_reply(
            {"topic": "path", "text": "Built motion planning for factory robots at Tesla on the Model Y line",
             "quote": "Built motion planning for factory robots on the Model Y line"},
            {"topic": "now", "text": "Leads the controls team building the charging arm", "quote": "Leads the controls team building the charging arm"},
            {"topic": "path", "text": "Spent 9 years at SpaceX", "quote": "Built motion planning for factory robots on the Model Y line"},
            {"topic": "education", "text": "PhD from Stanford", "quote": "a doctorate from Stanford University in robotics"},
        )
        prompts = []
        target = read_interviewer(
            self.conn, self.target["id"], user_id=USER,
            client=LinkedInClient(call=fake, config=lambda: GOOD_CONFIG, sleep=lambda _s: None),
            writer=judged(reply, prompts),
        )
        record = target["interviewer"]
        self.assertEqual(record["name"], "Dana Ortiz")
        self.assertTrue(record["linkedin"]["confirmed"], "the profile names Chargebot")
        self.assertEqual([note["text"] for note in record["notes"]], [
            "Built motion planning for factory robots at Tesla on the Model Y line",
            "Leads the controls team building the charging arm",
        ])
        self.assertEqual(len(record["refused"]), 2)
        self.assertEqual([tool for tool, _ in fake.calls], ["get_my_profile", "search_people", "get_person_profile"])
        self.assertEqual(fake.calls[1][1], {"keywords": "Dana Ortiz Chargebot"})
        self.assertNotIn("Head of Controls at Chargebot\\nAustin", json.dumps(record), "the profile text itself is not stored")
        self.assertEqual(target["interviewer_error"], "")
        self.assertFalse(interviewer_due(self.conn, target, USER), "done until who it is changes")
        update_target(self.conn, self.target["id"], {"interviewer_linkedin": "https://www.linkedin.com/in/dana-o/"}, user_id=USER)
        self.assertTrue(interviewer_due(self.conn, self.now(), USER))

    def test_a_profile_that_never_names_the_company_is_marked(self):
        self.inbox("1", "dana@chargebot.example", "Dana Ortiz", "Invitation: call", "2026-09-28T18:30:00+00:00")
        update_target(self.conn, self.target["id"], {"interviewer_linkedin": "linkedin.com/in/someone-else"}, user_id=USER)
        fake = FakeLinkedIn(profile="Dana Ortiz\nNurse at City Hospital\nExperience\nNurse\nCity Hospital\n")
        target = read_interviewer(self.conn, self.target["id"], user_id=USER,
                                  client=LinkedInClient(call=fake, config=lambda: GOOD_CONFIG, sleep=lambda _s: None), writer=None)
        self.assertFalse(target["interviewer"]["linkedin"]["confirmed"])
        self.assertNotIn("search_people", [tool for tool, _ in fake.calls], "the student's link is used as given")

    def test_who_is_kept_even_when_linkedin_is_off(self):
        self.inbox("1", "dana@chargebot.example", "Dana Ortiz", "Re: call", "2026-09-28T18:30:00+00:00")
        with mock.patch.dict("os.environ", {LINKEDIN_ENV: ""}):
            target = read_interviewer(self.conn, self.target["id"], user_id=USER,
                                      client=LinkedInClient(call=FakeLinkedIn(), config=lambda: GOOD_CONFIG), writer=None)
        self.assertEqual(target["interviewer"]["name"], "Dana Ortiz")
        self.assertIn("LinkedIn is off", target["interviewer_error"])

    def test_several_people_by_that_name_leave_the_choice_to_the_student(self):
        self.inbox("1", "dana@chargebot.example", "Dana Ortiz", "Re: call", "2026-09-28T18:30:00+00:00")
        fake = FakeLinkedIn(people=[("Dana Ortiz", "dana1", "Engineer at Chargebot"), ("Dana Ortiz", "dana2", "Designer at Chargebot")])
        target = read_interviewer(self.conn, self.target["id"], user_id=USER,
                                  client=LinkedInClient(call=fake, config=lambda: GOOD_CONFIG, sleep=lambda _s: None), writer=None)
        self.assertIn("several people", target["interviewer_error"])
        self.assertEqual(target["interviewer"]["candidate_count"], 2)
        self.assertNotIn("candidates", target["interviewer"], "other people's names and links are not stored, since nothing shows them")
        self.assertNotIn("dana1", json.dumps(target["interviewer"]))
        self.assertNotIn("get_person_profile", [tool for tool, _ in fake.calls])


    def new_target(self, company, website, **fields):
        """Another company to work with, in place of Chargebot."""
        self.target = create_target(self.conn, {"company": company, "website": website, **fields}, user_id=USER)
        update_target(self.conn, self.target["id"], {"status": "call_scheduled"}, user_id=USER)
        return self.target

    def read(self, fake, writer=None):
        return read_interviewer(self.conn, self.target["id"], user_id=USER,
                                client=LinkedInClient(call=fake, config=lambda: GOOD_CONFIG, sleep=lambda _s: None), writer=writer)

    def test_a_link_the_student_typed_names_the_person_from_the_profile_not_the_mailbox(self):
        self.inbox("1", "rita@chargebot.example", "Rita Chen", "Re: Internship", "2026-09-28T18:00:00+00:00")
        update_target(self.conn, self.target["id"], {"interviewer_linkedin": "https://www.linkedin.com/in/riley-park/"}, user_id=USER)
        fake = FakeLinkedIn(profile="Riley Park\nStaff Engineer at Chargebot\nExperience\nStaff Engineer\nChargebot\n2024 - Present\nLeads the perception team building the lidar stack.\n")
        reply = notes_reply({"topic": "now", "text": "Leads the perception team building the lidar stack", "quote": "Leads the perception team building the lidar stack"})
        record = self.read(fake, judged(reply))["interviewer"]
        self.assertEqual((record["name"], record["basis"], record["email"]), ("Riley Park", "student", ""))
        self.assertIn("Rita Chen", record["evidence"], "both names are shown when they differ")
        self.assertTrue(record["linkedin"]["confirmed"])
        self.assertEqual(len(record["notes"]), 1)

    def test_a_profile_that_is_not_the_named_person_is_not_confirmed_and_gets_no_notes(self):
        update_target(self.conn, self.target["id"], {"interviewer_name": "Rita Chen", "interviewer_linkedin": "linkedin.com/in/riley-park"}, user_id=USER)
        fake = FakeLinkedIn(profile="Riley Park\nStaff Engineer at Chargebot\nExperience\nStaff Engineer\nChargebot\n")

        def writer(instructions, content):
            raise AssertionError("no notes are written from a profile that is someone else's")

        record = self.read(fake, writer)["interviewer"]
        self.assertFalse(record["linkedin"]["confirmed"], "the company is named, but the profile is Riley Park's")
        self.assertIn("this profile is Riley Park, not Rita Chen", record["linkedin"]["why"])
        self.assertEqual(record["notes"], [])

    def test_shared_inboxes_and_role_names_are_not_the_interviewer(self):
        self.inbox("1", "dana@chargebot.example", "Dana Ortiz", "Re: Internship", "2026-09-28T18:00:00+00:00")
        invitation = "Invitation: Interview @ Tue Sep 29, 2026 10am - 10:30am (CDT) (student@school.edu)"
        self.inbox("2", "no-reply@chargebot.example", "Chargebot Recruiting Team", invitation, "2026-09-28T19:00:00+00:00", kind="possible")
        self.inbox("3", "notifications@chargebot.example", "Chargebot Notifications", invitation, "2026-09-28T19:10:00+00:00", kind="possible")
        self.inbox("4", "pat@chargebot.example", "Chargebot Recruiting Team", invitation, "2026-09-28T19:20:00+00:00", kind="possible")
        who = find_interviewer(self.conn, self.now(), USER)
        self.assertEqual((who["name"], who["basis"], who["others"]), ("Dana Ortiz", "mailbox_sender", []))

    def test_a_company_domain_is_the_one_the_inbox_counts(self):
        invitation = "Invitation: Call @ Tue Sep 29, 2026 10am - 10:30am (CDT)"
        # careers.acme3.com is acme3.com: the contact's and a colleague's mail are both the company's.
        self.new_target("Acme Three", "https://careers.acme3.com", contact_email="sam@acme3.com", contact_name="Sam Lee")
        self.inbox("1", "dana@acme3.com", "Dana Ortiz", invitation, "2026-09-28T18:00:00+00:00", kind="possible")
        who = find_interviewer(self.conn, self.now(), USER)
        self.assertEqual((who["name"], who["basis"]), ("Dana Ortiz", "mailbox_invitation"), "not the contact, with a made-up 'no one else wrote'")
        # A university's website stands for nobody else at the university.
        self.new_target("Bovi Lab", "https://stateu.edu/bovi-lab", contact_email="ann@stateu.edu", contact_name="Ann Bovi")
        self.inbox("2", "ann@stateu.edu", "Ann Bovi", "Re: hello", "2026-09-27T18:00:00+00:00")
        self.inbox("3", "registrar@stateu.edu", "Office of the Registrar", "Re: hello", "2026-09-28T18:00:00+00:00", kind="possible")
        self.inbox("4", "sports@stateu.edu", "Campus Sports Desk", "Hello", "2026-09-28T19:00:00+00:00", kind="possible")
        who = find_interviewer(self.conn, self.now(), USER)
        self.assertEqual((who["name"], who["others"], who["elsewhere"]), ("Ann Bovi", [], []))
        # The contact's own invitation counts even when the website is at another domain of theirs.
        self.new_target("Acme Ai", "https://acme.ai", contact_email="sam@acme.com", contact_name="Sam Lee")
        self.inbox("5", "sam@acme.com", "Sam Lee", invitation, "2026-09-28T18:00:00+00:00", kind="possible")
        who = find_interviewer(self.conn, self.now(), USER)
        self.assertEqual((who["name"], who["basis"], who["meeting"]), ("Sam Lee", "mailbox_invitation", "Tue Sep 29, 2026 10am - 10:30am (CDT)"))

    def test_the_call_that_is_next_beats_an_older_invitation_and_a_plain_reply(self):
        now = datetime(2026, 9, 28, 12, tzinfo=timezone.utc)
        self.inbox("1", "rec@chargebot.example", "Rita Chen", "Invitation: Phone screen @ Mon Sep 21, 2026 9am - 9:30am (CDT)", "2026-09-18T18:00:00+00:00", kind="possible")
        self.inbox("2", "dana@chargebot.example", "Dana Ortiz", "Invitation: Technical call @ Tue Sep 29, 2026 10am - 10:30am (CDT)", "2026-09-26T18:00:00+00:00", kind="possible")
        self.inbox("3", "rec@chargebot.example", "Rita Chen", "Re: next steps", "2026-09-27T18:00:00+00:00")
        who = find_interviewer(self.conn, self.now(), USER, now)
        self.assertEqual((who["name"], who["meeting"], [person["name"] for person in who["others"]]),
                         ("Dana Ortiz", "Tue Sep 29, 2026 10am - 10:30am (CDT)", ["Rita Chen"]))

    def test_an_invitation_for_a_call_already_past_loses_to_one_coming_even_when_newer(self):
        now = datetime(2026, 9, 28, 12, tzinfo=timezone.utc)
        self.inbox("1", "sam@chargebot.example", "Sam Lee", "Invitation: Second call @ Tue Sep 29, 2026 10am - 10:30am (CDT)", "2026-09-20T18:00:00+00:00", kind="possible")
        self.inbox("2", "dana@chargebot.example", "Dana Ortiz", "Invitation: First call @ Mon Sep 21, 2026 9am - 9:30am (CDT)", "2026-09-26T18:00:00+00:00", kind="possible")
        self.assertEqual(find_interviewer(self.conn, self.now(), USER, now)["name"], "Sam Lee")

    def test_a_canceled_invitation_is_withdrawn_and_an_updated_one_counts(self):
        now = datetime(2026, 9, 28, 12, tzinfo=timezone.utc)
        self.inbox("1", "dana@chargebot.example", "Dana Ortiz", "Invitation: Call @ Tue Sep 29, 2026 10am - 10:30am (CDT)", "2026-09-26T18:00:00+00:00", kind="possible")
        self.inbox("2", "dana@chargebot.example", "Dana Ortiz", "Canceled event: Call @ Tue Sep 29, 2026 10am - 10:30am (CDT)", "2026-09-27T18:00:00+00:00", kind="possible")
        who = find_interviewer(self.conn, self.now(), USER, now)
        self.assertEqual((who["name"], who["basis"], who["meeting"]), ("Dana Ortiz", "mailbox_sender", ""), "no call is coming from her")
        self.inbox("3", "sam@chargebot.example", "Sam Lee", "Updated invitation with note: Call @ Wed Sep 30, 2026 9am - 9:30am (CDT)", "2026-09-27T19:00:00+00:00", kind="possible")
        who = find_interviewer(self.conn, self.now(), USER, now)
        self.assertEqual((who["name"], who["basis"], who["meeting"]), ("Sam Lee", "mailbox_invitation", "Wed Sep 30, 2026 9am - 9:30am (CDT)"))

    def test_refused_notes_keep_no_model_text_and_are_capped(self):
        self.inbox("1", "dana@chargebot.example", "Dana Ortiz", "Re: call", "2026-09-28T18:30:00+00:00")
        reply = notes_reply(*[
            {"topic": "now", "text": f"Profile line {number}: Dana Ortiz phone 555 0100 home address 12 Elm St", "quote": "a doctorate from Stanford University in robotics"}
            for number in range(60)
        ])
        record = self.read(FakeLinkedIn(), lambda instructions, content: reply)["interviewer"]
        self.assertLessEqual(len(record["refused"]), 11)
        self.assertNotIn("555", json.dumps(record["refused"]))
        self.assertNotIn("Elm St", json.dumps(record["refused"]))

    def test_a_profile_is_confirmed_by_work_history_not_a_common_word_or_a_post(self):
        self.new_target("Ramp", "https://ramp.example")
        self.inbox("1", "dana@ramp.example", "Dana Ortiz", "Re: call", "2026-09-28T18:30:00+00:00")
        update_target(self.conn, self.target["id"], {"interviewer_linkedin": "linkedin.com/in/dana-rn"}, user_id=USER)
        record = self.read(FakeLinkedIn(profile="Dana Ortiz\nNurse at City Hospital\nHelped ramp up the new ICU\n"))["interviewer"]
        self.assertFalse(record["linkedin"]["confirmed"], "'ramp up' is not the company Ramp")
        record = self.read(FakeLinkedIn(sections={"main_profile": "Dana Ortiz\nNurse at City Hospital", "posts": "Ramp is hiring, come and see"}))["interviewer"]
        self.assertFalse(record["linkedin"]["confirmed"], "a post is not where someone works")
        record = self.read(FakeLinkedIn(profile="Dana Ortiz\nEngineer at Ramp\nAustin, Texas\n"))["interviewer"]
        self.assertTrue(record["linkedin"]["confirmed"])

    def test_no_result_naming_the_company_says_so_and_offers_only_people_by_that_name(self):
        self.inbox("1", "dana@chargebot.example", "Dana Ortiz", "Re: call", "2026-09-28T18:30:00+00:00")
        target = self.read(FakeLinkedIn(people=[("Priya Patel", "priya", "Nurse")]))
        self.assertEqual(target["interviewer"]["candidate_count"], 0)
        self.assertIn("no one found as Dana Ortiz", target["interviewer_error"])
        self.assertNotIn("several", target["interviewer_error"])
        target = self.read(FakeLinkedIn(people=[("Dana Ortiz", "dana-rn", "Nurse at City Hospital"), ("Priya Patel", "priya", "Nurse")]))
        self.assertEqual(target["interviewer"]["candidate_count"], 1)
        self.assertNotIn("dana-rn", json.dumps(target["interviewer"]))
        self.assertIn("one person named Dana Ortiz, but their result does not name Chargebot", target["interviewer_error"])

    def test_the_contact_is_not_said_to_be_the_only_one_when_a_reply_was_not_read(self):
        update_target(self.conn, self.target["id"], {"contact_name": "Jane Doe"}, user_id=USER)
        who = find_interviewer(self.conn, self.now(), USER)
        self.assertEqual((who["name"], who["basis"]), ("Jane Doe", "contact"))
        self.assertIn("no one else was found", who["evidence"])
        log_reply(self.conn, self.target["id"], "I'm Riley Park, CTO. Can we talk Thursday?", user_id=USER)
        who = find_interviewer(self.conn, self.now(), USER)
        self.assertEqual((who["name"], who["basis"]), ("Jane Doe", "contact_unconfirmed"))
        self.assertNotIn("no one else", who["evidence"])
        self.assertIn("check this", who["evidence"])
        fake = FakeLinkedIn()
        target = self.read(fake)
        self.assertEqual(fake.calls, [], "nothing is looked up on LinkedIn for a name that is not confirmed")
        self.assertIn("Not searched on LinkedIn", target["interviewer_error"])

    def test_someone_from_another_domain_is_not_dropped_silently(self):
        update_target(self.conn, self.target["id"], {"contact_name": "Jane Doe"}, user_id=USER)
        self.inbox("1", "riley@chargebot.ai", "Riley Park", "Re: Internship", "2026-09-28T18:00:00+00:00")
        who = find_interviewer(self.conn, self.now(), USER)
        self.assertEqual((who["name"], who["basis"]), ("Jane Doe", "contact_unconfirmed"))
        self.assertIn("Riley Park", who["evidence"])
        self.assertEqual([person["name"] for person in who["elsewhere"]], ["Riley Park"])


    def test_a_scheduling_inbox_is_not_the_interviewer_but_its_invitation_gives_the_time(self):
        invitation = "Invitation: Interview @ Tue Sep 29, 2026 10am - 10:30am (CDT) (student@school.edu)"
        now = datetime(2026, 9, 28, 12, tzinfo=timezone.utc)
        self.new_target("Acme Robotics", "https://acmerobotics.com")
        self.inbox("1", "dana@acmerobotics.com", "Dana Ortiz", "Re: hi", "2026-09-26T18:00:00+00:00")
        self.inbox("2", "interviews@acmerobotics.com", "Acme Robotics Interviews", invitation, "2026-09-27T18:00:00+00:00", kind="possible")
        who = find_interviewer(self.conn, self.now(), USER, now)
        self.assertEqual((who["name"], who["basis"], who["others"]), ("Dana Ortiz", "mailbox_sender", []))
        self.assertEqual(who["meeting"], "Tue Sep 29, 2026 10am - 10:30am (CDT)")
        # A role address, or a name that is the company's or ends in a role word, is never a person.
        for number, (address, name) in enumerate([
            ("scheduling@acmerobotics.com", "Sam Lee"), ("interview@acmerobotics.com", "Sam Lee"), ("talent@acmerobotics.com", "Sam Lee"),
            ("meetings@acmerobotics.com", "Sam Lee"), ("calendar@acmerobotics.com", "Sam Lee"), ("hiring@acmerobotics.com", "Sam Lee"),
            ("sam@acmerobotics.com", "Acme Robotics"), ("sam@acmerobotics.com", "Sam Lee Scheduling"), ("sam@acmerobotics.com", "Acme Interviews"),
        ], start=10):
            self.inbox(str(number), address, name, invitation, "2026-09-28T09:00:00+00:00", kind="possible")
        who = find_interviewer(self.conn, self.now(), USER, now)
        self.assertEqual((who["name"], who["others"]), ("Dana Ortiz", []))

    def test_someone_only_copied_at_another_domain_is_not_named(self):
        self.new_target("Acme Robotics", "https://acmerobotics.com", contact_email="sam@acmerobotics.com", contact_name="Sam Lee")
        update_target(self.conn, self.target["id"], {"contact_cc": "prof.jones@othercorp.com"}, user_id=USER)
        self.inbox("1", "prof.jones@othercorp.com", "Prof Jones", "Re: hi", "2026-09-27T18:00:00+00:00")
        who = find_interviewer(self.conn, self.now(), USER)
        self.assertNotEqual(who["name"], "Prof Jones")
        self.assertEqual((who["name"], who["basis"]), ("Sam Lee", "contact_unconfirmed"), "the contact, whose reply may not have been read")
        self.assertEqual([person["name"] for person in who["elsewhere"]], ["Prof Jones"])
        # A colleague copied at the company's own domain is still the company's.
        update_target(self.conn, self.target["id"], {"contact_cc": "dana@acmerobotics.com"}, user_id=USER)
        self.inbox("2", "dana@acmerobotics.com", "Dana Ortiz", "Re: hi", "2026-09-28T18:00:00+00:00")
        self.assertEqual(find_interviewer(self.conn, self.now(), USER)["name"], "Dana Ortiz")

    def test_every_kind_of_cancellation_withdraws_the_invitation(self):
        now = datetime(2026, 9, 28, 12, tzinfo=timezone.utc)
        for number, canceled in enumerate([
            "Canceled event with note: Call @ Wed Sep 30, 2026 10am - 10:30am (CDT)", "Canceled: Call @ Wed Sep 30, 2026 10am - 10:30am (CDT)",
            "Cancelled event: Call @ Wed Sep 30, 2026 10am - 10:30am (CDT)", "Invitation canceled",
        ]):
            with self.subTest(canceled):
                host = f"acme{number}.example"
                self.new_target(f"Acme {number}", f"https://{host}")
                self.inbox(f"a{number}", f"dana@{host}", "Dana Ortiz", "Invitation: Call @ Wed Sep 30, 2026 10am - 10:30am (CDT)", "2026-09-25T18:00:00+00:00", kind="possible")
                self.inbox(f"b{number}", f"dana@{host}", "Dana Ortiz", canceled, "2026-09-27T18:00:00+00:00", kind="possible")
                self.inbox(f"c{number}", f"sam@{host}", "Sam Lee", "Invitation: Call @ Tue Sep 29, 2026 10am - 10:30am (CDT)", "2026-09-20T18:00:00+00:00", kind="possible")
                who = find_interviewer(self.conn, self.now(), USER, now)
                self.assertEqual((who["name"], who["basis"]), ("Sam Lee", "mailbox_invitation"), "her call is withdrawn")
                self.assertEqual(who["meeting"], "Tue Sep 29, 2026 10am - 10:30am (CDT)")

    def test_the_soonest_call_wins_not_the_newest_invitation(self):
        now = datetime(2026, 9, 28, 12, tzinfo=timezone.utc)
        self.inbox("1", "dana@chargebot.example", "Dana Ortiz", "Invitation: First round @ Tue Sep 29, 2026 10am - 10:30am (CDT)", "2026-09-20T18:00:00+00:00", kind="possible")
        self.inbox("2", "sam@chargebot.example", "Sam Lee", "Invitation: Final round @ Mon Oct 12, 2026 10am - 11am (CDT)", "2026-09-27T18:00:00+00:00", kind="possible")
        self.assertEqual(find_interviewer(self.conn, self.now(), USER, now)["name"], "Dana Ortiz")
        # With no date to read, the newest invitation stands.
        self.new_target("Acme Undated", "https://acmeundated.example")
        self.inbox("3", "dana@acmeundated.example", "Dana Ortiz", "Invitation: First round", "2026-09-20T18:00:00+00:00", kind="possible")
        self.inbox("4", "sam@acmeundated.example", "Sam Lee", "Invitation: Final round", "2026-09-27T18:00:00+00:00", kind="possible")
        self.assertEqual(find_interviewer(self.conn, self.now(), USER, now)["name"], "Sam Lee")

    def test_a_one_word_company_is_confirmed_only_by_work_history(self):
        def confirmed(company, text=None, sections=None):
            profile = {"url": "u", "sections": sections or {"main_profile": text}}
            return not confirm_profile(profile, "Dana Ortiz", company)

        cases = [
            ("Ramp", "Dana Ortiz\nCharge Nurse\nExperience\nCharge Nurse\nCity Hospital\nRamp up of the new ICU wing\n", False),
            ("Linear", "Dana Ortiz\nTutor\nExperience\nTutor\nCity College\nLinear algebra and calculus\n", False),
            ("Scale", "Dana Ortiz\nOps Lead\nExperience\nOps Lead\nCity Hospital\nScale operations across 3 sites\n", False),
            ("Mercury", "Dana Ortiz\nAgent at Mercury Insurance\n", False),
            ("Acme Robotics", "Dana Ortiz\nNurse at City Hospital\nAbout\nI love reading about Acme Robotics and their lidar\nExperience\nNurse\nCity Hospital\n", False),
            ("Ramp", "Dana Ortiz\nEngineer at Ramp\n", True),
            ("Ramp", "Dana Ortiz\nEngineer @ Ramp\n", True),
            ("Ramp, Inc.", "Dana Ortiz\nExperience\nEngineer\nRamp · Full-time\n2024 - Present\n", True),
            ("Chargebot", "Dana Ortiz\nEngineer at ChargeBot\n", True),
            ("Doordash", "Dana Ortiz\nEngineer at DoorDash\n", True),
            ("Mercury", "Dana Ortiz\nAgent at Mercury\n", True),
            ("Acme Robotics, Inc.", "Dana Ortiz\nEngineer at Acme Robotics\n", True),
        ]
        for company, text, expected in cases:
            with self.subTest(company=company, text=text):
                self.assertEqual(confirmed(company, text), expected)
        self.assertFalse(confirmed("Ramp", sections={"main_profile": "Dana Ortiz\nNurse", "posts": "Ramp is hiring"}))

    def test_names_match_across_accents_and_short_given_names(self):
        def confirmed(name, header):
            profile = {"url": "u", "sections": {"main_profile": f"{header}\nEngineer at Ramp\n"}}
            return not confirm_profile(profile, name, "Ramp")

        self.assertTrue(confirmed("Jose Nunez", "José Núñez"))
        self.assertTrue(confirmed("José Núñez", "Jose Nunez"))
        self.assertTrue(confirmed("Dan Ortiz", "Daniel Ortiz"))
        self.assertTrue(confirmed("Daniel Ortiz", "Dan Ortiz"))
        self.assertFalse(confirmed("Dan Ortiz", "Daniel Ortega"), "the surname must match exactly")
        self.assertFalse(confirmed("Al Ortiz", "Dana Ortiz"), "a given name under 3 letters is not a short form")
        found = [{"username": "jn", "name": "José Núñez", "text": "José Núñez\nEngineer at Ramp"}]
        self.assertEqual(pick_profile(found, "Jose Nunez", "Ramp"), ("jn", []))

    def test_the_contacts_own_reply_signed_with_one_name_is_the_contact(self):
        self.new_target("Chargebot Six", "https://chargebot6.example", contact_email="jane@chargebot6.example", contact_name="Jane Doe")
        self.inbox("1", "jane@chargebot6.example", "Jane", "Re: Internship", "2026-09-28T18:00:00+00:00")
        who = find_interviewer(self.conn, self.now(), USER)
        self.assertEqual((who["name"], who["basis"]), ("Jane Doe", "mailbox_sender"))
        self.assertNotIn("not read", who["evidence"])
        fake = FakeLinkedIn(people=[("Jane Doe", "janedoe", "Engineer at Chargebot Six")])
        self.read(fake)
        self.assertIn("search_people", [tool for tool, _ in fake.calls], "the lookup is not blocked")
        # Someone else at the company signing with one name is still no one.
        self.inbox("2", "pat@chargebot6.example", "Pat", "Re: Internship", "2026-09-28T19:00:00+00:00")
        self.assertEqual(find_interviewer(self.conn, self.now(), USER)["name"], "Jane Doe")

    def test_a_failed_lookup_never_leaves_the_previous_person_in_place(self):
        self.inbox("1", "dana@chargebot.example", "Dana Ortiz", "Re: call", "2026-09-27T18:00:00+00:00")
        note = {"topic": "now", "text": "Leads the controls team building the charging arm", "quote": "Leads the controls team building the charging arm"}
        record = self.read(FakeLinkedIn(), judged(notes_reply(note)))["interviewer"]
        self.assertEqual((record["name"], len(record["notes"])), ("Dana Ortiz", 1))
        self.inbox("2", "sam@chargebot.example", "Sam Lee", "Re: call", "2026-09-28T18:00:00+00:00")

        def failing(exc):
            def call(tool, arguments):
                if tool == "get_my_profile":
                    return {"url": "https://www.linkedin.com/in/test-student-123/"}
                raise exc
            return call

        def bad_writer(instructions, content):
            raise TypeError("writer broke")

        attempts = [
            (failing(subprocess.TimeoutExpired(["mcporter"], 300)), None), (failing(AttributeError("'list' object has no attribute 'get'")), None),
            (failing(TypeError("'NoneType' object is not subscriptable")), None), (failing(KeyError("sections")), None),
            (FakeLinkedIn(people=[("Sam Lee", "samlee", "Engineer at Chargebot")], profile="Sam Lee\nEngineer at Chargebot\n"), bad_writer),
        ]
        for number, (call, writer) in enumerate(attempts):
            with self.subTest(number):
                target = self.read(call, writer)
                self.assertEqual(target["interviewer"]["name"], "Sam Lee", "the new person, not Dana")
                self.assertEqual(target["interviewer"]["notes"], [])
                self.assertTrue(target["interviewer_error"])

    def test_a_confirmed_profile_holds_no_other_peoples_links(self):
        self.inbox("1", "dana@chargebot.example", "Dana Ortiz", "Re: call", "2026-09-28T18:30:00+00:00")
        record = self.read(FakeLinkedIn(), judged(notes_reply()))["interviewer"]
        self.assertTrue(record["linkedin"]["confirmed"])
        self.assertEqual(record["candidate_count"], 0, "one result matched and was used, so nobody was left to choose from")
        self.assertNotIn("candidates", record)

    def test_the_profile_is_checked_a_line_at_a_time(self):
        """A name or number on a far-off line does not back a note about another line."""
        self.inbox("1", "dana@chargebot.example", "Dana Ortiz", "Re: call", "2026-09-28T18:30:00+00:00")
        filler = ["Worked on sensors and drives and firmware for many machines across several labs and plants."] * 8
        profile = "\n".join([
            "Dana Ortiz", "Head of Controls at Chargebot", "Austin, Texas", "Experience", "Head of Controls", "Chargebot", "2024 - Present",
            "Leads the controls team building the charging arm.", *filler, "Volunteered with 9 mentors and Stanford alumni at a shelter.",
        ])
        reply = notes_reply(
            {"topic": "now", "text": "Leads the controls team building the charging arm with 9 mentors", "quote": "Leads the controls team building the charging arm"},
            {"topic": "now", "text": "Leads the controls team building the charging arm with Stanford alumni", "quote": "Leads the controls team building the charging arm"},
            {"topic": "now", "text": "Leads the controls team building the charging arm", "quote": "Leads the controls team building the charging arm"},
        )
        record = self.read(FakeLinkedIn(profile=profile), judged(reply))["interviewer"]
        self.assertEqual([note["text"] for note in record["notes"]], ["Leads the controls team building the charging arm"])
        self.assertEqual(len(record["refused"]), 2)

    def test_the_second_read_sees_the_notes_own_line_not_the_whole_profile(self):
        self.inbox("1", "dana@chargebot.example", "Dana Ortiz", "Re: call", "2026-09-28T18:30:00+00:00")
        seen = []

        def model(instructions, content):
            if instructions == NOTES_INSTRUCTIONS:
                return notes_reply({"topic": "now", "text": "Leads the controls team building the charging arm",
                                    "quote": "Leads the controls team building the charging arm"})
            seen.extend(json.loads(content)["items"])
            return json.dumps({"verdicts": [{"id": item["id"], "supported": True, "why": ""} for item in json.loads(content)["items"]]})

        self.read(FakeLinkedIn(), model)
        self.assertEqual(len(seen), 1)
        self.assertIn("Leads the controls team building the charging arm.", seen[0]["passage"].splitlines(), "its own line stands whole")
        self.assertNotIn("Education", seen[0]["top"], "the top is the first lines, not the profile")

    def test_a_note_the_second_read_did_not_answer_is_not_kept_as_checked(self):
        self.inbox("1", "dana@chargebot.example", "Dana Ortiz", "Re: call", "2026-09-28T18:30:00+00:00")
        note = {"topic": "now", "text": "Leads the controls team building the charging arm", "quote": "Leads the controls team building the charging arm"}
        for answer in ("not json at all", json.dumps({"verdicts": []})):
            with self.subTest(answer):
                def model(instructions, content, answer=answer):
                    return notes_reply(note) if instructions == NOTES_INSTRUCTIONS else answer

                record = self.read(FakeLinkedIn(), model)["interviewer"]
                self.assertEqual(record["notes"], [])
                self.assertIn("gave no answer", record["refused"][0]["reason"])

    def test_the_company_renamed_during_the_lookup_is_not_given_the_old_ones_profile(self):
        self.inbox("1", "dana@chargebot.example", "Dana Ortiz", "Re: call", "2026-09-28T18:30:00+00:00")
        fake = FakeLinkedIn()
        real = fake.__call__

        def renaming(tool, arguments):
            if tool == "get_person_profile":
                update_target(self.conn, self.target["id"], {"company": "Different Motors", "website": "https://different.example"}, user_id=USER)
            return real(tool, arguments)

        before = self.now()["interviewer"]
        target = self.read(renaming, judged(notes_reply()))
        self.assertEqual(target["company"], "Different Motors")
        self.assertEqual(target["interviewer"], before, "nothing about the old company's person was written")
        self.assertNotIn("Chargebot", json.dumps(target["interviewer"]))


if __name__ == "__main__":
    unittest.main()
