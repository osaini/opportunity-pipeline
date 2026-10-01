"""Call prep notes for outreach targets that wrote back."""

import json
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi.testclient import TestClient

from opportunity_app import STATIC_DIR, automation
from opportunity_app.api import create_app
from opportunity_app.outreach import confirm_research, create_target, get_target, log_reply, update_target
from opportunity_app.operations import enqueue_job, run_next_job
from opportunity_app.outreach_call_prep import (
    DURING_CALL, JOB_TYPE, JOB_TYPES, PAUSED_WAIT, CallPrepRejected, CallPrepWorker, ReplyRequired, auto_queue_call_prep,
    generate_call_prep, is_automatic, queue_call_prep,
)
from opportunity_app import outreach_call_prep, outreach_call_questions
from opportunity_app.agent_providers import ProviderReply
from opportunity_app.outreach_research import queue_research
from opportunity_app.schema import ensure_product_schema
from opportunity_app.database import connect_product
from opportunity_app.worker import WEB_APP_JOB_TYPES

from helpers_platform import build_and_migrate
from helpers_source import js_function, python_modules, static_script_text
from helpers_outreach import BLOCKED_FACT, BRIEF, PORT_FACT, STACK_FACT, store_brief
from helpers_outreach import DRAFTING_AUTH as AUTH, USER, confirm_facts
from helpers_outreach import DraftingScriptedProvider as DraftProvider

REPLY = "Thanks for writing! Could we set up a call Thursday at 3pm? I'd like to hear about your drone work."
EXPERIENCE = [
    {
        "organization": "Dronelab", "role": "Engineering Intern", "outreach": "lead",
        "highlights": ["Cut drone weight from 1.5 kg to 500 g"],
    },
    {
        "organization": "Secret Co", "role": "Software Intern", "outreach": "omit",
        "highlights": ["Indexed 250,000 records"],
    },
]


class ScriptedProvider(DraftProvider):
    """The drafting tests' scripted model, which also answers call prep's line check on its own.

    The line check says yes to every line except those whose text is in
    ``refuse``, and is kept apart from ``prompts``, which hold only the notes'.
    """

    def __init__(self, replies, refuse=(), silent=()):
        super().__init__(replies)
        self.refuse, self.silent, self.line_checks = set(refuse), set(silent), []

    def create(self, *, instructions, messages, tools, max_output_tokens):
        if instructions == outreach_call_prep.LINE_CHECK_INSTRUCTIONS:
            items = json.loads(messages[-1]["content"])["items"]
            self.line_checks += items
            text = json.dumps({"verdicts": [
                {"id": item["id"], "supported": not any(line in item["line"] for line in self.refuse), "why": "checked"}
                for item in items if not any(line in item["line"] for line in self.silent)
            ]})
            return ProviderReply(text=text)
        return super().create(instructions=instructions, messages=messages, tools=tools, max_output_tokens=max_output_tokens)


def prep_json(**overrides):
    sections = {
        "questions": [
            {"text": "I read that the arm finds the port with a camera: how did you land on that?", "from": ["f1"]},
            {"text": "What would a first project be?", "from": ["f2"]},
        ],
        "talking_points": [{"text": "Cut our drone's weight from 1.5 kg to 500 g", "basis": "profile:experience"}],
    }
    sections.update(overrides)
    return json.dumps(sections)


class CallPrepTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.questions_file = Path(self.tempdir.name) / "call_prep.local.json"
        patcher = mock.patch.object(outreach_call_questions, "QUESTIONS_PATH", self.questions_file)
        patcher.start()
        self.addCleanup(patcher.stop)
        _, self.platform_path = build_and_migrate(Path(self.tempdir.name))
        self.conn = connect_product(self.platform_path)
        ensure_product_schema(self.conn)
        confirm_facts(self.conn, name="Test Student", school="UT Austin", degree="Mechanical Engineering", experience=EXPERIENCE)
        self.target = create_target(self.conn, {
            "company": "Chargebot",
            "website": "https://chargebot.example",
            "summary": "Robots that charge parked EVs",
            "contact_email": "hi@chargebot.example",
            "source_urls": ["https://chargebot.example/about"],
            "email_subject": "Drone builder", "email_body": "I cut drone weight from 1.5 kg to 500 g.",
        }, user_id=USER)
        update_target(self.conn, self.target["id"], {"status": "sent"}, user_id=USER)

    def tearDown(self):
        self.conn.close()
        self.tempdir.cleanup()

    def reply_and_mark(self, status="call_scheduled"):
        log_reply(self.conn, self.target["id"], REPLY, user_id=USER)
        update_target(self.conn, self.target["id"], {"status": status}, user_id=USER)

    def generate(self, provider):
        return generate_call_prep(self.conn, self.target["id"], user_id=USER, provider_factory=lambda *_: provider, provider="anthropic")

    def test_the_notes_open_with_what_to_ask_then_what_to_know(self):
        self.reply_and_mark()
        store_brief(self.conn, self.target["id"])
        provider = ScriptedProvider([prep_json(questions=[{"text": "Your arm plugs in within 90 seconds: what limits it?", "from": ["f1"]}])])
        notes = self.generate(provider)["call_prep"]
        order = [notes.index(heading) for heading in (
            "CHARGEBOT: CALL PREP", "=== ASK (in call order: rapport → their story → product → my role) ===", "TALKING POINTS (from my email → where it lands for them)",
            "\n=== KNOW ===\n", "\nHOW IT WORKS\n", "\nBUILT WITH\n", "\nFUNDING + PARTNERS\n", "\nNOT ONLINE",
            "\n=== DURING THE CALL ===\n", "\nSOURCES\n",
        )]
        self.assertEqual(order, sorted(order), notes)
        self.assertNotIn("WHAT THEY SAID", notes, "the student knows the thread")
        self.assertIn("1. Your arm plugs in within 90 seconds: what limits it? [1]\n", notes)
        self.assertIn(f"- {PORT_FACT['text']} [1]\n", notes)
        self.assertIn(f"- {STACK_FACT['text']} [2]\n", notes)
        self.assertIn(f"- {BLOCKED_FACT['text']} [3] (not checked)\n", notes, "a fact its page could not confirm says so")
        self.assertIn("- which motors the arm uses\n", notes)
        self.assertIn("1  news.example/chargebot-seed\n2  chargebot.example/careers\n3  news.example/blocked", notes, "short sources to copy")
        self.assertIn("Web research 2026-09-20: 3 facts, 3 pages, each confirmed on its page except 1 marked (not checked).", notes)
        self.assertNotIn("ABOUT THEM", notes, "with research, the one-line summary is not repeated")
        prompt = provider.prompts[0]
        self.assertIn(PORT_FACT["text"], prompt, "the model sees the research to build questions on")
        self.assertNotIn(BLOCKED_FACT["text"], prompt, "but nothing is built on a fact nobody could check")
        self.assertIn("which motors the arm uses", prompt)
        self.assertIn("Thursday at 3pm", prompt, "the reply still reaches the model")
        self.assertNotIn(PORT_FACT["source_url"], prompt.split('"source_urls"')[0], "but not the brief's sources")
        claims = self.target_claims()
        self.assertIn({"text": f"{BLOCKED_FACT['text']} (not checked)", "basis": BLOCKED_FACT["source_url"], "section": "traction"}, claims)
        self.assertEqual([item["line"] for item in provider.line_checks], ["Your arm plugs in within 90 seconds: what limits it?"],
                         "the question was checked against what it cites")

    def target_claims(self):
        return get_target(self.conn, self.target["id"], user_id=USER)["call_prep_claims"]

    def test_without_research_the_notes_use_what_is_on_file_and_say_why(self):
        self.reply_and_mark()
        store_brief(self.conn, self.target["id"], brief={}, at=None, error="Claude Code exited 1: not signed in")
        provider = ScriptedProvider([prep_json()])
        target = self.generate(provider)
        notes = target["call_prep"]
        for heading in ("=== ASK (in call order: rapport → their story → product → my role) ===", "TALKING POINTS", "=== KNOW ===", "ABOUT THEM (deep search only", "=== DURING THE CALL ==="):
            self.assertIn(heading, notes)
        self.assertIn("No web research yet: Claude Code exited 1: not signed in. Press Research this company", notes)
        self.assertIn("- Robots that charge parked EVs\n", notes)
        self.assertIn("- Cut our drone's weight from 1.5 kg to 500 g\n", notes)
        self.assertIn("   ready: no web research yet", notes)
        self.assertNotIn("SOURCES", notes)
        for blank in DURING_CALL:
            self.assertIn(f"- {blank}", notes)
        self.assertEqual(target["call_prep_generated_by"], "anthropic:claude-sonnet-5")
        self.assertEqual({claim["section"] for claim in target["call_prep_claims"]}, {"about_them", "talking_points"})
        prompt = provider.prompts[0]
        self.assertIn("Thursday at 3pm", prompt, "the logged reply reaches the model")
        self.assertIn("Dronelab", prompt)
        self.assertNotIn("Secret Co", prompt, "an entry marked omit never reaches the model")

    def test_the_students_own_questions_come_after_theirs_in_their_words_with_a_hook_and_what_to_have_ready(self):
        self.questions_file.write_text(json.dumps({"questions": [
            {"lead_in": "At my last job, my lead set the use case, and it shaped my design.",
             "ask": "What's the use case of the project I'd work on?", "research": ["technology"], "blank": "Use case:"},
            {"lead_in": "Startups race the clock.", "ask": "Where are you looking to expand?", "research": ["growth"],
             "blank": "Where they're expanding:"},
            {"ask": "Where are you short-staffed?", "research": ["hiring", "made_up_section"]},
            {"lead_in": "no question here"},
        ]}), encoding="utf-8")
        self.reply_and_mark()
        store_brief(self.conn, self.target["id"])
        standing = [
            {"id": "s1", "hook": "I read the arm finds the port with a stereo camera.", "ask": "", "from": ["f1"]},
            {"id": "s2", "hook": "", "ask": "What's the bottleneck in getting there?", "from": []},
            {"id": "s3", "hook": "I read you write motion planning in C++.", "ask": "Where do you need hands most?", "from": ["f2"]},
        ]
        provider = ScriptedProvider([prep_json(standing=standing)] * 2, refuse=["Where do you need hands most?"])
        notes = self.generate(provider)["call_prep"]
        ask = notes[notes.index("=== ASK"):notes.index("TALKING POINTS")]
        self.assertIn("3. At my last job, my lead set the use case, and it shaped my design. "
                      "I read the arm finds the port with a stereo camera. What's the use case of the project I'd work on? [1]\n"
                      "   ready: HOW IT WORKS", ask, "their lead-in word for word, the hook after it")
        self.assertIn("4. Startups race the clock. Where are you looking to expand?\n   ready: nothing found online; listen for it", ask,
                      "a sharper ask that builds on nothing is not used")
        self.assertIn("5. Where are you short-staffed?\n   ready: nothing found online; listen for it", ask,
                      "the line check said no, so the student's own words stand")
        self.assertNotIn("no question here", notes)
        self.assertIn("- Use case:\n- Where they're expanding:\n- Role details", notes)
        self.assertIn("Where are you looking to expand?", provider.prompts[0], "the model is told not to ask it again")

    def test_a_questions_file_that_cannot_be_read_says_so(self):
        self.questions_file.write_text("{not json", encoding="utf-8")
        self.reply_and_mark()
        notes = self.generate(ScriptedProvider([prep_json()]))["call_prep"]
        self.assertIn("(call_prep.local.json could not be read, so the default questions are used.)", notes)
        self.assertIn(". Where are you looking to expand?", notes)

    def test_the_reading_is_marked_as_a_read_and_names_its_facts(self):
        self.reply_and_mark()
        store_brief(self.conn, self.target["id"])
        reading = [
            {"about": "enabling_technology", "text": "A stereo camera lets the arm plug in within 90 seconds", "facts": ["f1"]},
            {"about": "selling_point", "text": "fast, camera-guided plug-in", "facts": ["f1", "f2"]},
        ]
        notes = self.generate(ScriptedProvider([prep_json(reading=reading)]))["call_prep"]
        self.assertIn(
            "MY READ (not stated by them)\n"
            "- Edge: fast, camera-guided plug-in [1, 2]\n"
            "- Enabled by: A stereo camera lets the arm plug in within 90 seconds [1]", notes)
        claims = [claim for claim in self.target_claims() if claim["section"] == "reading"]
        self.assertEqual({claim["basis"] for claim in claims}, {"inference"})

    def test_a_reading_line_that_goes_past_its_facts_is_left_out(self):
        self.reply_and_mark()
        store_brief(self.conn, self.target["id"])
        good = {"about": "enabling_technology", "text": "a stereo camera guides the plug", "facts": ["f1"]}
        too_far = [
            {"about": "selling_point", "text": "plugs in within 45 seconds", "facts": ["f1"]},
            {"about": "ideal_customer", "text": "fleet depots", "facts": ["f3"]},
            {"about": "direction", "text": "catching up with Voltarm", "facts": ["f1", "f2"]},
            good,
        ]
        provider = ScriptedProvider([prep_json(reading=too_far), prep_json(reading=too_far)], refuse=["catching up with Voltarm"])
        notes = self.generate(provider)["call_prep"]
        retry = provider.prompts[1]
        self.assertIn("reading selling_point states 45, which its facts do not", retry)
        self.assertIn("reading ideal_customer cites no technical_research fact", retry, "fact 3 was not checked, so it has no id")
        self.assertIn("- Enabled by: a stereo camera guides the plug [1]", notes)
        for gone in ("45 seconds", "fleet depots", "Voltarm"):
            self.assertNotIn(gone, notes, "the notes stand without the lines that went too far; the line check caught the name")

    def test_a_competitor_the_agent_picked_says_so(self):
        self.reply_and_mark()
        rival = {
            "section": "competitors", "text": "Voltarm sells charging robots with lidar", "source_url": "https://news.example/voltarm",
            "quote": "Voltarm builds charging robots with lidar", "person": "", "competitor": "Voltarm", "checked": True,
            "note": "picked as a competitor by the research agent",
        }
        store_brief(self.conn, self.target["id"], brief={**BRIEF, "facts": [*BRIEF["facts"], rival]})
        notes = self.generate(ScriptedProvider([prep_json()]))["call_prep"]
        self.assertIn("COMPETITORS (the research agent's picks)\n- Voltarm sells charging robots with lidar [4]", notes)

    def test_the_interviewer_heads_what_to_know_and_the_call_heads_the_notes(self):
        self.reply_and_mark()
        store_brief(self.conn, self.target["id"])
        record = {
            "name": "Dana Ortiz", "email": "dana@chargebot.example", "basis": "mailbox_invitation",
            "evidence": "Sent the calendar invitation \"Student X Chargebot\"", "meeting": "Tue Sep 29, 2026 10am - 10:30am (CDT)",
            "others": [{"name": "Sam Lee", "email": "sam@chargebot.example"}], "key": "dana ortiz|",
            "linkedin": {"url": "https://www.linkedin.com/in/danaortiz/", "username": "danaortiz", "confirmed": True,
                         "read_at": "2026-09-28T20:00:00+00:00"},
            "notes": [{"topic": "path", "text": "Built motion planning at Tesla", "quote": "..."}],
        }
        # The record is about the person the student named: one for someone else is set aside.
        update_target(self.conn, self.target["id"], {"interviewer_name": "Dana Ortiz"}, user_id=USER)
        self.conn.execute("UPDATE outreach_targets SET interviewer_json=? WHERE id=?", (json.dumps(record), self.target["id"]))
        self.conn.commit()
        question = {"text": "I saw you built motion planning at Tesla: what made you leave for Chargebot?", "from": ["p1"]}
        provider = ScriptedProvider([prep_json(questions=[question])])
        notes = self.generate(provider)["call_prep"]
        self.assertTrue(notes.startswith("CHARGEBOT: CALL PREP\nTue Sep 29, 2026 10am - 10:30am (CDT) | Dana Ortiz\n"), notes[:120])
        know = notes[notes.index("=== KNOW ==="):]
        self.assertIn('DANA ORTIZ  (dana@chargebot.example | sent the invite)\n'
                      "- Also wrote to you: Sam Lee\n- Before: Built motion planning at Tesla\n"
                      "- LI read 2026-09-28 via test account: linkedin.com/in/danaortiz", know)
        self.assertIn(f"1. {question['text']} [LI]", notes)
        self.assertIn("LI linkedin.com/in/danaortiz", notes[notes.index("SOURCES"):])
        self.assertIn('"p1"', provider.prompts[0], "the model sees the notes by id")
        record["linkedin"]["confirmed"] = False
        self.conn.execute("UPDATE outreach_targets SET interviewer_json=? WHERE id=?", (json.dumps(record), self.target["id"]))
        self.conn.commit()
        provider = ScriptedProvider([prep_json()])
        notes = self.generate(provider)["call_prep"]
        self.assertIn("it never names Chargebot; check it is them. Nothing is built on it.", notes)
        self.assertNotIn("Built motion planning at Tesla", provider.prompts[0], "an unconfirmed profile is not built on")

    def test_notes_wait_for_a_reply_status(self):
        with self.assertRaisesRegex(ValueError, "replied"):
            self.generate(ScriptedProvider([prep_json()]))

    def test_an_unsupported_number_or_basis_is_retried_then_refused(self):
        self.reply_and_mark("replied")
        bad = prep_json(talking_points=[{"text": "Flew 900 hours", "basis": "profile:experience"}])
        bad_basis = prep_json(talking_points=[{"text": "They raised a Series B", "basis": "research:funding"}])
        provider = ScriptedProvider([bad, bad_basis])
        with self.assertRaises(CallPrepRejected) as caught:
            self.generate(provider)
        self.assertIn("research:funding", str(caught.exception))
        self.assertIn("900", provider.prompts[1], "the retry says what was wrong")
        self.assertEqual(get_target(self.conn, self.target["id"], user_id=USER)["call_prep"], "", "nothing is stored")

    def test_a_question_may_use_a_number_from_the_research_but_not_a_made_up_one(self):
        self.reply_and_mark("replied")
        store_brief(self.conn, self.target["id"])
        made_up = prep_json(questions=[{"text": "How did you get plug-in time down to 45 seconds?", "from": ["f1"]}])
        provider = ScriptedProvider([made_up, prep_json(questions=[{"text": "What limits the 90 seconds it takes to plug in?", "from": ["f1"]}])])
        notes = self.generate(provider)["call_prep"]
        self.assertIn("45", provider.prompts[1])
        self.assertIn(". What limits the 90 seconds it takes to plug in? [1]", notes)

    def test_a_number_only_in_an_unchecked_fact_or_the_gaps_is_refused(self):
        self.reply_and_mark("replied")
        store_brief(self.conn, self.target["id"], brief={**BRIEF, "gaps": ["why they cut 30 percent of staff"]})
        from_unchecked = prep_json(questions=[{"text": "What will the $4.5M seed round pay for first?", "from": ["f1"]}])
        from_gap = prep_json(questions=[{"text": "Why did you cut 30 percent of staff?", "from": ["g1"]}])
        provider = ScriptedProvider([from_unchecked, from_gap])
        with self.assertRaises(CallPrepRejected) as caught:
            self.generate(provider)
        self.assertIn("4.5", provider.prompts[1])
        self.assertIn("30", str(caught.exception))

    def test_nothing_rests_on_inference(self):
        self.reply_and_mark("replied")
        guessed = prep_json(talking_points=[{"text": "I would be great at sensor mounting", "basis": "inference"}])
        provider = ScriptedProvider([guessed, prep_json()])
        target = self.generate(provider)
        self.assertIn("talking_points rests", provider.prompts[1])
        self.assertNotIn("sensor mounting", target["call_prep"])

    def test_at_most_three_talking_points(self):
        self.reply_and_mark("replied")
        point = {"text": "Cut our drone's weight from 1.5 kg to 500 g", "basis": "profile:experience"}
        notes = self.generate(ScriptedProvider([prep_json(talking_points=[point] * 5)]))["call_prep"]
        self.assertEqual(notes.count(point["text"]), 3)

    def test_the_reply_is_not_summarized_but_the_model_writes_around_it(self):
        self.reply_and_mark()
        provider = ScriptedProvider([prep_json(their_reply=[{"text": "Wants a call Thursday at 3pm", "basis": "reply"}])])
        notes = self.generate(provider)["call_prep"]
        self.assertNotIn("Wants a call", notes, "a summary of the thread the student knows is never printed")
        self.assertIn("Thursday at 3pm", provider.prompts[0])
        self.assertNotIn("their_reply", outreach_call_prep.INSTRUCTIONS)

    def test_notes_need_a_logged_reply(self):
        update_target(self.conn, self.target["id"], {"status": "replied"}, user_id=USER)
        provider = ScriptedProvider([prep_json(their_reply=[])])
        with self.assertRaisesRegex(ReplyRequired, "Paste their reply"):
            self.generate(provider)
        with self.assertRaises(ReplyRequired):
            queue_call_prep(self.conn, self.target["id"], user_id=USER, replace=True, reason="test")
        self.assertEqual(provider.prompts, [], "the model is never asked")

    def test_regenerating_keeps_the_replaced_notes_in_history(self):
        self.reply_and_mark()
        self.generate(ScriptedProvider([prep_json()]))
        update_target(self.conn, self.target["id"], {"call_prep": "My own notes from the call"}, user_id=USER)
        self.generate(ScriptedProvider([prep_json()]))
        events = get_target(self.conn, self.target["id"], user_id=USER, include_events=True)["events"]
        replaced = [event for event in events if event["event_type"] == "call_prep_replaced"]
        self.assertEqual([event["detail"] for event in replaced], ["My own notes from the call"])

    def test_unverified_research_is_labelled_and_cited_as_unverified_until_confirmed(self):
        self.conn.execute("UPDATE outreach_targets SET research_confidence='unverified' WHERE id=?", (self.target["id"],))
        self.conn.commit()
        self.reply_and_mark()
        target = self.generate(ScriptedProvider([prep_json()]))
        self.assertIn("- Robots that charge parked EVs (unverified)\n", target["call_prep"])
        self.assertEqual(target["call_prep_claims"][1], {"text": "Robots that charge parked EVs", "basis": "unverified:summary", "section": "about_them"})
        confirmed = confirm_research(self.conn, self.target["id"], user_id=USER)
        self.assertEqual(confirmed["call_prep_claims"][1]["basis"], "research:summary")

    def test_template_notes_use_facts_only(self):
        self.reply_and_mark()
        store_brief(self.conn, self.target["id"])
        target = generate_call_prep(self.conn, self.target["id"], user_id=USER, provider_factory=None, provider="legacy")
        self.assertEqual(target["call_prep_generated_by"], "template")
        self.assertIn("Cut drone weight from 1.5 kg to 500 g", target["call_prep"])
        self.assertIn(f"- {PORT_FACT['text']} [1]", target["call_prep"], "the research needs no model")
        self.assertNotIn("Thursday at 3pm", target["call_prep"], "the thread is not repeated")
        self.assertNotIn("Indexed", target["call_prep"])

    def store_interviewer(self, name="Dana Ortiz", confirmed=True, key=None):
        """A stored look-up for one person, as read_interviewer leaves it."""
        record = {
            "name": name, "email": "dana@chargebot.example", "basis": "mailbox_invitation",
            "evidence": "Sent the calendar invitation \"Student X Chargebot\"", "meeting": "", "others": [],
            "key": key if key is not None else f"{name.casefold()}|",
            "linkedin": {"url": "https://www.linkedin.com/in/danaortiz/", "username": "danaortiz", "confirmed": confirmed,
                         "read_at": "2026-09-28T20:00:00+00:00"},
            "notes": [{"topic": "path", "text": "Built motion planning at Tesla", "quote": "..."}],
        }
        self.conn.execute("UPDATE outreach_targets SET interviewer_json=? WHERE id=?", (json.dumps(record), self.target["id"]))
        self.conn.commit()

    def test_a_record_for_someone_else_is_never_printed_or_sent_after_the_student_names_a_different_person(self):
        self.reply_and_mark()
        store_brief(self.conn, self.target["id"])
        self.store_interviewer("Dana Ortiz")
        update_target(self.conn, self.target["id"], {"interviewer_name": "Riley Park"}, user_id=USER)
        provider = ScriptedProvider([prep_json()])
        notes = self.generate(provider)["call_prep"]
        self.assertIn("RILEY PARK  (You named them)", notes)
        self.assertIn("- LinkedIn not read yet.", notes)
        for gone in ("Dana Ortiz", "Tesla", "dana@chargebot.example"):
            self.assertNotIn(gone, notes)
            self.assertNotIn(gone, provider.prompts[0], "nothing stored about the other person reaches the model")
        self.assertEqual([claim for claim in self.target_claims() if claim["section"] == "interviewer"], [])

    def test_a_lookup_that_times_out_leaves_no_stale_interviewer_in_the_notes(self):
        self.reply_and_mark("replied")
        self.store_interviewer("Dana Ortiz")
        update_target(self.conn, self.target["id"], {"interviewer_name": "Riley Park"}, user_id=USER)

        def times_out(conn, target_id, user_id):
            raise subprocess.TimeoutExpired("mcporter", 30)

        queue_call_prep(self.conn, self.target["id"], user_id=USER, replace=True, reason="t")
        worker = CallPrepWorker(
            self.platform_path, provider_factory=lambda *_: ScriptedProvider([prep_json()]), provider="anthropic", interviewer=times_out,
        )
        worker.run_pending()
        target = get_target(self.conn, self.target["id"], user_id=USER)
        self.assertEqual(target["call_prep_job"]["state"], "succeeded")
        self.assertIn("Riley Park", target["call_prep"])
        self.assertNotIn("Dana Ortiz", target["call_prep"])
        self.assertNotIn("Tesla", target["call_prep"])

    def test_a_question_that_states_more_than_it_cites_is_left_out_by_the_line_check(self):
        self.reply_and_mark("replied")
        store_brief(self.conn, self.target["id"])
        siemens = {"text": "I read that Siemens is your biggest customer: how did you win them?", "from": ["f1"]}
        good = {"text": "What would a first project be?", "from": ["f2"]}
        provider = ScriptedProvider([prep_json(questions=[siemens, good])], refuse=["Siemens"])
        notes = self.generate(provider)["call_prep"]
        self.assertNotIn("Siemens", notes)
        self.assertIn("1. What would a first project be? [2]", notes)
        checked = {item["line"]: item["cites"] for item in provider.line_checks}
        self.assertEqual(checked[siemens["text"]], [PORT_FACT["text"]], "the line check sees what the question cites")
        my = {"text": "My drone work used cameras too: how did you pick a stereo camera?", "from": ["f1"]}
        going = {"text": "Going forward, what would you change about how the arm finds the port?", "from": ["f1"]}
        notes = self.generate(ScriptedProvider([prep_json(questions=[my, going])]))["call_prep"]
        self.assertIn(my["text"], notes, "a question may open on My, Going, or any plain word")
        self.assertIn(going["text"], notes)

    def test_a_question_may_state_a_name_from_the_note_it_cites(self):
        self.reply_and_mark("replied")
        self.store_interviewer("Dana Ortiz")
        update_target(self.conn, self.target["id"], {"interviewer_name": "Dana Ortiz"}, user_id=USER)
        question = {"text": "I saw you built motion planning at Tesla: what made you leave for Chargebot?", "from": ["p1"]}
        provider = ScriptedProvider([prep_json(questions=[question])])
        self.assertIn(question["text"], self.generate(provider)["call_prep"])
        self.assertEqual(len(provider.prompts), 1, "nothing to send back")

    def test_a_question_may_not_use_a_number_from_a_fact_it_does_not_cite(self):
        self.reply_and_mark("replied")
        store_brief(self.conn, self.target["id"])
        from_other_fact = {"text": "What would a first project be at the 90 seconds a plug-in takes?", "from": ["f2"]}
        provider = ScriptedProvider([prep_json(questions=[from_other_fact]), prep_json()])
        notes = self.generate(provider)["call_prep"]
        self.assertIn("states 90", provider.prompts[1])
        self.assertNotIn("a plug-in takes", notes)

    def test_a_number_is_found_only_as_a_whole_number_in_the_inputs_words(self):
        inputs = {
            "technical_research": [{"id": "f1", "section": "technology", "text": "The arm plugs in within 90 seconds"}],
            "replies": [{"logged_on": "2026-09-28", "text": "Could we talk Thursday at 3pm?"}],
            "research_gaps": [{"id": "g1", "text": "why they cut 30 percent of staff"}],
            "source_urls": ["https://news.example/page-28"], "sent_on": "2026-09-28",
        }
        written = "28 engineers, 9 seconds, 90 seconds, 3pm, 30 percent, 2026"
        self.assertEqual(outreach_call_prep._unsupported_numbers(written, inputs), ["28", "9", "30", "2026"])

    def test_a_scheme_less_link_is_not_a_source_of_numbers_and_naming_one_is_not_a_claim(self):
        inputs = {
            "technical_research": [{"id": "f1", "section": "technology", "text": "Their site is acme360.com and the arm plugs in within 90 seconds"}],
            "student_links": ["github.com/t/arm-2024", "linkedin.com/in/t-512"],
        }
        written = "360 engineers, 512 people, 2024 builds, 90 seconds, see github.com/t/arm-2024 or acme360.com/careers"
        self.assertEqual(outreach_call_prep._unsupported_numbers(written, inputs), ["360", "512", "2024"])
        self.assertEqual(outreach_call_prep._unsupported_numbers("Their site: acme360.com, linkedin.com/in/t-512.", inputs), [])
        self.assertEqual(outreach_call_prep._unsupported_numbers("A 3.5 GPA, U.S. only, Ph.D. track, 9 seconds.", inputs), ["3.5", "9"])

    def test_the_sent_date_reaches_the_model_as_a_day_not_a_timestamp(self):
        self.reply_and_mark()
        provider = ScriptedProvider([prep_json()])
        self.generate(provider)
        sent_on = json.loads(provider.prompts[0].split("\n\nYour previous")[0])["sent_on"]
        self.assertRegex(sent_on, r"^\d{4}-\d{2}-\d{2}$")

    def test_a_made_up_number_is_not_hidden_by_a_date_in_the_inputs(self):
        self.reply_and_mark("replied")
        store_brief(self.conn, self.target["id"])
        # 202 is inside the year of every date in the inputs, and 9 inside the 90.
        made_up = {"text": "I read your team grew to 202 engineers and the arm plugs in within 9 seconds: how?", "from": ["f1"]}
        provider = ScriptedProvider([prep_json(questions=[made_up]), prep_json()])
        notes = self.generate(provider)["call_prep"]
        self.assertIn("states 202, 9", provider.prompts[1], "sent back")
        self.assertNotIn("grew to", notes)
        point = prep_json(talking_points=[{"text": "Led 202 engineers", "basis": "profile:experience"}])
        provider = ScriptedProvider([point, point])
        with self.assertRaisesRegex(CallPrepRejected, "numbers found in neither"):
            self.generate(provider)

    def test_a_competitor_only_the_agent_picked_is_never_called_one(self):
        self.reply_and_mark()
        rival = {
            "section": "competitors", "text": "Voltarm sells charging robots with lidar", "source_url": "https://news.example/voltarm",
            "quote": "Voltarm builds charging robots with lidar", "person": "", "competitor": "Voltarm", "checked": True,
            "note": "picked as a competitor by the research agent",
        }
        store_brief(self.conn, self.target["id"], brief={**BRIEF, "facts": [*BRIEF["facts"], rival]})
        states = {"text": "I read that you compete with Voltarm: how do you beat their lidar?", "from": ["f3"]}
        asks = {"text": "I read that Voltarm sells charging robots with lidar: how did you choose a camera instead?", "from": ["f3"]}
        reading = [
            {"about": "selling_point", "text": "a camera where Voltarm uses lidar", "facts": ["f1", "f3"]},
            {"about": "direction", "text": "rival to Voltarm", "facts": ["f3"]},
        ]
        provider = ScriptedProvider([prep_json(questions=[states, asks], reading=reading)] * 2)
        notes = self.generate(provider)["call_prep"]
        prompt = provider.prompts[0]
        self.assertIn('"competitor_basis"', prompt, "the model is told it is only a pick")
        self.assertIn("calls a company a competitor", provider.prompts[1])
        self.assertNotIn("you compete with Voltarm", notes)
        self.assertNotIn("rival to Voltarm", notes)
        self.assertIn(f"1. {asks['text']} [4; the research agent's pick of a competitor]", notes)
        self.assertIn("- Edge: a camera where Voltarm uses lidar [1, 4; 4 is the research agent's pick of a competitor]", notes)

    def test_notes_from_a_profile_not_confirmed_as_them_are_marked_wherever_they_appear(self):
        self.reply_and_mark()
        store_brief(self.conn, self.target["id"])
        self.store_interviewer("Dana Ortiz", confirmed=False)
        update_target(self.conn, self.target["id"], {"interviewer_name": "Dana Ortiz"}, user_id=USER)
        provider = ScriptedProvider([prep_json()])
        notes = self.generate(provider)["call_prep"]
        marked = "Before: Built motion planning at Tesla (profile not confirmed as them)"
        self.assertIn(f"- {marked}\n", notes)
        claims = [claim for claim in self.target_claims() if claim["section"] == "interviewer"]
        self.assertEqual([claim["text"] for claim in claims], [marked])
        self.assertNotIn("Tesla", provider.prompts[0])

    def test_a_usable_first_draft_is_kept_when_the_retry_is_worse(self):
        self.reply_and_mark()
        store_brief(self.conn, self.target["id"])
        bad_reading = [{"about": "direction", "text": "within 45 seconds", "facts": ["f1"]}]
        worse = prep_json(talking_points=[{"text": "Flew 900 hours", "basis": "profile:experience"}])
        provider = ScriptedProvider([prep_json(reading=bad_reading), worse])
        notes = self.generate(provider)["call_prep"]
        self.assertEqual(len(provider.prompts), 2, "the retry was tried")
        self.assertIn("TALKING POINTS", notes)
        self.assertNotIn("900", notes)
        self.assertNotIn("45 seconds", notes, "the first notes stand without the reading line that went too far")

    def test_a_question_without_text_is_skipped_not_printed_as_none(self):
        self.reply_and_mark()
        store_brief(self.conn, self.target["id"])
        questions = [{"from": ["f1"]}, {"text": None, "from": ["f1"]}, {"text": "What would a first project be?", "from": ["f2"]}]
        notes = self.generate(ScriptedProvider([prep_json(questions=questions)]))["call_prep"]
        self.assertNotIn("None", notes)
        self.assertIn("1. What would a first project be? [2]", notes)

    def test_a_from_or_facts_that_is_not_a_list_is_handled_not_a_crash(self):
        self.reply_and_mark()
        store_brief(self.conn, self.target["id"])
        questions = [{"text": "What would a first project be?", "from": "f2"}]
        reading = [{"about": "direction", "text": "camera-guided plug-in", "facts": 1}, {"about": "selling_point", "text": "camera", "facts": "f1"}]
        notes = self.generate(ScriptedProvider([prep_json(questions=questions, reading=reading)] * 2))["call_prep"]
        self.assertIn("1. What would a first project be? [2]", notes, "one id in place of a list still counts")
        self.assertIn("- Edge: camera [1]", notes)
        self.assertNotIn("camera-guided", notes)
        lone = prep_json(questions=[{"text": "How?", "from": 1}])
        with self.assertRaises(CallPrepRejected):
            self.generate(ScriptedProvider([lone, lone]))

    def test_placeholders_left_in_the_example_file_are_not_the_students_words(self):
        example = Path(__file__).resolve().parent.parent / "config" / "call_prep.local.example.json"
        questions, note = outreach_call_questions.standing_questions(example)
        self.assertEqual(questions[0]["lead_in"], "", "the lead-in is dropped")
        self.assertEqual(questions[0]["ask"], "What's the use case of the project I'd be working on?", "the question is still asked")
        self.assertEqual(questions[1]["ask"], "Where are you looking to expand?")
        self.assertIn("<placeholders> in a lead-in; fill it in or remove it", note)
        self.questions_file.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
        self.reply_and_mark()
        provider = ScriptedProvider([prep_json()])
        notes = self.generate(provider)["call_prep"]
        self.assertNotIn("<where you worked>", notes)
        self.assertNotIn("<where you worked>", provider.prompts[0])
        self.assertIn("(call_prep.local.json still has <placeholders> in a lead-in; fill it in or remove it.)", notes)
        self.assertIn(". What's the use case of the project I'd be working on?\n", notes)

    def test_a_question_or_blank_with_a_placeholder_is_left_out_too(self):
        self.questions_file.write_text(json.dumps({"questions": [
            {"ask": "What did <name> build?", "blank": "Notes:"},
            {"ask": "Where are you looking to expand?", "blank": "About <topic>:"},
        ]}), encoding="utf-8")
        questions, note = outreach_call_questions.standing_questions(self.questions_file)
        self.assertEqual([(item["ask"], item["blank"]) for item in questions], [("Where are you looking to expand?", "")])
        self.assertIn("<placeholders>", note)

    def test_a_line_the_second_read_did_not_answer_is_left_out_not_printed_as_checked(self):
        self.reply_and_mark()
        store_brief(self.conn, self.target["id"])
        provider = ScriptedProvider([prep_json()], silent=["What would a first project be?"])
        notes = self.generate(provider)["call_prep"]
        self.assertIn("I read that the arm finds the port with a camera", notes, "the answered line stands")
        self.assertNotIn("What would a first project be?", notes, "nothing read it against its facts")
        self.assertIn("TALKING POINTS", notes, "the notes stand without it")

    def test_the_students_own_question_and_the_sent_emails_points_do_not_depend_on_the_second_read(self):
        self.questions_file.write_text(json.dumps({"questions": [{"ask": "Where are you short-staffed?", "research": ["hiring"]}]}), encoding="utf-8")
        self.reply_and_mark()
        store_brief(self.conn, self.target["id"])
        provider = ScriptedProvider([prep_json()], silent=["Where are you short-staffed?", "Cut our drone"])
        notes = self.generate(provider)["call_prep"]
        self.assertIn("Where are you short-staffed?", notes)
        self.assertIn("Cut our drone's weight from 1.5 kg to 500 g", notes)

    def test_a_pick_named_beside_competitor_is_caught_whatever_fact_the_line_cites(self):
        self.reply_and_mark()
        rival = {
            "section": "competitors", "text": "Voltarm sells charging robots with lidar", "source_url": "https://news.example/voltarm",
            "quote": "Voltarm builds charging robots with lidar", "person": "", "competitor": "Voltarm", "checked": True,
            "note": "picked as a competitor by the research agent",
        }
        store_brief(self.conn, self.target["id"], brief={**BRIEF, "facts": [*BRIEF["facts"], rival]})
        cites_another = {"text": "I read the arm uses a camera: is Voltarm a competitor you watch?", "from": ["f1"]}
        clean = {"text": "What would a first project be?", "from": ["f2"]}
        reading = [{"about": "direction", "text": "Voltarm is their main competitor", "facts": ["f1"]}]
        provider = ScriptedProvider([prep_json(questions=[cites_another, clean], reading=reading)] * 2)
        notes = self.generate(provider)["call_prep"]
        self.assertIn("calls a company a competitor", provider.prompts[1])
        self.assertNotIn("competitor you watch", notes)
        self.assertNotIn("main competitor", notes)
        self.assertIn("What would a first project be?", notes)

    def test_a_hook_built_only_on_what_the_web_did_not_say_must_ask_not_state(self):
        self.reply_and_mark()
        store_brief(self.conn, self.target["id"])
        standing = [
            {"id": "s1", "hook": "I know you have no motor supplier yet.", "ask": "", "from": ["g1"]},
            {"id": "s2", "hook": "I could not find which motors the arm uses.", "ask": "Which motors does it use?", "from": ["g1"]},
        ]
        self.questions_file.write_text(json.dumps({"questions": [
            {"ask": "Where do you need hands most?", "research": ["hiring"]},
            {"ask": "What is the bottleneck?", "research": ["growth"]},
        ]}), encoding="utf-8")
        provider = ScriptedProvider([prep_json(standing=standing)] * 2)
        notes = self.generate(provider)["call_prep"]
        self.assertIn("goes past what it builds on", provider.prompts[1])
        self.assertNotIn("no motor supplier", notes, "a guess about what the web did not say is not printed as a hook")
        gap_question = {"text": "You have no motor supplier yet, right?", "from": ["g1"]}
        asks = {"text": "Which motors does the arm use?", "from": ["g1"]}
        provider = ScriptedProvider([prep_json(questions=[gap_question, asks])] * 2)
        notes = self.generate(provider)["call_prep"]
        self.assertNotIn("no motor supplier yet", notes, "a statement in question form built only on a gap is left out")
        self.assertIn("Which motors does the arm use?", notes, "a real question on a gap stands")


