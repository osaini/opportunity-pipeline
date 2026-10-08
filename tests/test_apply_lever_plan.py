"""The plan for a Lever form (docs/phase5-lever-handoff-spec.md 6.6): every Lever row of the truth table, against the sanitized pages in
tests/fixtures/apply/lever/. No browser, no network, no database; every company, person and posting is fictional.
"""

import dataclasses
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import realdata_guard

realdata_guard.install()

from opportunity_app.apply import ats as apply_ats, lever, lever_form
from opportunity_app.apply.policy import SchemaField, build_plan

from apply_fake_ats import LEVER_COMPANY, lever_fixture_text
from helpers_apply import Store, answer, entry, sources

FACTS = {
    "name_parts": {"first": "Sam", "last": "Rivera", "preferred": "Sammy"},
    "contact": {"email": "sam.rivera@example.test", "phone": "555-0100", "linkedin": "https://linkedin.example.test/in/sam",
                "github": "https://github.example.test/sam"},
}
LOCATION = "Austin, Texas, United States"


def schema_of(name):
    form = lever_form.parse_lever_form(lever_fixture_text(name))
    assert form is not None, name
    return apply_ats.lever_parse_schema({"lever_form": form})


def lever_plan(name, company, *, src=None, mode="handoff", **kwargs):
    return build_plan(schema_of(name), None, src or sources(facts=FACTS, labels={"location": LOCATION}), company, mode, ats_name="Lever", ats="lever", **kwargs)


def row(plan, key):
    found = [item for item in plan.fields if item.key == key]
    assert len(found) == 1, (key, [item.key for item in plan.fields])
    return found[0]


def card(plan, text):
    found = [item for item in plan.fields if item.question.startswith(text)]
    assert len(found) == 1, (text, [item.question for item in plan.fields])
    return found[0]


# --- The plan, Lever rows (spec 6.6) --------------------------------------------------------------------------------------

