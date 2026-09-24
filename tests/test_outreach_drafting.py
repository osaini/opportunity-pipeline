"""Grounded outreach drafts, approval gating, replies, and follow-up reminders."""

import json
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import date, datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi.testclient import TestClient

from opportunity_app import STATIC_DIR
from opportunity_app.agent_providers import ProviderReply
from opportunity_app.api import create_app
from opportunity_app.outreach import (
    DraftChangedError,
    approve_draft,
    confirm_research,
    create_target,
    get_target,
    list_targets,
    local_today,
    location_region,
    region_phrase,
    log_reply,
    queue_follow_up_reminders,
    suggest_reply_status,
    update_target,
)
from opportunity_app.outreach_drafting import (
    INSTRUCTIONS,
    DraftRejected,
    DraftVersionNotFoundError,
    draft_versions,
    generate_draft,
    restore_draft_version,
)
from opportunity_app.schema import connect_product, ensure_product_schema, utc_now

from helpers_platform import build_and_migrate, use_profile_regions

AUTH = {"Authorization": "Bearer drafting-owner"}
USER = "local-user"


def confirm_facts(conn, **facts):
    for field, value in facts.items():
        conn.execute(
            """
            INSERT INTO profile_facts(user_id, field_path, value_json, source, confirmed, created_at, updated_at)
            VALUES(?, ?, ?, 'user', 1, ?, ?)
            ON CONFLICT(user_id, field_path) DO UPDATE SET value_json=excluded.value_json, confirmed=1
            """,
            (USER, field, json.dumps(value), utc_now(), utc_now()),
        )
    conn.commit()


class ScriptedProvider:
    """Returns queued replies and records every prompt it was given."""

    name = "anthropic"
    model = "test-model"

    def __init__(self, replies):
        self.replies = list(replies)
        self.prompts = []

    def create(self, *, instructions, messages, tools, max_output_tokens):
        self.prompts.append(messages[-1]["content"])
        return ProviderReply(text=self.replies.pop(0))


def draft_json(subject, body, claims):
    return json.dumps({"subject": subject, "body": body, "claims": claims})


GOOD_BODY = (
    "Hi Greg,\n\nI'm Test Student, studying Mechanical Engineering at UT Austin. Bovi's work on dairy robotics "
    "caught my attention, and I have built projects with SolidWorks.\n\nWould you be open to a 15 minute call?\n\n"
    "Thank you,\nTest Student\nstudent@example.edu"
)
GOOD_CLAIMS = [
    {"text": "Test Student", "basis": "profile:name"},
    {"text": "Mechanical Engineering at UT Austin", "basis": "profile:degree"},
    {"text": "dairy robotics", "basis": "research:summary"},
    {"text": "SolidWorks", "basis": "profile:skills"},
]
GOOD = draft_json("Mechanical engineering student interested in Bovi", GOOD_BODY, GOOD_CLAIMS)


def bay_area_draft(body: str) -> str:
    """The same reply with the location note where it belongs, right after the school."""
    return draft_json(
        "Hello Bovi",
        body.replace("at UT Austin.", "at UT Austin (live in the Bay Area)."),
        [*GOOD_CLAIMS, {"text": "(live in the Bay Area)", "basis": "profile:break_location"}],
    )


class DraftingTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        use_profile_regions(self)
        _, self.platform_path = build_and_migrate(Path(self.tempdir.name))
        self.conn = connect_product(self.platform_path)
        ensure_product_schema(self.conn)
        confirm_facts(self.conn, school="UT Austin", degree="Mechanical Engineering", skills=["SolidWorks"])
        self.target = create_target(self.conn, {
            "company": "Bovi",
            "website": "https://bovi.example",
            "summary": "Dairy robotics for small farms",
            "contact_name": "Greg Hall",
            "contact_email": "greg@bovi.example",
            "source_urls": ["https://bovi.example/about"],
        }, user_id=USER)
        env = mock.patch.dict("os.environ", {"PIPELINE_OUTREACH_ACCOUNT": "student@example.edu"})
        env.start()
        self.addCleanup(env.stop)

    def tearDown(self):
        self.conn.close()
        self.tempdir.cleanup()

    def generate(self, provider, **kwargs):
        return generate_draft(
            self.conn, self.target["id"], user_id=USER, provider_factory=lambda *_: provider, provider="anthropic", **kwargs,
        )

    def test_a_grounded_draft_is_stored_for_review_with_its_claims(self):
        provider = ScriptedProvider([GOOD])
        target = self.generate(provider)
        self.assertEqual(target["draft_status"], "generated")
        self.assertEqual(target["status"], "drafted")
        self.assertEqual(target["draft_generated_by"], "anthropic:claude-sonnet-5")
        self.assertEqual(len(target["draft_claims"]), 4)
        prompt = json.loads(provider.prompts[0])
        self.assertEqual(prompt["student"]["school"], "UT Austin")
        self.assertNotIn("regions", prompt["student"], "only facts meant for an email reach the model")
        self.assertEqual(prompt["sender_address"], "student@example.edu")

    def test_an_invented_fact_is_retried_and_then_refused(self):
        invented = draft_json(
            "Hello", "Hi Greg,\n\nI led a team of 12 engineers at SpaceX.\n\nTest Student",
            [{"text": "led a team at SpaceX", "basis": "profile:experience"}],
        )
        provider = ScriptedProvider([invented, invented])
        with self.assertRaises(DraftRejected) as caught:
            self.generate(provider)
        self.assertIn("profile:experience", str(caught.exception))
        self.assertIn("12", str(caught.exception))
        self.assertIn("rejected because", provider.prompts[1])
        self.assertEqual(get_target(self.conn, self.target["id"], user_id=USER)["email_body"], "")

    def test_a_dash_is_fixed_on_retry(self):
        dashed = GOOD.replace("caught my attention, and", "caught my attention — and")
        provider = ScriptedProvider([dashed, GOOD])
        target = self.generate(provider)
        self.assertEqual(target["draft_checks"]["dash_count"], 0)
        self.assertEqual(len(provider.prompts), 2)

    def confirm_proof(self):
        confirm_facts(
            self.conn,
            experience=[
                {"organization": "Dronecorp", "role": "Engineering Intern", "outreach": "lead",
                 "highlights": ["Cut drone weight from 1.5 kg to 500 g across 30 units"]},
                {"organization": "Datacorp", "role": "Software Intern", "outreach": "omit",
                 "highlights": ["Parsed 250,000 records"]},
            ],
            projects=[{"title": "Go-kart", "highlights": ["Welded a 40 kg frame"]}],
            contact={"email": "student@example.edu", "phone": "555-555-5555", "portfolio": "https://student.example", "linkedin": ""},
        )

    def test_omitted_entries_never_reach_the_model_and_lead_entries_are_named(self):
        self.confirm_proof()
        led = GOOD.replace("I have built projects with SolidWorks.", "I cut drone weight from 1.5 kg to 500 g at Dronecorp.")
        provider = ScriptedProvider([led])
        self.generate(provider)
        prompt = json.loads(provider.prompts[0])
        self.assertEqual(prompt["lead_with"], ["Dronecorp"])
        self.assertEqual([entry["organization"] for entry in prompt["student"]["experience"]], ["Dronecorp"])
        self.assertNotIn("Datacorp", provider.prompts[0])
        self.assertNotIn("outreach", prompt["student"]["experience"][0])
        self.assertEqual(prompt["student"]["projects"][0]["title"], "Go-kart", "unmarked entries are support")
        self.assertEqual(prompt["links"], ["https://student.example"])
        self.assertNotIn("555-555-5555", provider.prompts[0], "a phone number stays out of a cold email")

    def test_the_email_stays_on_its_primary_experience(self):
        self.confirm_proof()
        led = GOOD.replace("I have built projects with SolidWorks.", "I cut drone weight from 1.5 kg to 500 g at Dronecorp.")
        wandering = led.replace("Would you be open", "I also welded frames on my go-kart. Would you be open")
        provider = ScriptedProvider([wandering, led])
        target = self.generate(provider)
        self.assertEqual(json.loads(provider.prompts[0])["primary_experience"], "Dronecorp")
        self.assertIn("names Go-kart, which the reader has not met; keep the email on Dronecorp", provider.prompts[1])
        self.assertEqual(target["draft_status"], "generated")

    def test_a_result_from_another_lead_entry_does_not_count_as_the_opening(self):
        self.confirm_proof()
        confirm_facts(self.conn, projects=[{"title": "Go-kart", "outreach": "lead", "highlights": ["Welded a 40 kg frame"]}])
        other_lead = GOOD.replace("I have built projects with SolidWorks.", "I welded a 40 kg frame.")
        provider = ScriptedProvider([other_lead, other_lead])
        with self.assertRaises(DraftRejected) as caught:
            self.generate(provider)
        self.assertEqual(json.loads(provider.prompts[0])["lead_with"], ["Dronecorp", "Go-kart"])
        self.assertIn("primary lead_with entry (Dronecorp)", str(caught.exception))

    def test_a_draft_without_a_lead_result_is_refused(self):
        self.confirm_proof()
        # "40" is a real number from a support entry, but the email must lead with a lead entry's result.
        support_only = GOOD.replace("I have built projects with SolidWorks.", "I welded a 40 kg go-kart frame.")
        provider = ScriptedProvider([support_only, support_only])
        with self.assertRaises(DraftRejected) as caught:
            self.generate(provider)
        self.assertIn("lead_with entry (Dronecorp)", str(caught.exception))

    def test_filler_and_a_second_inference_are_retried(self):
        filler = GOOD.replace("caught my attention", "is something I am especially interested in")
        inferred = json.loads(GOOD)
        inferred["claims"] += [
            {"text": "my robotics work fits Bovi", "basis": "inference"},
            {"text": "Bovi needs interns", "basis": "inference"},
        ]
        one_inference = json.loads(GOOD)
        one_inference["claims"].append({"text": "my robotics work fits Bovi", "basis": "inference"})
        provider = ScriptedProvider([filler, json.dumps(inferred)])
        with self.assertRaises(DraftRejected) as caught:
            self.generate(provider)
        self.assertIn("especially interested", provider.prompts[1])
        self.assertIn("2 claims on inference", str(caught.exception))
        target = self.generate(ScriptedProvider([json.dumps(one_inference)]))
        self.assertEqual(target["draft_claims"][-1]["basis"], "inference")

    def test_a_scale_comparison_bridge_is_retried(self):
        template = GOOD.replace("caught my attention", "is a bigger version of my projects, a smaller version of their problem")
        provider = ScriptedProvider([template, GOOD])
        self.assertEqual(self.generate(provider)["draft_status"], "generated")
        self.assertIn("smaller version of", provider.prompts[1])

    def test_a_second_question_is_retried(self):
        two = GOOD.replace("caught my attention", "caught my attention. Do you build the arms in house?")
        provider = ScriptedProvider([two, GOOD])
        self.assertEqual(self.generate(provider)["draft_status"], "generated")
        self.assertIn("more than one question", provider.prompts[1])

    def test_a_basis_inside_a_field_counts_as_that_field(self):
        precise = json.loads(GOOD)
        precise["claims"][1]["basis"] = "profile:degree[0].name"
        target = self.generate(ScriptedProvider([json.dumps(precise)]))
        self.assertEqual(target["draft_claims"][1]["basis"], "profile:degree")

    def test_an_offer_to_hand_over_work_material_is_refused_but_describing_it_is_not(self):
        offer = GOOD.replace("Would you be open to a 15 minute call?", "I'm happy to send my wiring and assembly docs or talk for 15 minutes.")
        provider = ScriptedProvider([offer, offer])
        with self.assertRaises(DraftRejected) as caught:
            self.generate(provider)
        self.assertIn("send my wiring and assembly docs", str(caught.exception))
        described = GOOD.replace("I have built projects with SolidWorks.", "I wrote the assembly docs for our SolidWorks builds.")
        self.assertEqual(self.generate(ScriptedProvider([described]))["draft_status"], "generated")

    def test_template_leads_with_a_lead_entry_and_signs_with_links(self):
        self.confirm_proof()
        target = generate_draft(self.conn, self.target["id"], user_id=USER, provider_factory=None, provider="legacy")
        self.assertIn("At Dronecorp, I cut drone weight from 1.5 kg to 500 g", target["email_body"])
        self.assertIn("student@example.edu | https://student.example", target["email_body"])

    def test_the_region_of_a_location_needs_its_state_to_agree(self):
        # Regions from tests/fixtures/profile_regions.json, set up in setUp.
        self.assertEqual(location_region("San Francisco, CA"), "Bay Area")
        self.assertEqual(location_region("Palo Alto, California"), "Bay Area")
        self.assertEqual(location_region("Bay Area"), "Bay Area")
        # How a company's own site and a YC page write the same places.
        self.assertEqual(location_region("South SF, CA"), "Bay Area")
        self.assertEqual(location_region("SF, CA"), "Bay Area")
        self.assertEqual(location_region("East Bay, CA"), "Bay Area")
        self.assertEqual(location_region("North Austin, TX"), "Austin")
        self.assertEqual(location_region("Austin, TX"), "Austin")
        self.assertEqual(location_region("UT Austin"), "Austin", "UT is the school, not Utah")
        self.assertEqual(location_region("Austin, MN"), "")
        self.assertEqual(location_region("Oakland, MI"), "")
        self.assertEqual(location_region("Dublin, Ireland"), "")
        self.assertEqual(location_region("Oakland"), "", "a bare town name is common elsewhere")
        self.assertEqual(location_region("Boston, MA"), "")
        self.assertEqual(location_region(""), "")

    def test_regions_come_only_from_the_students_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = Path(tmp) / "profile.json"
            profile.write_text(json.dumps({"regions": [{
                "name": "Atlanta", "state_markers": ["ga", "georgia"], "places": ["atlanta", "marietta"],
            }]}), encoding="utf-8")
            with mock.patch("opportunity_app.outreach.PROFILE_PATH", profile):
                self.assertEqual(location_region("Marietta, GA"), "Atlanta")
                self.assertEqual(location_region("Atlanta, Texas"), "")
                self.assertEqual(location_region("Austin, TX"), "", "no metro is built in")
                self.assertEqual(region_phrase("Atlanta"), "Atlanta")
            with mock.patch("opportunity_app.outreach.PROFILE_PATH", Path(tmp) / "missing.json"):
                self.assertEqual(location_region("Marietta, GA"), "")
                self.assertEqual(location_region("San Francisco, CA"), "")
                self.assertEqual(region_phrase("Research Triangle Area"), "the Research Triangle Area")

    def test_a_bay_area_company_hears_in_the_opening_that_the_student_is_based_there(self):
        confirm_facts(self.conn, break_location="Bay Area")
        update_target(self.conn, self.target["id"], {"location": "San Carlos, CA"}, user_id=USER)
        missing = draft_json("Hello Bovi", GOOD_BODY, GOOD_CLAIMS)
        provider = ScriptedProvider([missing, bay_area_draft(GOOD_BODY)])
        target = self.generate(provider)
        prompt = json.loads(provider.prompts[0])
        self.assertEqual(prompt["location_line"], "(live in the Bay Area)")
        self.assertEqual(prompt["student"]["break_location"], "Bay Area")
        self.assertIn("leaves out location_line", provider.prompts[1])
        opening, ask = target["email_body"].split("\n\n")[1:3]
        self.assertIn("at UT Austin (live in the Bay Area).", opening)
        self.assertNotIn("Bay Area", ask)
        self.assertEqual(target["location_region"], "Bay Area")

    def test_a_location_line_left_out_of_the_opening_is_sent_back(self):
        confirm_facts(self.conn, break_location="Bay Area")
        update_target(self.conn, self.target["id"], {"location": "San Carlos, CA"}, user_id=USER)
        in_the_ask = draft_json(
            "Hello Bovi",
            GOOD_BODY.replace("Would you", "I live in the Bay Area. Would you"),
            [*GOOD_CLAIMS, {"text": "live in the Bay Area", "basis": "profile:break_location"}],
        )
        provider = ScriptedProvider([in_the_ask, bay_area_draft(GOOD_BODY)])
        target = self.generate(provider)
        self.assertIn("says where you live later on instead of the opening's '(live in the Bay Area)'", provider.prompts[1])
        self.assertIn("at UT Austin (live in the Bay Area).", target["email_body"].split("\n\n")[1])

    def test_the_home_note_goes_right_after_the_school_in_its_own_words(self):
        confirm_facts(self.conn, break_location="Bay Area")
        update_target(self.conn, self.target["id"], {"location": "San Carlos, CA"}, user_id=USER)
        paraphrased = draft_json(
            "Hello Bovi",
            GOOD_BODY.replace("at UT Austin.", "at UT Austin. I'm based in the Bay Area."),
            [*GOOD_CLAIMS, {"text": "based in the Bay Area", "basis": "profile:break_location"}],
        )
        provider = ScriptedProvider([paraphrased, bay_area_draft(GOOD_BODY)])
        target = self.generate(provider)
        self.assertIn("'(live in the Bay Area)', which goes right after the school's name", provider.prompts[1])
        self.assertIn("studying Mechanical Engineering at UT Austin (live in the Bay Area).", target["email_body"])
        self.assertIn('"I\'m a [major] student at [school] [location_line] and', INSTRUCTIONS)
        self.assertIn("at Georgia Tech (live in the Seattle area) and", INSTRUCTIONS, "the example shows the note")

    def test_a_draft_written_before_the_company_was_placed_cannot_be_approved_without_the_line(self):
        # Seen 2026-09-21: discovery drafted Bay Area companies minutes before its
        # web search placed them, so their drafts never said the student lives there.
        confirm_facts(self.conn, break_location="Bay Area")
        target = self.generate(ScriptedProvider([GOOD]))
        self.assertEqual(target["draft_location"], {"phrase": "", "terms": [], "missing": False})

        update_target(self.conn, self.target["id"], {"location": "San Carlos, CA"}, user_id=USER)
        target = get_target(self.conn, self.target["id"], user_id=USER)
        self.assertEqual(target["draft_location"], {"phrase": "the Bay Area", "terms": ["the Bay Area", "Bay Area"], "missing": True})
        self.assertTrue(
            next(item for item in list_targets(self.conn, user_id=USER) if item["id"] == target["id"])["draft_location"]["missing"],
            "the list view flags it too",
        )
        with self.assertRaisesRegex(ValueError, "never says you live in the Bay Area, though Bovi is in San Carlos, CA"):
            approve_draft(self.conn, target["id"], user_id=USER, fingerprint=target["draft_fingerprint"], acknowledge_warnings=True)

        target = self.generate(ScriptedProvider([bay_area_draft(GOOD_BODY)]))
        self.assertFalse(target["draft_location"]["missing"])
        approved = approve_draft(self.conn, target["id"], user_id=USER, fingerprint=target["draft_fingerprint"], acknowledge_warnings=True)
        self.assertEqual(approved["draft_status"], "approved")

    def test_an_approved_draft_that_lacks_the_line_is_not_put_in_gmail(self):
        from opportunity_app.outreach_gmail import create_gmail_draft

        confirm_facts(self.conn, break_location="Bay Area")
        target = self.generate(ScriptedProvider([GOOD]))
        approve_draft(self.conn, target["id"], user_id=USER, fingerprint=target["draft_fingerprint"], acknowledge_warnings=True)
        update_target(self.conn, self.target["id"], {"location": "Oakland, CA"}, user_id=USER)
        self.assertEqual(get_target(self.conn, self.target["id"], user_id=USER)["draft_status"], "approved")

        def no_client():
            raise AssertionError("Gmail must not be called")

        with self.assertRaisesRegex(ValueError, "never says you live in the Bay Area"):
            create_gmail_draft(self.conn, self.target["id"], user_id=USER, client_factory=no_client)

    def test_a_company_away_from_home_or_an_unchecked_location_never_flags_a_draft(self):
        target = self.generate(ScriptedProvider([GOOD]))
        for home, location in (("Bay Area", "Austin, TX"), ("", "Oakland, CA"), ("Seattle", "Seattle, WA")):
            confirm_facts(self.conn, break_location=home)
            update_target(self.conn, self.target["id"], {"location": location}, user_id=USER)
            self.assertFalse(get_target(self.conn, target["id"], user_id=USER)["draft_location"]["missing"], (home, location))
        confirm_facts(self.conn, break_location="Bay Area")
        self.conn.execute("UPDATE outreach_targets SET location_basis='research', research_confidence='unverified' WHERE id=?", (target["id"],))
        self.conn.commit()
        self.assertFalse(
            get_target(self.conn, target["id"], user_id=USER)["draft_location"]["missing"],
            "an unconfirmed deep-search location is not grounds for the line",
        )

    def test_an_austin_or_unknown_company_gets_no_location_line(self):
        confirm_facts(self.conn, break_location="Bay Area")
        for location in ("Austin, TX", "Denver, CO", ""):
            update_target(self.conn, self.target["id"], {"location": location}, user_id=USER)
            provider = ScriptedProvider([GOOD])
            self.generate(provider)
            prompt = json.loads(provider.prompts[0])
            self.assertEqual(prompt["location_line"], "", location)
            self.assertNotIn("break_location", prompt["student"], "a home the email should not mention stays out")

    def test_a_home_in_the_schools_region_is_mentioned_as_year_round(self):
        confirm_facts(self.conn, school="University of Texas at Austin", break_location="Austin")
        update_target(self.conn, self.target["id"], {"location": "Round Rock, TX"}, user_id=USER)
        provider = ScriptedProvider([GOOD, GOOD])
        with self.assertRaises(DraftRejected):
            self.generate(provider)
        self.assertEqual(json.loads(provider.prompts[0])["location_line"], "(live in Austin year-round)")
        self.assertIn("leaves out location_line", provider.prompts[1])

    def test_a_home_city_outside_every_region_still_gets_the_line(self):
        # A student who never set up a region for home still lives somewhere.
        confirm_facts(self.conn, break_location="Seattle, WA")
        update_target(self.conn, self.target["id"], {"location": "Seattle, Washington"}, user_id=USER)
        seattle = draft_json(
            "Hello Bovi",
            GOOD_BODY.replace("at UT Austin.", "at UT Austin (live in Seattle)."),
            [*GOOD_CLAIMS, {"text": "(live in Seattle)", "basis": "profile:break_location"}],
        )
        provider = ScriptedProvider([seattle])
        target = self.generate(provider)
        self.assertEqual(json.loads(provider.prompts[0])["location_line"], "(live in Seattle)")
        self.assertEqual(target["draft_location"], {"phrase": "Seattle", "terms": ["Seattle"], "missing": False})

        for location in ("Seattle, OR", "Tacoma, WA", "Seattle"):
            update_target(self.conn, self.target["id"], {"location": location}, user_id=USER)
            self.assertEqual(get_target(self.conn, self.target["id"], user_id=USER)["draft_location"]["phrase"], "", location)
        confirm_facts(self.conn, break_location="Portland")
        update_target(self.conn, self.target["id"], {"location": "Portland, ME"}, user_id=USER)
        self.assertEqual(
            get_target(self.conn, self.target["id"], user_id=USER)["draft_location"]["phrase"], "",
            "a home town with no state could be anywhere, so nothing is assumed",
        )

    def test_template_says_the_student_is_nearby_for_a_bay_area_company(self):
        confirm_facts(self.conn, break_location="Bay Area")
        update_target(self.conn, self.target["id"], {"location": "Oakland, CA"}, user_id=USER)
        target = generate_draft(self.conn, self.target["id"], user_id=USER, provider_factory=None, provider="legacy")
        self.assertIn("at UT Austin (live in the Bay Area).", target["email_body"])
        self.assertNotIn("Bay Area", target["email_body"].split("\n\n")[2], "the ask does not repeat it")
        self.assertIn("profile:break_location", [claim["basis"] for claim in target["draft_claims"]])

    def test_template_fallback_uses_only_confirmed_facts(self):
        target = generate_draft(self.conn, self.target["id"], user_id=USER, provider_factory=None, provider="legacy")
        self.assertEqual(target["draft_generated_by"], "template")
        self.assertIn("Hi Greg,", target["email_body"])
        self.assertIn("Mechanical Engineering at UT Austin", target["email_body"])
        self.assertEqual(target["draft_checks"]["warnings"], [])

    def test_comments_steer_a_regeneration_without_becoming_a_source(self):
        self.generate(ScriptedProvider([GOOD]))
        shorter = GOOD.replace("Bovi's work on dairy robotics caught my attention, and ", "")
        provider = ScriptedProvider([shorter])
        target = self.generate(provider, comments="  Make it shorter.\r\nDrop the robotics line.  ")
        inputs, revision = provider.prompts[0].split("\n\nThe student reviewed", 1)
        self.assertNotIn("Make it shorter", inputs, "comments stay out of the grounded inputs")
        self.assertIn("Current draft:\nSubject: Mechanical engineering student interested in Bovi", revision)
        self.assertIn("caught my attention", revision, "the model sees the draft it is revising")
        self.assertTrue(revision.endswith("The student's comments:\nMake it shorter.\nDrop the robotics line."))
        self.assertEqual(target["email_body"], json.loads(shorter)["body"])
        versions = draft_versions(self.conn, self.target["id"], user_id=USER, kind="initial")
        self.assertEqual([version["comments"] for version in versions], ["", "Make it shorter.\nDrop the robotics line."])

    def test_a_number_only_in_the_comments_is_still_refused(self):
        self.generate(ScriptedProvider([GOOD]))
        claimed = GOOD.replace("I have built projects with SolidWorks.", "I have built 40 projects with SolidWorks.")
        provider = ScriptedProvider([claimed, claimed])
        with self.assertRaises(DraftRejected) as caught:
            self.generate(provider, comments="Say I have built 40 projects.")
        self.assertIn("40", str(caught.exception))
        self.assertIn("Say I have built 40 projects.", provider.prompts[1], "the retry keeps the comments")
        self.assertEqual(len(draft_versions(self.conn, self.target["id"], user_id=USER, kind="initial")), 1)

    def test_the_template_refuses_comments_it_cannot_follow(self):
        with self.assertRaises(ValueError):
            generate_draft(self.conn, self.target["id"], user_id=USER, provider_factory=None, provider="legacy", comments="Shorter")
        with self.assertRaises(ValueError):
            self.generate(ScriptedProvider([GOOD]), comments="x" * 2001)

    def test_every_draft_is_kept_and_an_earlier_one_can_be_restored(self):
        first = self.generate(ScriptedProvider([GOOD]))
        self.assertEqual(first["draft_history_count"], 0, "the only draft is the one in the editor")
        edited = update_target(self.conn, self.target["id"], {"email_body": first["email_body"] + "\nP.S. hi"}, user_id=USER)
        self.assertEqual(edited["draft_history_count"], 1)
        second_reply = GOOD.replace("caught my attention", "stood out to me")
        second = self.generate(ScriptedProvider([second_reply]))
        versions = draft_versions(self.conn, self.target["id"], user_id=USER, kind="initial")
        self.assertEqual([version["source"] for version in versions], ["generated", "saved", "generated"])
        self.assertTrue(versions[1]["body"].endswith("P.S. hi"), "a hand edit is kept before a regeneration replaces it")
        self.assertEqual([version["is_current"] for version in versions], [False, False, True])
        self.assertEqual(second["draft_history_count"], 2)

        approve_draft(self.conn, self.target["id"], user_id=USER, fingerprint=second["draft_fingerprint"])
        restored = restore_draft_version(self.conn, self.target["id"], versions[0]["id"], user_id=USER)
        self.assertEqual(restored["email_body"], first["email_body"])
        self.assertEqual(restored["draft_claims"], first["draft_claims"])
        self.assertEqual(restored["draft_generated_by"], first["draft_generated_by"])
        self.assertEqual(restored["draft_status"], "generated", "a restored draft needs approval again")
        self.assertEqual(restored["draft_fingerprint"], first["draft_fingerprint"])
        after = draft_versions(self.conn, self.target["id"], user_id=USER, kind="initial")
        self.assertEqual(len(after), 3, "the replaced draft was already stored, so nothing is duplicated")
        self.assertEqual([version["is_current"] for version in after], [True, False, False])
        events = [event["event_type"] for event in get_target(self.conn, self.target["id"], user_id=USER, include_events=True)["events"]]
        self.assertIn("draft_restored", events)
        self.assertIn("approval_withdrawn", events)

        again = restore_draft_version(self.conn, self.target["id"], versions[0]["id"], user_id=USER)
        self.assertEqual(again["updated_at"], restored["updated_at"], "restoring the draft already in the editor is a no-op")
        with self.assertRaises(DraftVersionNotFoundError):
            restore_draft_version(self.conn, self.target["id"], "draft-version-missing", user_id=USER)

    def test_a_draft_written_before_history_is_kept_when_regenerated(self):
        update_target(self.conn, self.target["id"], {"email_subject": "Old", "email_body": "Hi Greg, an older draft."}, user_id=USER)
        self.assertEqual(draft_versions(self.conn, self.target["id"], user_id=USER, kind="initial"), [])
        self.generate(ScriptedProvider([GOOD]))
        versions = draft_versions(self.conn, self.target["id"], user_id=USER, kind="initial")
        self.assertEqual([(version["source"], version["subject"]) for version in versions][0], ("saved", "Old"))

    def test_approval_unlocks_once_and_any_edit_withdraws_it(self):
        self.generate(ScriptedProvider([GOOD]))
        approved = approve_draft(
            self.conn, self.target["id"], user_id=USER,
            fingerprint=get_target(self.conn, self.target["id"], user_id=USER)["draft_fingerprint"],
        )
        self.assertEqual(approved["draft_status"], "approved")
        self.assertTrue(approved["draft_approved_at"])

        edited = update_target(self.conn, self.target["id"], {"email_body": approved["email_body"] + "\nP.S. hi"}, user_id=USER)
        self.assertEqual(edited["draft_status"], "generated")
        self.assertIsNone(edited["draft_approved_at"])

        approve_draft(
            self.conn, self.target["id"], user_id=USER,
            fingerprint=get_target(self.conn, self.target["id"], user_id=USER)["draft_fingerprint"],
        )
        rerouted = update_target(self.conn, self.target["id"], {"contact_email": "info@bovi.example"}, user_id=USER)
        self.assertEqual(rerouted["draft_status"], "generated", "a new recipient needs a new approval")
        events = [event["event_type"] for event in get_target(self.conn, self.target["id"], user_id=USER, include_events=True)["events"]]
        self.assertEqual(events.count("approval_withdrawn"), 2)

    def test_approval_requires_a_recipient_and_no_placeholders(self):
        update_target(self.conn, self.target["id"], {"email_subject": "Hi", "email_body": "Hi [Name], hello"}, user_id=USER)
        with self.assertRaisesRegex(ValueError, "placeholders"):
            approve_draft(
                self.conn, self.target["id"], user_id=USER,
                fingerprint=get_target(self.conn, self.target["id"], user_id=USER)["draft_fingerprint"],
                acknowledge_warnings=True,
            )
        update_target(self.conn, self.target["id"], {"email_body": "Hi Greg — hello", "contact_email": ""}, user_id=USER)
        with self.assertRaisesRegex(ValueError, "contact email"):
            approve_draft(
                self.conn, self.target["id"], user_id=USER,
                fingerprint=get_target(self.conn, self.target["id"], user_id=USER)["draft_fingerprint"],
            )
        update_target(self.conn, self.target["id"], {"contact_email": "greg@bovi.example"}, user_id=USER)
        with self.assertRaisesRegex(ValueError, "warnings"):
            approve_draft(
                self.conn, self.target["id"], user_id=USER,
                fingerprint=get_target(self.conn, self.target["id"], user_id=USER)["draft_fingerprint"],
            )
        current = get_target(self.conn, self.target["id"], user_id=USER)
        self.assertEqual(approve_draft(
            self.conn, self.target["id"], user_id=USER, fingerprint=current["draft_fingerprint"], acknowledge_warnings=True,
        )["draft_status"], "approved")

    def test_follow_up_drafts_need_a_sent_email(self):
        with self.assertRaisesRegex(ValueError, "marked sent"):
            self.generate(ScriptedProvider([GOOD]), kind="follow_up")
        self.generate(ScriptedProvider([GOOD]))
        update_target(self.conn, self.target["id"], {"status": "sent"}, user_id=USER)
        follow_up = draft_json(
            "Re: Mechanical engineering student interested in Bovi",
            "Hi Greg,\n\nFollowing up on my note about a 15 minute call.\n\nTest Student",
            [{"text": "Test Student", "basis": "profile:name"}],
        )
        target = self.generate(ScriptedProvider([follow_up]), kind="follow_up")
        self.assertEqual(target["follow_up_status"], "generated")
        self.assertTrue(target["follow_up_subject"].startswith("Re:"))
        self.assertEqual(target["draft_status"], "generated", "the original draft is untouched")

    def test_fingerprints_bind_boundaries_claims_and_atomic_content(self):
        first = create_target(self.conn, {
            "company": "Boundary One", "contact_email": "x@bovi.example", "email_subject": "a\nb", "email_body": "c",
        }, user_id=USER)
        second = create_target(self.conn, {
            "company": "Boundary Two", "contact_email": "x@bovi.example", "email_subject": "a", "email_body": "b\nc",
        }, user_id=USER)
        self.assertNotEqual(first["draft_fingerprint"], second["draft_fingerprint"])

        drafted = self.generate(ScriptedProvider([GOOD]))
        stale = drafted["draft_fingerprint"]
        update_target(self.conn, self.target["id"], {"email_body": drafted["email_body"] + " changed"}, user_id=USER)
        with self.assertRaises(DraftChangedError):
            approve_draft(self.conn, self.target["id"], user_id=USER, fingerprint=stale)

        current = get_target(self.conn, self.target["id"], user_id=USER)
        old = current["draft_fingerprint"]
        self.conn.execute("UPDATE outreach_targets SET draft_claims_json='[{\"text\":\"x\",\"basis\":\"unverified:summary\"}]' WHERE id=?", (self.target["id"],))
        self.conn.commit()
        with self.assertRaises(DraftChangedError):
            approve_draft(self.conn, self.target["id"], user_id=USER, fingerprint=old)

    def test_atomic_approval_refuses_a_change_after_the_read(self):
        drafted = self.generate(ScriptedProvider([GOOD]))
        original = get_target
        changed = False

        def racing_get(conn, target_id, **kwargs):
            nonlocal changed
            target = original(conn, target_id, **kwargs)
            if not changed:
                changed = True
                conn.execute("UPDATE outreach_targets SET email_body=email_body || ' raced' WHERE id=?", (target_id,))
                conn.commit()
            return target

        with mock.patch("opportunity_app.outreach.get_target", side_effect=racing_get):
            with self.assertRaises(DraftChangedError):
                approve_draft(self.conn, self.target["id"], user_id=USER, fingerprint=drafted["draft_fingerprint"])

    def test_unverified_research_is_partitioned_warned_and_template_omits_it(self):
        target = create_target(self.conn, {
            "company": "Unverified Co", "website": "https://unverified.example", "summary": "Secret propulsion",
            "activity_signal": "$90M seed", "contact_name": "Dana Founder", "contact_role": "CEO",
            "contact_email": "dana@unverified.example", "source_urls": ["https://unverified.example/about"],
        }, user_id=USER, origin="discovery")
        provider = ScriptedProvider([draft_json(
            "Hello", "Hi team,\n\nUnverified Co builds Secret propulsion.\n\nTest Student",
            [{"text": "Secret propulsion", "basis": "unverified:summary"}, {"text": "Test Student", "basis": "profile:name"}],
        )])
        drafted = generate_draft(
            self.conn, target["id"], user_id=USER, provider_factory=lambda *_: provider, provider="anthropic",
        )
        prompt = json.loads(provider.prompts[0])
        self.assertEqual(set(prompt["company_research"]), {"company", "website"})
        self.assertEqual(prompt["unverified_research"]["summary"], "Secret propulsion")
        with self.assertRaisesRegex(ValueError, "research is unverified"):
            approve_draft(self.conn, target["id"], user_id=USER, fingerprint=drafted["draft_fingerprint"])

        template = generate_draft(self.conn, target["id"], user_id=USER, provider_factory=None, provider="legacy")
        for text in ("Secret propulsion", "$90M seed", "Dana Founder", "CEO"):
            self.assertNotIn(text, template["email_subject"] + template["email_body"])

    def test_confirming_research_clears_the_warning_on_a_draft_already_written(self):
        """Confirmation reaches a draft written before it, without rewriting history.

        The claim citations are frozen at generation time, so a draft written
        while the research was unverified kept warning after the student had
        confirmed it — and the only way out was "Approve anyway".
        """
        target = create_target(self.conn, {
            "company": "Unverified Co", "website": "https://unverified.example", "summary": "Secret propulsion",
            "contact_name": "Dana Founder", "contact_role": "CEO",
            "contact_email": "dana@unverified.example", "source_urls": ["https://unverified.example/about"],
        }, user_id=USER, origin="discovery")
        provider = ScriptedProvider([draft_json(
            "Hello", "Hi Dana,\n\nUnverified Co builds Secret propulsion.\n\nTest Student",
            [{"text": "Secret propulsion", "basis": "unverified:summary"}, {"text": "Test Student", "basis": "profile:name"}],
        )])
        drafted = generate_draft(
            self.conn, target["id"], user_id=USER, provider_factory=lambda *_: provider, provider="anthropic",
        )
        self.assertEqual(drafted["draft_claims"][0]["basis"], "unverified:summary")

        confirmed = confirm_research(self.conn, target["id"], user_id=USER)
        self.assertEqual(
            [claim["basis"] for claim in confirmed["draft_claims"]], ["research:summary", "profile:name"],
        )
        self.assertEqual(
            confirmed["draft_fingerprint"], drafted["draft_fingerprint"],
            "confirming the research must not look like the draft changed",
        )
        approved = approve_draft(
            self.conn, target["id"], user_id=USER, fingerprint=confirmed["draft_fingerprint"],
        )
        self.assertEqual(approved["draft_status"], "approved")
        accepted = [
            event for event in get_target(self.conn, target["id"], user_id=USER, include_events=True)["events"]
            if "Accepted warnings" in (event["detail"] or "")
        ]
        self.assertEqual(accepted, [], "nothing was waved through")
        versions = draft_versions(self.conn, target["id"], user_id=USER, kind="initial")
        self.assertEqual(
            versions[0]["claims"][0]["basis"], "unverified:summary",
            "the stored version keeps the citation the model wrote",
        )

    def test_confirmed_research_source_url_claim_does_not_require_acknowledgement(self):
        provider = ScriptedProvider([draft_json(
            "Hello Bovi", "Hi Greg,\n\nBovi builds dairy robots.\n\nTest Student",
            [{"text": "Bovi builds dairy robots", "basis": "https://bovi.example/about"}],
        )])
        drafted = self.generate(provider)
        approved = approve_draft(
            self.conn, self.target["id"], user_id=USER, fingerprint=drafted["draft_fingerprint"],
        )
        self.assertEqual(approved["draft_status"], "approved")

    def test_unverified_research_source_url_claim_requires_acknowledgement(self):
        target = create_target(self.conn, {
            "company": "Unverified Source", "website": "https://unverified.example",
            "contact_email": "dana@unverified.example", "source_urls": ["https://unverified.example/about"],
        }, user_id=USER, origin="discovery")
        provider = ScriptedProvider([draft_json(
            "Hello", "Hi Dana,\n\nI saw Unverified Source's work.\n\nTest Student",
            [{"text": "Unverified Source's work", "basis": "https://unverified.example/about"}],
        )])
        drafted = generate_draft(
            self.conn, target["id"], user_id=USER, provider_factory=lambda *_: provider, provider="anthropic",
        )
        with self.assertRaisesRegex(ValueError, "research is unverified"):
            approve_draft(self.conn, target["id"], user_id=USER, fingerprint=drafted["draft_fingerprint"])

    def test_unverified_follow_up_claims_are_stored_and_warned(self):
        self.conn.execute("UPDATE outreach_targets SET research_confidence='unverified' WHERE id=?", (self.target["id"],))
        self.conn.commit()
        update_target(
            self.conn, self.target["id"],
            {"email_subject": "Hello", "email_body": "Original", "status": "sent"},
            user_id=USER, today=date(2026, 9, 1),
        )
        follow = draft_json(
            "Re: hello", "Hi Greg,\n\nFollowing up about dairy robotics.\n\nTest Student",
            [{"text": "dairy robotics", "basis": "unverified:summary"}],
        )
        drafted = self.generate(ScriptedProvider([follow]), kind="follow_up")
        self.assertEqual(drafted["follow_up_claims"][0]["basis"], "unverified:summary")
        self.assertTrue(drafted["follow_up_generated_by"])
        with self.assertRaisesRegex(ValueError, "research is unverified"):
            approve_draft(
                self.conn, self.target["id"], user_id=USER, kind="follow_up",
                fingerprint=drafted["follow_up_fingerprint"],
            )


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        _, platform_path = build_and_migrate(Path(self.tempdir.name))
        self.conn = connect_product(platform_path)
        ensure_product_schema(self.conn)

    def tearDown(self):
        self.conn.close()
        self.tempdir.cleanup()

    def test_reply_suggestions(self):
        cases = {
            "Thanks for reaching out! Could we set up a quick call next week?": "call_scheduled",
            "Unfortunately we are not hiring interns this year.": "declined",
            "Please reach back out next semester, we may have room then.": "paused",
            "We'd like to extend an offer for the summer.": "offer",
            "Thanks, I'll pass this to the team.": "replied",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(suggest_reply_status(text)["status"], expected)

    def test_logging_a_reply_records_it_without_changing_status(self):
        target = create_target(self.conn, {"company": "Align", "status": "sent"}, user_id=USER)
        result = log_reply(self.conn, target["id"], "Happy to chat, when are you free?", user_id=USER)
        self.assertEqual(result["suggestion"]["status"], "call_scheduled")
        self.assertEqual(result["target"]["status"], "sent")
        self.assertEqual(result["target"]["events"][0]["event_type"], "reply_logged")

    def test_events_keep_their_order_when_the_clock_does_not_move(self):
        # The Windows clock on Python 3.12 ticks about every 15ms, so one action
        # that logs two events can stamp both with the same time. Freeze the
        # clock to force that tie everywhere.
        with mock.patch("opportunity_app.outreach.utc_now", return_value="2026-09-23T12:00:00.000000+00:00"):
            target = create_target(self.conn, {"company": "Align", "status": "sent"}, user_id=USER)
            result = log_reply(self.conn, target["id"], "Happy to chat, when are you free?", user_id=USER)
        self.assertEqual([event["event_type"] for event in result["target"]["events"]], ["reply_logged", "created"])

    def test_logging_a_long_reply_keeps_the_whole_accepted_text(self):
        target = create_target(self.conn, {"company": "Long Reply", "status": "sent"}, user_id=USER)
        body = "Start\n" + "x" * 2500 + "\nEnd marker"
        result = log_reply(self.conn, target["id"], body, user_id=USER)
        self.assertEqual(result["target"]["events"][0]["detail"], body)

    def test_follow_up_reminders_are_queued_once(self):
        create_target(self.conn, {"company": "Align", "status": "sent"}, user_id=USER, today=date(2026, 9, 1))
        create_target(self.conn, {"company": "Later", "status": "sent"}, user_id=USER, today=date(2026, 9, 10))
        first = queue_follow_up_reminders(self.conn, today=date(2026, 9, 9))
        second = queue_follow_up_reminders(self.conn, today=date(2026, 9, 9))
        self.assertEqual(first, {"due": 1, "queued": 1})
        self.assertEqual(second, {"due": 1, "queued": 0})

    def test_revisit_dates_queue_their_own_reminder(self):
        create_target(self.conn, {"company": "Westmag", "status": "paused", "follow_up_at": "2027-01-05"}, user_id=USER)
        create_target(self.conn, {"company": "Gone", "status": "declined", "follow_up_at": "2027-01-05"}, user_id=USER)
        self.assertEqual(queue_follow_up_reminders(self.conn, today=date(2027, 1, 4)), {"due": 0, "queued": 0})
        self.assertEqual(queue_follow_up_reminders(self.conn, today=date(2027, 1, 5)), {"due": 1, "queued": 1})
        payload = json.loads(self.conn.execute("SELECT payload_json FROM notification_outbox").fetchone()[0])
        self.assertEqual(payload["subject"], "Get back in touch with Westmag")

    def test_no_response_is_suggested_after_an_ignored_follow_up(self):
        create_target(self.conn, {"company": "Quiet", "status": "followed_up", "follow_up_at": "2026-09-01"}, user_id=USER)
        self.assertIsNone(list_targets(self.conn, user_id=USER, today=date(2026, 9, 10))[0]["suggestion"])
        self.assertEqual(list_targets(self.conn, user_id=USER, today=date(2026, 9, 15))[0]["suggestion"]["status"], "no_response")

    def test_followed_up_records_the_sent_day_and_queues_no_second_reminder(self):
        target = create_target(self.conn, {"company": "Once", "status": "sent"}, user_id=USER, today=date(2026, 9, 1))
        followed = update_target(self.conn, target["id"], {"status": "followed_up"}, user_id=USER, today=date(2026, 9, 8))
        self.assertEqual(followed["follow_up_at"], "2026-09-08")
        self.assertEqual(queue_follow_up_reminders(self.conn, today=date(2026, 10, 1)), {"due": 0, "queued": 0})
        self.assertIsNone(list_targets(self.conn, user_id=USER, today=date(2026, 9, 21))[0]["suggestion"])
        self.assertEqual(list_targets(self.conn, user_id=USER, today=date(2026, 9, 22))[0]["suggestion"]["status"], "no_response")

    def test_local_dates_honor_env_explicit_utc_and_each_reminder_owner(self):
        instant = datetime(2026, 9, 17, 3, 55, tzinfo=timezone.utc)
        with mock.patch.dict("os.environ", {"PIPELINE_TIMEZONE": "America/Chicago"}):
            self.assertEqual(local_today(self.conn, USER, instant), date(2026, 9, 16))
        self.conn.execute("INSERT INTO users(id, email, display_name, role, created_at, updated_at) VALUES('utc-user', 'u@example.com', 'UTC', 'student', 'x', 'x')")
        self.conn.execute("INSERT INTO notification_preferences(user_id, timezone, timezone_explicit, updated_at) VALUES('utc-user', 'UTC', 1, 'x')")
        self.conn.commit()
        self.assertEqual(local_today(self.conn, "utc-user", instant), date(2026, 9, 17))

    def test_reminders_use_each_owners_local_day(self):
        self.conn.execute(
            "INSERT INTO notification_preferences(user_id, timezone, timezone_explicit, updated_at) VALUES(?, 'America/Chicago', 1, 'x') "
            "ON CONFLICT(user_id) DO UPDATE SET timezone='America/Chicago', timezone_explicit=1",
            (USER,),
        )
        self.conn.execute("INSERT INTO users(id, email, display_name, role, created_at, updated_at) VALUES('tokyo', 't@example.com', 'Tokyo', 'student', 'x', 'x')")
        self.conn.execute("INSERT INTO notification_preferences(user_id, timezone, timezone_explicit, updated_at) VALUES('tokyo', 'Asia/Tokyo', 1, 'x')")
        self.conn.commit()
        create_target(self.conn, {"company": "Chicago", "status": "sent", "follow_up_at": "2026-09-17"}, user_id=USER)
        create_target(self.conn, {"company": "Tokyo", "status": "sent", "follow_up_at": "2026-09-17"}, user_id="tokyo")
        result = queue_follow_up_reminders(
            self.conn, now=datetime(2026, 9, 16, 20, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(result, {"due": 1, "queued": 1})
        queued_user = self.conn.execute("SELECT user_id FROM notification_outbox").fetchone()[0]
        self.assertEqual(queued_user, "tokyo")

    def test_import_skips_a_company_already_tracked_under_another_name(self):
        from opportunity_app.outreach import import_targets

        create_target(self.conn, {"company": "Acme", "website": "https://www.acme.com"}, user_id=USER)
        result = import_targets(self.conn, [
            {"company": "Acme Robotics", "website": "https://acme.com/about"},
            {"company": "Other", "website": "https://other.com", "draft_status": "approved", "origin": "discovery"},
        ], user_id=USER)
        self.assertEqual((result["imported"], result["skipped"]), (1, 1))
        other = get_target(self.conn, result["created_ids"][0], user_id=USER)
        self.assertEqual((other["origin"], other["draft_status"]), ("import", "none"), "an import cannot pre-approve a draft")


class DraftingApiTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        _, self.platform_path = build_and_migrate(root)
        self.provider = ScriptedProvider([GOOD])
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
            confirm_facts(conn, school="UT Austin", degree="Mechanical Engineering", skills=["SolidWorks"])

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.tempdir.cleanup()

    def test_generate_approve_and_compose_settings(self):
        created = self.client.post("/api/v1/outreach", headers=AUTH, json={
            "company": "Bovi", "summary": "Dairy robotics", "contact_name": "Greg Hall", "contact_email": "greg@bovi.example",
        }).json()
        blocked = self.client.post(f"/api/v1/outreach/{created['id']}/approve", headers=AUTH, json={})
        self.assertEqual(blocked.status_code, 422)
        drafted = self.client.post(f"/api/v1/outreach/{created['id']}/draft", headers=AUTH, json={"kind": "initial"})
        self.assertEqual(drafted.status_code, 200, drafted.text)
        self.assertEqual(drafted.json()["draft_status"], "generated")
        approved = self.client.post(
            f"/api/v1/outreach/{created['id']}/approve", headers=AUTH,
            json={"kind": "initial", "fingerprint": drafted.json()["draft_fingerprint"]},
        )
        self.assertEqual(approved.json()["draft_status"], "approved", approved.text)

        with mock.patch.dict("os.environ", {"PIPELINE_OUTREACH_COMPOSE": "gmail", "PIPELINE_OUTREACH_ACCOUNT": "me@example.edu"}):
            listing = self.client.get("/api/v1/outreach", headers=AUTH).json()
        self.assertEqual(listing["compose"], {"provider": "gmail", "account": "me@example.edu"})
        self.assertEqual(listing["summary"]["drafts_awaiting_approval"], 0)
        self.assertFalse(listing["discovery"]["available"], "a test database never gets a live deep search")

    def test_stale_and_malformed_approval_fingerprints_are_rejected(self):
        created = self.client.post("/api/v1/outreach", headers=AUTH, json={
            "company": "Stale", "contact_email": "x@stale.example", "email_subject": "Hi", "email_body": "Body",
        }).json()
        malformed = self.client.post(
            f"/api/v1/outreach/{created['id']}/approve", headers=AUTH, json={"fingerprint": "nope"},
        )
        self.assertEqual(malformed.status_code, 422)
        self.client.patch(f"/api/v1/outreach/{created['id']}", headers=AUTH, json={"email_body": "Changed"})
        stale = self.client.post(
            f"/api/v1/outreach/{created['id']}/approve", headers=AUTH, json={"fingerprint": created["draft_fingerprint"]},
        )
        self.assertEqual(stale.status_code, 409)

    def test_confirm_research_logs_and_patch_cannot_change_confidence(self):
        with closing(connect_product(self.platform_path)) as conn:
            target = create_target(conn, {"company": "Discovery"}, user_id=USER, origin="discovery")
        patched = self.client.patch(
            f"/api/v1/outreach/{target['id']}", headers=AUTH, json={"research_confidence": "confirmed"},
        ).json()
        self.assertEqual(patched["research_confidence"], "unverified")
        confirmed = self.client.post(f"/api/v1/outreach/{target['id']}/confirm-research", headers=AUTH).json()
        self.assertEqual(confirmed["research_confidence"], "confirmed")
        detail = self.client.get(f"/api/v1/outreach/{target['id']}", headers=AUTH).json()
        self.assertIn("research_confirmed", [event["event_type"] for event in detail["events"]])

    def test_draft_history_routes(self):
        created = self.client.post("/api/v1/outreach", headers=AUTH, json={
            "company": "Bovi", "summary": "Dairy robotics", "contact_email": "greg@bovi.example",
            "email_subject": "Old", "email_body": "Hi, an older draft.",
        }).json()
        other = self.client.post("/api/v1/outreach", headers=AUTH, json={"company": "Other"}).json()
        too_long = self.client.post(f"/api/v1/outreach/{created['id']}/draft", headers=AUTH, json={"comments": "x" * 2001})
        self.assertEqual(too_long.status_code, 422)
        drafted = self.client.post(f"/api/v1/outreach/{created['id']}/draft", headers=AUTH, json={"kind": "initial", "comments": "Shorter"})
        self.assertEqual(drafted.status_code, 200, drafted.text)
        self.assertIn("Shorter", self.provider.prompts[-1])
        self.assertEqual(drafted.json()["draft_history_count"], 1)

        history = self.client.get(f"/api/v1/outreach/{created['id']}/drafts", headers=AUTH, params={"kind": "initial"}).json()["items"]
        self.assertEqual([(item["subject"], item["is_current"]) for item in history], [("Old", False), (drafted.json()["email_subject"], True)])
        self.assertEqual(history[1]["comments"], "Shorter")
        self.assertEqual(self.client.get(f"/api/v1/outreach/{created['id']}/drafts", headers=AUTH, params={"kind": "follow_up"}).json()["items"], [])
        self.assertEqual(self.client.get("/api/v1/outreach/missing/drafts", headers=AUTH).status_code, 404)

        crossed = self.client.post(f"/api/v1/outreach/{other['id']}/drafts/{history[0]['id']}/restore", headers=AUTH)
        self.assertEqual(crossed.status_code, 404, "a version only restores into its own target")
        restored = self.client.post(f"/api/v1/outreach/{created['id']}/drafts/{history[0]['id']}/restore", headers=AUTH)
        self.assertEqual(restored.status_code, 200, restored.text)
        self.assertEqual(restored.json()["email_subject"], "Old")
        self.assertEqual(restored.json()["draft_history_count"], 1)

        exported = self.client.get("/api/v1/outreach/export", headers=AUTH, params={"format": "json"}).json()
        self.assertNotIn("draft_history_count", exported["items"][0])

    def test_a_patch_cannot_approve_a_draft(self):
        created = self.client.post("/api/v1/outreach", headers=AUTH, json={"company": "Bovi", "email_body": "Hi", "email_subject": "Hi"}).json()
        self.assertEqual(created["draft_status"], "generated")
        patched = self.client.patch(f"/api/v1/outreach/{created['id']}", headers=AUTH, json={"draft_status": "approved"})
        self.assertEqual(patched.json()["draft_status"], "generated")

    def test_reply_route_and_discovery_unavailable(self):
        created = self.client.post("/api/v1/outreach", headers=AUTH, json={"company": "Bovi", "status": "sent"}).json()
        reply = self.client.post(f"/api/v1/outreach/{created['id']}/reply", headers=AUTH, json={"text": "Not hiring right now, sorry."})
        self.assertEqual(reply.json()["suggestion"]["status"], "declined")
        started = self.client.post("/api/v1/outreach/discovery", headers=AUTH, json={})
        self.assertEqual(started.status_code, 409)


if __name__ == "__main__":
    unittest.main()