class FailingProvider:
    """A model call cut off mid-flight, as a sleeping laptop cuts one off."""

    name = "anthropic"
    model = "test-model"

    def create(self, **_kwargs):
        raise RuntimeError("connection reset")


class CallPrepJobTests(unittest.TestCase):
    """Background jobs: automatic starts, retries, and recovery after a restart."""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        patcher = mock.patch.object(outreach_call_questions, "QUESTIONS_PATH", Path(self.tempdir.name) / "none.json")
        patcher.start()
        self.addCleanup(patcher.stop)
        _, self.platform_path = build_and_migrate(Path(self.tempdir.name))
        self.conn = connect_product(self.platform_path)
        ensure_product_schema(self.conn)
        confirm_facts(self.conn, name="Test Student", school="UT Austin", experience=EXPERIENCE)
        self.target = create_target(self.conn, {
            "company": "Chargebot", "summary": "Robots that charge parked EVs", "contact_email": "hi@chargebot.example",
        }, user_id=USER)
        update_target(self.conn, self.target["id"], {"status": "sent"}, user_id=USER)
        self.provider = ScriptedProvider([prep_json(), prep_json()])

    def tearDown(self):
        self.conn.close()
        self.tempdir.cleanup()

    def worker(self, provider=None, researcher=None):
        return CallPrepWorker(
            self.platform_path, provider_factory=lambda *_: provider or self.provider, provider="anthropic", researcher=researcher,
        )

    def researcher(self, fail=None):
        """Stands in for the web research: records each company it was asked about and stores a brief."""
        calls = []

        def research(conn, target_id, user_id):
            calls.append(target_id)
            conn.execute("UPDATE outreach_targets SET tech_brief_tried_at=? WHERE id=?", (datetime.now(timezone.utc).isoformat(), target_id))
            conn.commit()
            if fail:
                conn.execute("UPDATE outreach_targets SET tech_brief_error=? WHERE id=?", (fail, target_id))
                conn.commit()
                raise RuntimeError(fail)
            store_brief(conn, target_id, at=datetime.now(timezone.utc).isoformat())

        return research, calls

    def target_now(self):
        return get_target(self.conn, self.target["id"], user_id=USER)

    def test_the_web_app_and_maintenance_worker_share_one_job_type_name(self):
        self.assertEqual(WEB_APP_JOB_TYPES, JOB_TYPES)

    def test_a_company_without_recent_research_is_researched_before_its_notes(self):
        self.replied()
        researcher, calls = self.researcher()
        auto_queue_call_prep(self.conn, self.target["id"], user_id=USER, reason="t")
        self.worker(researcher=researcher).run_pending()
        self.assertEqual(calls, [self.target["id"]])
        notes = self.target_now()["call_prep"]
        self.assertIn(f"- {PORT_FACT['text']} [1]", notes)
        self.assertIn(PORT_FACT["text"], self.provider.prompts[0], "the notes were written from the new research")

    def test_fresh_research_is_not_done_again(self):
        self.replied()
        store_brief(self.conn, self.target["id"], at=datetime.now(timezone.utc).isoformat())
        researcher, calls = self.researcher()
        queue_call_prep(self.conn, self.target["id"], user_id=USER, replace=True, reason="t")
        self.worker(researcher=researcher).run_pending()
        self.assertEqual(calls, [])
        self.assertIn(PORT_FACT["text"], self.target_now()["call_prep"])

    def test_research_that_fails_never_holds_up_the_notes(self):
        self.replied()
        researcher, calls = self.researcher(fail="Claude Code exited 1: not signed in")
        queue_call_prep(self.conn, self.target["id"], user_id=USER, replace=True, reason="t")
        self.worker(researcher=researcher).run_pending()
        target = self.target_now()
        self.assertEqual((calls, target["call_prep_job"]["state"]), ([self.target["id"]], "succeeded"))
        self.assertIn("No web research yet: Claude Code exited 1: not signed in.", target["call_prep"])

    def test_a_retried_job_does_not_research_again(self):
        self.replied()
        researcher, calls = self.researcher(fail="connection reset")
        queue_call_prep(self.conn, self.target["id"], user_id=USER, replace=True, reason="t")
        self.worker(FailingProvider(), researcher=researcher).run_pending()
        self.assertEqual(self.target_now()["call_prep_job"]["state"], "retry")
        self.a_minute_passes()
        self.worker(researcher=researcher).run_pending()
        self.assertEqual(calls, [self.target["id"]], "one search, however many tries the notes take")
        self.assertEqual(self.target_now()["call_prep_job"]["state"], "succeeded")

    def test_an_automatic_job_on_a_decline_does_not_research(self):
        self.replied()
        self.conn.execute(
            "UPDATE outreach_targets SET reply_suggestion_json=? WHERE id=?",
            (json.dumps({"status": "declined", "reason": "They said no"}), self.target["id"]),
        )
        self.conn.commit()
        researcher, calls = self.researcher()
        auto_queue_call_prep(self.conn, self.target["id"], user_id=USER, reason="Reply found in Gmail")
        self.worker(researcher=researcher).run_pending()
        self.assertEqual(calls, [], "a no needs no research")
        self.assertIn("ABOUT THEM", self.target_now()["call_prep"])
        queue_call_prep(self.conn, self.target["id"], user_id=USER, replace=True, reason="You asked for new call prep")
        self.worker(researcher=researcher).run_pending()
        self.assertEqual(calls, [self.target["id"]], "asking for new notes still researches")

    def test_a_pause_during_research_holds_the_notes(self):
        self.replied()
        auto_queue_call_prep(self.conn, self.target["id"], user_id=USER, reason="Reply logged")
        researched, calls = self.researcher()

        def research_then_pause(conn, target_id, user_id):
            researched(conn, target_id, user_id)
            automation.set_paused(conn, user_id, True)

        self.worker(researcher=research_then_pause).run_pending()
        self.assertEqual(self.provider.prompts, [], "the reply does not go to the model once paused")
        self.assertEqual(self.job_row()["state"], "queued")
        self.assertEqual(len(self.target_now()["tech_brief"]["facts"]), 3, "the research is kept")
        automation.set_paused(self.conn, USER, False)
        self.a_minute_passes()
        self.worker(researcher=research_then_pause).run_pending()
        self.assertEqual(calls, [self.target["id"]], "and not done again")
        self.assertIn(PORT_FACT["text"], self.target_now()["call_prep"])

    def test_an_automatic_job_that_will_not_write_does_not_research(self):
        self.replied()
        auto_queue_call_prep(self.conn, self.target["id"], user_id=USER, reason="t")
        update_target(self.conn, self.target["id"], {"call_prep": "My own notes"}, user_id=USER)
        researcher, calls = self.researcher()
        self.worker(researcher=researcher).run_pending()
        self.assertEqual(calls, [], "no search spent on notes that stay as they are")

    def test_nothing_is_researched_while_paused(self):
        self.replied()
        automation.set_paused(self.conn, USER, True)
        auto_queue_call_prep(self.conn, self.target["id"], user_id=USER, reason="Reply logged")
        researcher, calls = self.researcher()
        self.worker(researcher=researcher).run_pending()
        self.assertEqual((calls, self.provider.prompts), ([], []))

    def test_research_the_student_asked_for_runs_while_paused(self):
        automation.set_paused(self.conn, USER, True)
        researcher, calls = self.researcher()
        queue_research(self.conn, self.target["id"], user_id=USER, reason="You asked for research")
        self.assertEqual(self.worker(researcher=researcher).run_pending(), 1)
        target = self.target_now()
        self.assertEqual((calls, target["tech_brief_job"]["state"]), ([self.target["id"]], "succeeded"))
        self.assertEqual(len(target["tech_brief"]["facts"]), 3)

    def test_a_research_job_that_fails_is_retried(self):
        researcher, _ = self.researcher(fail="connection reset")
        queue_research(self.conn, self.target["id"], user_id=USER, reason="t")
        self.worker(researcher=researcher).run_pending()
        job = self.target_now()["tech_brief_job"]
        self.assertEqual((job["state"], job["attempts"]), ("retry", 1))
        self.assertIn("connection reset", job["error"])

    def test_a_reply_and_a_reply_status_start_it_once(self):
        self.assertFalse(auto_queue_call_prep(self.conn, self.target["id"], user_id=USER, reason="t"), "not replied yet")
        update_target(self.conn, self.target["id"], {"status": "replied"}, user_id=USER)
        self.assertFalse(auto_queue_call_prep(self.conn, self.target["id"], user_id=USER, reason="t"), "no reply logged")
        log_reply(self.conn, self.target["id"], REPLY, user_id=USER)
        self.assertTrue(auto_queue_call_prep(self.conn, self.target["id"], user_id=USER, reason="t"))
        self.assertFalse(auto_queue_call_prep(self.conn, self.target["id"], user_id=USER, reason="t"), "one job at a time")
        self.assertEqual(self.target_now()["call_prep_job"]["state"], "queued")
        self.assertEqual(self.worker().run_pending(), 1)
        target = self.target_now()
        self.assertEqual(target["call_prep_job"]["state"], "succeeded")
        self.assertIn("TALKING POINTS", target["call_prep"])
        self.assertFalse(auto_queue_call_prep(self.conn, self.target["id"], user_id=USER, reason="t"), "notes exist")

    def test_a_cut_off_model_call_is_retried(self):
        update_target(self.conn, self.target["id"], {"status": "call_scheduled"}, user_id=USER)
        log_reply(self.conn, self.target["id"], REPLY, user_id=USER)
        queue_call_prep(self.conn, self.target["id"], user_id=USER, replace=True, reason="t")
        self.worker(FailingProvider()).run_pending()
        job = self.target_now()["call_prep_job"]
        self.assertEqual((job["state"], job["attempts"]), ("retry", 1))
        self.assertIn("connection reset", job["error"])
        self.assertEqual(self.worker().run_pending(), 0, "the retry waits for its backoff")
        self.conn.execute("UPDATE job_queue SET next_attempt_at='2000-01-01T00:00:00+00:00' WHERE job_type=?", (JOB_TYPE,))
        self.conn.commit()
        self.worker().run_pending()
        target = self.target_now()
        self.assertEqual(target["call_prep_job"]["state"], "succeeded")
        self.assertIn("TALKING POINTS", target["call_prep"])

    def test_a_job_cut_off_by_a_restart_runs_again(self):
        update_target(self.conn, self.target["id"], {"status": "replied"}, user_id=USER)
        log_reply(self.conn, self.target["id"], REPLY, user_id=USER)
        queue_call_prep(self.conn, self.target["id"], user_id=USER, replace=True, reason="t")
        # The server died mid-generation: the row still says running.
        self.conn.execute("UPDATE job_queue SET state='running', locked_at='2026-01-01T00:00:00+00:00' WHERE job_type=?", (JOB_TYPE,))
        self.conn.commit()
        worker = self.worker()
        self.assertEqual(worker.recover_interrupted(), 1)
        worker.run_pending()
        self.assertIn("TALKING POINTS", self.target_now()["call_prep"])

    def test_an_automatic_job_keeps_notes_saved_while_it_waited(self):
        update_target(self.conn, self.target["id"], {"status": "replied"}, user_id=USER)
        log_reply(self.conn, self.target["id"], REPLY, user_id=USER)
        auto_queue_call_prep(self.conn, self.target["id"], user_id=USER, reason="t")
        update_target(self.conn, self.target["id"], {"call_prep": "My own notes"}, user_id=USER)
        self.worker().run_pending()
        self.assertEqual(self.target_now()["call_prep"], "My own notes")
        self.assertEqual(self.provider.prompts, [])

    def test_a_job_whose_company_left_a_reply_status_writes_nothing(self):
        update_target(self.conn, self.target["id"], {"status": "replied"}, user_id=USER)
        log_reply(self.conn, self.target["id"], REPLY, user_id=USER)
        queue_call_prep(self.conn, self.target["id"], user_id=USER, replace=True, reason="t")
        update_target(self.conn, self.target["id"], {"status": "declined"}, user_id=USER)
        self.worker().run_pending()
        target = self.target_now()
        self.assertEqual((target["call_prep_job"]["state"], target["call_prep"]), ("succeeded", ""))

    def test_the_thread_runs_jobs_it_is_woken_for(self):
        update_target(self.conn, self.target["id"], {"status": "replied"}, user_id=USER)
        log_reply(self.conn, self.target["id"], REPLY, user_id=USER)
        worker = self.worker()
        worker.start()
        # Stopped here, not in a cleanup: cleanups run after tearDown, and
        # Windows cannot delete the database while the thread holds it open.
        try:
            queue_call_prep(self.conn, self.target["id"], user_id=USER, replace=True, reason="t")
            worker.wake()
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and not self.target_now()["call_prep"]:
                time.sleep(0.05)
        finally:
            worker.stop()
        self.assertIn("TALKING POINTS", self.target_now()["call_prep"])

    def replied(self):
        update_target(self.conn, self.target["id"], {"status": "replied"}, user_id=USER)
        log_reply(self.conn, self.target["id"], REPLY, user_id=USER)

    def interviewer_lookup(self):
        calls = []

        def lookup(conn, target_id, user_id):
            calls.append(target_id)

        return lookup, calls

    def test_an_automatic_job_on_a_pasted_decline_researches_nothing_and_reads_no_linkedin(self):
        update_target(self.conn, self.target["id"], {"status": "replied"}, user_id=USER)
        pasted = log_reply(
            self.conn, self.target["id"],
            "Thank you for your interest, but we are not hiring interns and will not be moving forward with your application.",
            user_id=USER,
        )
        self.assertEqual(self.target_now().get("reply_suggestion") or {}, {}, "the pasted reply's suggestion is not kept on the company")
        self.assertEqual(pasted["suggestion"]["status"], "declined")
        researcher, calls = self.researcher()
        lookup, looked = self.interviewer_lookup()
        auto_queue_call_prep(self.conn, self.target["id"], user_id=USER, reason="Reply pasted")
        CallPrepWorker(
            self.platform_path, provider_factory=lambda *_: self.provider, provider="anthropic", researcher=researcher, interviewer=lookup,
        ).run_pending()
        self.assertEqual((calls, looked), ([], []), "a no needs neither a web search nor a LinkedIn read")
        # Asking for notes is the student's own act, and still researches.
        queue_call_prep(self.conn, self.target["id"], user_id=USER, replace=True, reason="You asked for new call prep")
        CallPrepWorker(
            self.platform_path, provider_factory=lambda *_: self.provider, provider="anthropic", researcher=researcher, interviewer=lookup,
        ).run_pending()
        self.assertEqual(calls, [self.target["id"]])

    def test_a_company_marked_declined_during_research_is_not_read_on_linkedin(self):
        self.replied()
        researched, calls = self.researcher()
        lookup, looked = self.interviewer_lookup()

        def research_then_decline(conn, target_id, user_id):
            researched(conn, target_id, user_id)
            update_target(conn, target_id, {"status": "declined"}, user_id=user_id)

        queue_call_prep(self.conn, self.target["id"], user_id=USER, replace=True, reason="t")
        CallPrepWorker(
            self.platform_path, provider_factory=lambda *_: self.provider, provider="anthropic",
            researcher=research_then_decline, interviewer=lookup,
        ).run_pending()
        target = self.target_now()
        self.assertEqual((calls, looked), ([self.target["id"]], []))
        self.assertEqual((target["call_prep_job"]["state"], target["call_prep"]), ("succeeded", ""))
        self.assertEqual(self.provider.prompts, [])

    def test_a_research_job_cut_off_by_a_restart_is_not_run_again_on_its_own(self):
        researcher, calls = self.researcher()
        queue_research(self.conn, self.target["id"], user_id=USER, reason="You asked for research")
        self.conn.execute("UPDATE outreach_targets SET tech_brief_tried_at=? WHERE id=?",
                          (datetime.now(timezone.utc).isoformat(), self.target["id"]))
        # The server died mid-search: the row still says running, and its day's try was spent.
        self.conn.execute("UPDATE job_queue SET state='running', locked_at='2026-01-01T00:00:00+00:00' WHERE job_type=?", (outreach_call_prep.research.JOB_TYPE,))
        self.conn.commit()
        worker = self.worker(researcher=researcher)
        self.assertEqual(worker.recover_interrupted(), 1)
        self.conn.execute("UPDATE job_queue SET next_attempt_at='2000-01-01T00:00:00+00:00'")
        self.conn.commit()
        worker.run_pending()
        self.assertEqual(calls, [], "the search it cut off is not started again")
        self.assertIn("cut off when the app stopped", self.target_now()["tech_brief_error"])
        # Pressing the button again is the student's own act, and runs.
        queue_research(self.conn, self.target["id"], user_id=USER, reason="You asked for research")
        worker.run_pending()
        self.assertEqual(calls, [self.target["id"]])

    def test_research_is_not_queued_by_a_click_while_call_prep_researches_inline(self):
        self.replied()
        self.conn.execute("UPDATE outreach_targets SET tech_brief_tried_at=? WHERE id=?",
                          (datetime.now(timezone.utc).isoformat(), self.target["id"]))
        self.conn.commit()
        queue_call_prep(self.conn, self.target["id"], user_id=USER, replace=True, reason="t")
        self.conn.execute("UPDATE job_queue SET state='running' WHERE job_type=?", (JOB_TYPE,))
        self.conn.commit()
        self.assertEqual(self.target_now()["call_prep_job"]["state"], "running")
        queue_research(self.conn, self.target["id"], user_id=USER, reason="You asked for research")
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM job_queue WHERE job_type=?", (outreach_call_prep.research.JOB_TYPE,)).fetchone()[0], 0,
        )

    def job_row(self):
        return self.conn.execute("SELECT state, attempts, last_error, payload_json FROM job_queue WHERE job_type=?", (JOB_TYPE,)).fetchone()

    def a_minute_passes(self):
        self.conn.execute("UPDATE job_queue SET next_attempt_at='2000-01-01T00:00:00+00:00' WHERE job_type=?", (JOB_TYPE,))
        self.conn.commit()

    def test_a_job_the_app_started_waits_while_paused_and_runs_after_resume(self):
        self.replied()
        automation.set_paused(self.conn, USER, True)
        self.assertTrue(auto_queue_call_prep(self.conn, self.target["id"], user_id=USER, reason="Reply found in Gmail"),
                        "queued even while paused, so it is not forgotten")
        self.worker().run_pending()
        self.assertEqual(self.provider.prompts, [], "nothing went to the model while paused")
        job = self.job_row()
        self.assertEqual((job["state"], job["attempts"], job["last_error"]), ("queued", 0, PAUSED_WAIT),
                         "held in line: not failed, and no try used up")
        self.assertEqual(self.target_now()["call_prep"], "")
        self.a_minute_passes()
        self.worker().run_pending()
        self.assertEqual(self.provider.prompts, [], "still paused")
        automation.set_paused(self.conn, USER, False)
        self.a_minute_passes()
        self.worker().run_pending()
        target = self.target_now()
        self.assertEqual(target["call_prep_job"]["state"], "succeeded")
        self.assertIn("TALKING POINTS", target["call_prep"])

    def test_call_prep_the_student_asked_for_runs_while_paused(self):
        self.replied()
        automation.set_paused(self.conn, USER, True)
        queue_call_prep(self.conn, self.target["id"], user_id=USER, replace=True, reason="You asked for new call prep")
        self.worker().run_pending()
        self.assertEqual(self.target_now()["call_prep_job"]["state"], "succeeded", "a click is the student's own act")
        self.assertEqual(len(self.provider.prompts), 1)

    def test_asking_while_an_automatic_job_waits_makes_it_the_students(self):
        self.replied()
        automation.set_paused(self.conn, USER, True)
        auto_queue_call_prep(self.conn, self.target["id"], user_id=USER, reason="Reply logged")
        self.worker().run_pending()
        self.assertEqual(self.job_row()["state"], "queued")
        queue_call_prep(self.conn, self.target["id"], user_id=USER, replace=True, reason="You asked for new call prep")
        self.assertEqual(json.loads(self.job_row()["payload_json"])["automatic"], False)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM job_queue WHERE job_type=?", (JOB_TYPE,)).fetchone()[0], 1, "one job")
        self.worker().run_pending()
        self.assertIn("TALKING POINTS", self.target_now()["call_prep"])

    def test_jobs_queued_before_their_origin_was_recorded_are_told_apart_by_replace(self):
        self.assertTrue(is_automatic({"replace": False}))
        self.assertFalse(is_automatic({"replace": True}))
        self.assertFalse(is_automatic({"replace": False, "automatic": False}))
        self.assertTrue(is_automatic({"replace": True, "automatic": True}))

    def test_call_prep_waits_for_the_research_the_student_already_queued(self):
        self.replied()
        researcher, calls = self.researcher()
        queue_call_prep(self.conn, self.target["id"], user_id=USER, replace=True, reason="t")
        queue_research(self.conn, self.target["id"], user_id=USER, reason="You asked for research")
        worker = self.worker(researcher=researcher)
        worker.run_pending()
        self.assertEqual(calls, [self.target["id"]], "the research ran once")
        self.assertEqual(self.target_now()["tech_brief_job"]["state"], "succeeded")
        self.assertEqual(self.target_now()["call_prep"], "", "the notes did not go out ahead of it")
        self.assertEqual(self.job_row()["state"], "queued")
        self.assertEqual(self.provider.prompts, [])
        self.a_minute_passes()
        worker.run_pending()
        self.assertEqual(calls, [self.target["id"]], "and is not done again")
        notes = self.target_now()["call_prep"]
        self.assertIn(f"- {PORT_FACT['text']} [1]", notes)
        self.assertNotIn("No web research yet", notes)
        self.assertIn(PORT_FACT["text"], self.provider.prompts[0])

    def test_research_already_queued_does_not_hold_notes_when_the_brief_is_fresh(self):
        self.replied()
        store_brief(self.conn, self.target["id"], at=datetime.now(timezone.utc).isoformat())
        researcher, calls = self.researcher()
        queue_call_prep(self.conn, self.target["id"], user_id=USER, replace=True, reason="t")
        queue_research(self.conn, self.target["id"], user_id=USER, reason="You asked for research")
        self.worker(researcher=researcher).run_pending()
        self.assertIn(PORT_FACT["text"], self.target_now()["call_prep"], "written at once from the fresh brief")

    def test_the_two_workers_never_take_each_others_jobs(self):
        enqueue_job(self.conn, "someone_elses_job", {}, "other-job")
        self.assertIsNone(run_next_job(self.conn, {JOB_TYPE: lambda _payload: None}, only_handled=True))
        state = self.conn.execute("SELECT state FROM job_queue WHERE idempotency_key='other-job'").fetchone()[0]
        self.assertEqual(state, "queued")
        self.conn.execute("DELETE FROM job_queue")
        self.conn.commit()
        update_target(self.conn, self.target["id"], {"status": "replied"}, user_id=USER)
        log_reply(self.conn, self.target["id"], REPLY, user_id=USER)
        queue_call_prep(self.conn, self.target["id"], user_id=USER, replace=True, reason="t")
        self.assertIsNone(run_next_job(self.conn, {}, exclude_types=WEB_APP_JOB_TYPES), "the maintenance worker leaves it")
        self.assertEqual(self.target_now()["call_prep_job"]["state"], "queued")


class CallPrepApiTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        _, self.platform_path = build_and_migrate(root)
        self.provider = ScriptedProvider([prep_json(), prep_json()])
        app = create_app(
            db_path=self.platform_path,
            access_token="drafting-owner",
            static_dir=STATIC_DIR,
            resume_storage=root / "resumes",
            capture_storage=root / "captures",
            interview_storage=root / "interviews",
            agent_provider_factory=lambda *_: self.provider,
            outreach_draft_provider="anthropic",
        )
        self.client = TestClient(app)
        self.client.__enter__()
        with closing(connect_product(self.platform_path)) as conn:
            confirm_facts(conn, name="Test Student", school="UT Austin", experience=EXPERIENCE)

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.tempdir.cleanup()

    def run_jobs(self):
        self.client.app.state.call_prep_worker.run_pending()

    def test_write_edit_and_refuse_before_a_reply(self):
        created = self.client.post("/api/v1/outreach", headers=AUTH, json={
            "company": "Chargebot", "summary": "Robots that charge parked EVs", "contact_email": "hi@chargebot.example",
        }).json()
        early = self.client.post(f"/api/v1/outreach/{created['id']}/call-prep", headers=AUTH)
        self.assertEqual(early.status_code, 422, early.text)
        moved = self.client.patch(f"/api/v1/outreach/{created['id']}", headers=AUTH, json={"status": "replied"}).json()
        self.assertIsNone(moved["call_prep_job"], "nothing starts without a reply")
        self.assertEqual(moved["reply_count"], 0)
        no_reply = self.client.post(f"/api/v1/outreach/{created['id']}/call-prep", headers=AUTH)
        self.assertEqual(no_reply.status_code, 409, no_reply.text)
        self.assertIn("Paste their reply", no_reply.json()["detail"])

        logged = self.client.post(f"/api/v1/outreach/{created['id']}/reply", headers=AUTH, json={"text": REPLY}).json()
        self.assertEqual(logged["target"]["call_prep_job"]["state"], "queued", "logging the reply starts it")
        self.run_jobs()
        written = self.client.get(f"/api/v1/outreach/{created['id']}", headers=AUTH).json()
        self.assertIn("TALKING POINTS", written["call_prep"])
        self.assertEqual(written["call_prep_job"]["state"], "succeeded")

        again = self.client.post(f"/api/v1/outreach/{created['id']}/call-prep", headers=AUTH)
        self.assertEqual(again.status_code, 202, again.text)
        self.assertEqual(again.json()["call_prep_job"]["state"], "queued")
        edited = self.client.patch(f"/api/v1/outreach/{created['id']}", headers=AUTH, json={"call_prep": "Talked to the CTO\r\n- next: send a thank-you"})
        self.assertEqual(edited.json()["call_prep"], "Talked to the CTO\n- next: send a thank-you")
        missing = self.client.post("/api/v1/outreach/outreach-nope/call-prep", headers=AUTH)
        self.assertEqual(missing.status_code, 404)

    def test_moving_to_a_reply_status_with_a_reply_starts_it(self):
        created = self.client.post("/api/v1/outreach", headers=AUTH, json={
            "company": "Chargebot", "summary": "Robots that charge parked EVs", "contact_email": "hi@chargebot.example",
        }).json()
        self.client.patch(f"/api/v1/outreach/{created['id']}", headers=AUTH, json={"status": "sent"})
        logged = self.client.post(f"/api/v1/outreach/{created['id']}/reply", headers=AUTH, json={"text": REPLY}).json()
        self.assertIsNone(logged["target"]["call_prep_job"], "still at Sent: no call prep yet")
        moved = self.client.patch(f"/api/v1/outreach/{created['id']}", headers=AUTH, json={"status": "call_scheduled"}).json()
        self.assertEqual(moved["call_prep_job"]["state"], "queued")
        self.assertEqual(moved["reply_count"], 1)

    def test_research_is_refused_where_it_cannot_run(self):
        created = self.client.post("/api/v1/outreach", headers=AUTH, json={"company": "Chargebot", "website": "https://chargebot.example"}).json()
        refused = self.client.post(f"/api/v1/outreach/{created['id']}/research", headers=AUTH)
        self.assertEqual(refused.status_code, 409, refused.text)
        self.assertIsNone(self.client.get(f"/api/v1/outreach/{created['id']}", headers=AUTH).json()["tech_brief_job"], "no job that cannot run")
        self.assertEqual(self.client.get("/api/v1/outreach", headers=AUTH).json()["company_research"], {"available": False})

    def test_research_is_queued_for_the_worker(self):
        calls = []

        def researcher(conn, target_id, user_id):
            calls.append(target_id)

        worker = CallPrepWorker(self.platform_path, provider_factory=lambda *_: self.provider, provider="anthropic", researcher=researcher)
        app = create_app(
            db_path=self.platform_path, access_token="drafting-owner", static_dir=STATIC_DIR,
            resume_storage=Path(self.tempdir.name) / "resumes", capture_storage=Path(self.tempdir.name) / "captures",
            interview_storage=Path(self.tempdir.name) / "interviews", agent_provider_factory=lambda *_: self.provider,
            outreach_draft_provider="anthropic", call_prep_worker=worker, start_call_prep_worker=False,
        )
        with TestClient(app) as client:
            self.assertEqual(client.get("/api/v1/outreach", headers=AUTH).json()["company_research"], {"available": True})
            created = client.post("/api/v1/outreach", headers=AUTH, json={"company": "Chargebot", "website": "https://chargebot.example"}).json()
            queued = client.post(f"/api/v1/outreach/{created['id']}/research", headers=AUTH)
            self.assertEqual(queued.status_code, 202, queued.text)
            self.assertEqual(queued.json()["tech_brief_job"]["state"], "queued")
            again = client.post(f"/api/v1/outreach/{created['id']}/research", headers=AUTH).json()
            self.assertEqual(again["tech_brief_job_id"], queued.json()["tech_brief_job_id"], "one job at a time")
            self.assertEqual(client.post("/api/v1/outreach/outreach-nope/research", headers=AUTH).status_code, 404)
            worker.run_pending()
            self.assertEqual(calls, [created["id"]])
            self.assertEqual(client.get(f"/api/v1/outreach/{created['id']}", headers=AUTH).json()["tech_brief_job"]["state"], "succeeded")

    def test_the_pane_offers_call_prep(self):
        script = static_script_text()  # every shipped script, so the asserts follow code that moves between files
        self.assertIn("/call-prep`", script)
        self.assertIn('["prep", "Call prep"]', script)
        self.assertIn("window.confirm(", js_function(script, "askForReply"))
        self.assertIn("visibilitychange", script)
        self.assertIn("/research`", script)
        self.assertIn("function outreachTechBrief", script)

    def test_the_panes_say_what_is_happening_and_do_not_overstate_the_second_read(self):
        script = static_script_text()
        # Each pane is cut out by function name, not by character offset, so moving the code or changing its length cannot shift a window.
        # Call prep researching inline shows as Researching, not as a live Research button.
        tech = js_function(script, "outreachTechBrief")
        self.assertIn("preppingFirst", tech)
        self.assertIn("item.call_prep_job?.state", tech)
        # The interviewer pane never shows a stored person after the student named someone else.
        pane = js_function(script, "outreachInterviewer")
        self.assertIn("(you named them); LinkedIn not read yet", pane)
        self.assertIn("record = {};", pane, "the old profile link is dropped")
        self.assertIn("linkedin.why", pane, "the reason comes from the record, not hardcoded copy")
        self.assertNotIn("it never names the company", script)
        # A separate read of a passage is not a different model, so no copy says it is.
        self.assertNotIn("a second model confirm", script)
        # The call-prep modules, whether each stays one file or becomes a package. Other outreach modules legitimately say
        # "second model": the follow-up and thank-you reviews really are read by a different model.
        names = ("outreach_research", "outreach_interviewer", "outreach_call_prep")
        modules = {path: text for path, text in python_modules("*.py").items() if path.split("/")[0].removesuffix(".py") in names}
        self.assertEqual({path.split("/")[0].removesuffix(".py") for path in modules}, set(names))
        for name, text in modules.items():
            self.assertNotIn("second model", text.casefold(), name)
        readme = (Path(outreach_call_prep.__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")
        self.assertNotIn("second model confirms", readme)
        self.assertNotIn("a second model which did not write", readme)


if __name__ == "__main__":
    unittest.main()