class LeverPlanTests(unittest.TestCase):
    def test_the_full_name_is_the_name_for_applications_never_the_preferred_name(self):
        plan = lever_plan("demo_eeo_survey.html", LEVER_COMPANY)
        name = row(plan, "name")
        self.assertEqual((name.source.kind, name.source.ref, name.value, name.disposition), ("profile", "name_parts.full", "Sam Rivera", "fill"))
        self.assertNotIn("Sammy", json.dumps([item.value for item in plan.fields]))

    def test_a_missing_or_half_a_name_is_a_name_problem(self):
        for facts in ({"contact": FACTS["contact"]}, {"name_parts": {"first": "Sam", "last": ""}, "contact": FACTS["contact"]}):
            with self.subTest(facts=facts):
                plan = lever_plan("demo_eeo_survey.html", LEVER_COMPANY, src=sources(facts=facts))
                self.assertEqual((row(plan, "name").problem_kind, row(plan, "name").required), ("name", True))

    def test_email_and_phone_come_from_the_confirmed_contact_and_are_problems_without_one(self):
        plan = lever_plan("demo_eeo_survey.html", LEVER_COMPANY)
        self.assertEqual([(row(plan, key).source.ref, row(plan, key).disposition) for key in ("email", "phone")], [("contact.email", "fill"), ("contact.phone", "fill")])
        bare = lever_plan("demo_eeo_survey.html", LEVER_COMPANY, src=sources(facts={"name_parts": FACTS["name_parts"], "contact": {}}))
        self.assertEqual([(row(bare, key).problem_kind, row(bare, key).required) for key in ("email", "phone")], [("profile_fact", True)] * 2)

    def test_location_is_the_option_the_student_confirmed_for_lever_or_a_choose_your_location_problem(self):
        plan = lever_plan("many_cards.html", "Orbital Ledger")
        location = row(plan, "location")
        self.assertEqual((location.source.kind, location.value, location.disposition), ("ats_label", LOCATION, "fill"))
        self.assertEqual([item.key for item in plan.fields if item.key == "selectedLocation"], [], "the hidden field is the page's own")
        needed = lever_plan("many_cards.html", "Orbital Ledger", src=sources(facts=FACTS))
        location = row(needed, "location")
        self.assertEqual((location.problem_kind, location.label_field, location.required), ("label_needed", "location", True))
        self.assertTrue(location.problem.startswith("Choose your current location"))

    def test_current_company_has_no_source_and_a_required_one_is_the_window_not_the_apps(self):
        required = row(lever_plan("demo_eeo_survey.html", LEVER_COMPANY), "org")
        self.assertEqual((required.required, required.problem_kind, required.disposition, required.source.kind), (True, "window", "left_for_you", "none"))
        optional = row(lever_plan("many_cards.html", "Orbital Ledger"), "org")
        self.assertEqual((optional.required, optional.problem_kind, optional.disposition), (False, "", "blank"))
        self.assertIn("Type it in the window", optional.note)

    def test_links_take_a_confirmed_fact_only_for_linkedin_github_and_website_labels(self):
        plan = lever_plan("cards_files_consent.html", "Quillfeather Pets")
        self.assertEqual([(key, row(plan, key).source.ref) for key in ("urls[LinkedIn]", "urls[GitHub]")], [("urls[LinkedIn]", "contact.linkedin"), ("urls[GitHub]", "contact.github")])
        self.assertEqual(row(plan, "urls[Portfolio]").note, "Add your website address to your profile", "no confirmed website: nothing is made up")
        for key in ("urls[Twitter]", "urls[Other]"):
            with self.subTest(key=key):
                self.assertEqual((row(plan, key).source.kind, row(plan, key).disposition), ("none", "blank"))
        demo = lever_plan("demo_eeo_survey.html", LEVER_COMPANY)
        for key in ("urls[Other Website]", "urls[Video Link ]"):
            with self.subTest(key=key):
                self.assertEqual((row(demo, key).source.kind, row(demo, key).disposition), ("none", "blank"))

    def test_a_required_link_the_student_has_no_fact_for_points_to_the_profile(self):
        facts = {**FACTS, "contact": {k: v for k, v in FACTS["contact"].items() if k != "linkedin"}}
        schema = [dataclasses.replace(item, required=True) if item.name == "urls[LinkedIn]" else item for item in schema_of("many_cards.html")]
        plan = build_plan(schema, None, sources(facts=facts, labels={"location": LOCATION}), "Orbital Ledger", "handoff", ats_name="Lever", ats="lever")
        self.assertEqual((row(plan, "urls[LinkedIn]").problem_kind, row(plan, "urls[LinkedIn]").disposition), ("profile_fact", "left_for_you"))

    def test_pronouns_are_never_planned_even_with_every_kind_of_answer_stored(self):
        stored = Store(entry("eeo_gender", "Pronouns", "Decline to self-identify"))
        src = sources(facts=FACTS, allowed={"eeo_gender"}, store=stored, answers=[answer("Pronouns", "she/her", LEVER_COMPANY)])
        pronouns = row(lever_plan("demo_eeo_survey.html", LEVER_COMPANY, src=src), "pronouns")
        self.assertEqual((pronouns.source.kind, pronouns.disposition, pronouns.value), ("none", "blank", None))

    def test_comments_and_the_office_have_no_source_and_a_required_office_is_the_students(self):
        plan = lever_plan("variants.html", "Tidewater Games")
        office = row(plan, "opportunityLocationId")
        self.assertEqual((office.required, office.problem_kind, office.disposition), (True, "window", "left_for_you"))
        self.assertEqual((row(lever_plan("many_cards.html", "Orbital Ledger"), "comments").source.kind), "none")

    def test_the_marketing_consent_is_never_ticked_even_with_a_consent_stored(self):
        stored = Store(entry("consent", "Orbital Ledger has my consent to contact me about future roles", "checked", kind="checkbox"))
        src = sources(facts=FACTS, labels={"location": LOCATION}, allowed={"consent"}, store=stored)
        consent = row(lever_plan("many_cards.html", "Orbital Ledger", src=src), "consent[marketing]")
        self.assertEqual((consent.source.kind, consent.value, consent.disposition), ("none", None, "blank"))

    def test_a_home_address_is_never_planned(self):
        plan = lever_plan("variants.html", "Tidewater Games")
        for key in ("residentialLocation[street]", "residentialLocation[city]"):
            with self.subTest(key=key):
                self.assertEqual((row(plan, key).source.kind, row(plan, key).disposition), ("none", "blank"))

    def test_the_disability_question_and_its_signature_and_date_are_never_planned_even_with_a_decline_stored(self):
        stored = Store(
            entry("eeo_disability", "Disability status", "I do not want to answer"),
            entry("eeo_disability", "Name", "Sam Rivera", kind="text"), entry("eeo_disability", "Date", "2026-10-08", kind="text"),
        )
        src = sources(facts=FACTS, labels={"location": LOCATION}, allowed={"eeo_disability", "eeo_gender", "eeo_race", "eeo_veteran"}, store=stored)
        plan = lever_plan("demo_eeo_survey.html", LEVER_COMPANY, src=src)
        for key in ("disability_status", "eeo[disabilitySignature]", "eeo[disabilitySignatureDate]"):
            with self.subTest(key=key):
                self.assertEqual((row(plan, key).source.kind, row(plan, key).value, row(plan, key).disposition), ("none", None, "blank"))
        self.assertIn("typed signature and a date", row(plan, "disability_status").note)

    def test_gender_race_and_veteran_take_only_a_stored_decline_the_student_switched_on(self):
        declines = Store(
            entry("eeo_gender", "Gender", "Decline to self-identify"), entry("eeo_race", "Race", "Decline to self-identify"),
            entry("eeo_veteran", "Veteran status", "Decline to self-identify"),
        )
        on = lever_plan("cards_files_consent.html", "Quillfeather Pets", src=sources(facts=FACTS, allowed={"eeo_gender", "eeo_race", "eeo_veteran"}, store=declines))
        self.assertEqual([(row(on, key).source.kind, row(on, key).disposition) for key in ("gender", "race", "veteran_status")], [("sensitive", "fill")] * 3)
        for key in ("gender", "race", "veteran_status"):
            self.assertTrue(row(on, key).value.lower().startswith(("decline", "i decline")), "only a decline is ever the value")
        off = lever_plan("cards_files_consent.html", "Quillfeather Pets", src=sources(facts=FACTS, store=declines))
        self.assertEqual([(row(off, key).source.kind, row(off, key).disposition) for key in ("gender", "race", "veteran_status")], [("none", "blank")] * 3)

    def test_a_survey_question_about_a_demographic_topic_is_never_filled(self):
        src = sources(facts=FACTS, allowed={"eeo_gender", "eeo_race"}, answers=[answer("What is your age range?", "25-34", LEVER_COMPANY)])
        plan = lever_plan("demo_eeo_survey.html", LEVER_COMPANY, src=src)
        for text in ("What is your age range?", "What gender do you identify as?", "I identify my ethnicity as"):
            with self.subTest(text=text):
                self.assertEqual((card(plan, text).source.kind, card(plan, text).disposition), ("none", "blank"))

    def test_a_card_text_question_takes_an_exact_saved_answer_for_this_company_only(self):
        question = "Please share why you feel that you are a good fit for this role"
        listed = [item.question for item in lever_plan("cards_files_consent.html", "Quillfeather Pets").fields if item.question.startswith("Please share")]
        self.assertEqual(len(listed), 1)
        full = listed[0]
        here = lever_plan("cards_files_consent.html", "Quillfeather Pets", src=sources(facts=FACTS, answers=[answer(full, "I like fixing things", "Quillfeather Pets")]))
        self.assertEqual((card(here, question).source.kind, card(here, question).value), ("answer", "I like fixing things"))
        elsewhere = lever_plan("cards_files_consent.html", "Quillfeather Pets", src=sources(facts=FACTS, answers=[answer(full, "I like fixing things", "Orbital Ledger")]))
        self.assertEqual((card(elsewhere, question).source.kind, card(elsewhere, question).problem_kind), ("none", "missing_answer"))

    def test_a_dropdown_takes_the_option_whose_label_equals_the_answer_and_never_the_first_one(self):
        field = next(item for item in schema_of("many_cards.html") if item.label.startswith("Please tell us how you heard"))
        options = list(field.options)
        self.assertGreater(len(options), 2)
        good = lever_plan("many_cards.html", "Orbital Ledger", src=sources(facts=FACTS, labels={"location": LOCATION}, answers=[answer(field.label, options[1], "Orbital Ledger")]))
        self.assertEqual((card(good, field.label[:20]).value, card(good, field.label[:20]).source.kind), (options[1], "answer"))
        bad = lever_plan("many_cards.html", "Orbital Ledger", src=sources(facts=FACTS, labels={"location": LOCATION}, answers=[answer(field.label, "A friend of a friend", "Orbital Ledger")]))
        self.assertEqual((card(bad, field.label[:20]).value, card(bad, field.label[:20]).problem_kind), (None, "answer_mismatch"))
        self.assertNotEqual(card(bad, field.label[:20]).value, options[0])

    def test_a_required_group_of_boxes_is_left_to_the_student_the_shared_net_never_ticks_a_box_from_a_saved_answer(self):
        # The Lever spec lets a plan tick the chosen boxes of a multiple-select; the Phase 5 net (spec 7.1 "As built") has never ticked a
        # box from the answer library, and this milestone keeps it: the whole group is left, and says so.
        field = next(item for item in schema_of("many_cards.html") if item.label.startswith("Language Skill"))
        plan = lever_plan("many_cards.html", "Orbital Ledger", src=sources(facts=FACTS, labels={"location": LOCATION}, answers=[answer(field.label, field.options[0], "Orbital Ledger")]))
        group = card(plan, "Language Skill")
        self.assertEqual((group.control, group.source.kind, group.value, group.disposition), ("multiselect", "none", None, "left_for_you"))

    def test_a_consent_or_certification_box_is_left_unless_an_exact_statement_is_stored(self):
        plan = lever_plan("cards_files_consent.html", "Quillfeather Pets")
        certify = card(plan, "I certify that the answers")
        self.assertEqual((certify.control, certify.source.kind, certify.disposition, certify.required), ("checkbox", "none", "left_for_you", True))

    def test_a_cover_letter_file_card_and_every_other_file_field_are_left_to_the_student(self):
        plan = lever_plan("cards_files_consent.html", "Quillfeather Pets")
        letter = card(plan, "Cover Letter")
        self.assertEqual((letter.control, letter.source.kind, letter.disposition, letter.problem_kind or "window"), ("file", "none", "blank", "window"))
        self.assertIn("attaches no file", letter.note)

    def test_the_resume_is_the_students_to_attach_unless_they_let_the_app(self):
        off = row(lever_plan("cards_files_consent.html", "Quillfeather Pets"), "resume")
        self.assertEqual((off.required, off.problem_kind, off.source.kind, off.disposition), (True, "window", "none", "left_for_you"))
        self.assertIn("Lever reads it as soon as it is attached", off.problem)
        src = sources(facts=FACTS, labels={"location": LOCATION})
        src.resume_upload = True
        on = row(lever_plan("cards_files_consent.html", "Quillfeather Pets", src=src), "resume")
        self.assertEqual((on.source.kind, on.disposition, on.file_name, on.problem_kind), ("resume", "fill", "Sam Rivera Resume.pdf", ""))

    def test_a_control_the_app_does_not_know_is_the_students_with_the_reason(self):
        # A control the parser has no family for, required, is a problem the student is told about.
        extra = lever_fixture_text("variants.html").replace("</form>", '<input type="date" name="startDate" required></form>', 1)
        unknown = apply_ats.lever_parse_schema({"lever_form": lever_form.parse_lever_form(extra)})
        plan = build_plan(unknown, None, sources(facts=FACTS, labels={"location": LOCATION}), "Tidewater Games", "handoff", ats_name="Lever", ats="lever")
        start = row(plan, "startDate")
        self.assertEqual((start.required, start.problem_kind, start.disposition), (True, "window", "left_for_you"))
        self.assertIn("Lever's form has a question the app doesn't read", start.problem)

    def test_an_unreadable_or_unknown_control_with_a_sensitive_sounding_label_keeps_the_reason_and_asks_for_no_stored_answer(self):
        # The app can never fill these controls, so telling the student to store a salary or sponsorship answer for them would be wrong.
        for kind in (lever.UNREADABLE_TYPE, lever.UNKNOWN_TYPE):
            for label in ("What is your desired salary?", "Will you now or in the future require visa sponsorship?", "Gender"):
                with self.subTest(kind=kind, label=label):
                    field = SchemaField(name="cards[x][field0]", label=label, required=True, type=kind, section="standard", description="the page and its description disagree")
                    src = sources(facts=FACTS, labels={"location": LOCATION}, allowed=("salary", "sponsorship", "gender"))
                    plan = build_plan([field], None, src, "Fixture Co", "handoff", ats_name="Lever", ats="lever")
                    (entry,) = plan.fields
                    self.assertEqual((entry.problem_kind, entry.disposition), ("window", "left_for_you"))
                    self.assertIn("Lever's form has a question the app doesn't read", entry.problem)
                    self.assertIn("the page and its description disagree", entry.problem)
                    self.assertNotIn("Apply agent settings", entry.problem)

    def test_the_plan_hash_is_stable_and_holds_no_value(self):
        first, second = lever_plan("demo_eeo_survey.html", LEVER_COMPANY), lever_plan("demo_eeo_survey.html", LEVER_COMPANY)
        self.assertEqual(first.plan_hash, second.plan_hash)
        self.assertNotIn("Rivera", json.dumps([item.value_mac for item in first.fields]))

    def test_the_same_form_is_planned_the_greenhouse_way_when_asked_for_greenhouse(self):
        # The Lever rows apply to a Lever plan only: a field that happens to be named "name" on a Greenhouse listing is an ordinary question there.
        plan = build_plan(schema_of("demo_eeo_survey.html"), None, sources(facts=FACTS), LEVER_COMPANY, "handoff", ats_name="Greenhouse")
        self.assertEqual(row(plan, "name").source.kind, "none")


if __name__ == "__main__":
    unittest.main()
