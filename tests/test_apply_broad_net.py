"""Apply for me's broad net (apply_classify.NET_TOPICS, docs/phase5-apply-agent-spec.md 7.3 "As built").

The precise classifier recognizes sensitive and agreement questions from listed wordings, and every review round found
wordings the lists miss. The net is a second, deliberately wide reading that never marks a question sensitive by itself but
fails closed: a question it hits (or one filed under or following such a question) is company-only, one that hits a
never-storable topic is left for the student and is never saved to or filled from the answer library, and a box that agrees
to something in any words is never ticked from the answer library. No browser and no network; every company, form and
answer is fictional. The wordings under test are the ones the review of PR #52 found the lists missed.
"""

import copy
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from opportunity_app.apply import classify as apply_classify, policy as apply_policy, preflight as apply_preflight, sensitive as apply_sensitive
from opportunity_app.apply.checks import question_key
from opportunity_app.apply.classify import classify_item, net_topics, never_storable, possibly_sensitive
from opportunity_app.apply.policy import SchemaField
from opportunity_app.apply.sensitive import StoreRefused
from pipeline_core.identity import employer_key

import helpers_apply as apply_helpers
from helpers_apply import BASE, COMPANY, F, MULTI, OTHER, SINGLE, USER, answer, kinds, plan, sources
# unittest and pytest run the module fixtures they find in the test module's namespace.
from helpers_apply import setUpModule, tearDownModule  # noqa: F401
from helpers_source import static_script_text

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "apply"
FELONY = "Have you ever been convicted of a felony?"
VISA = "Do you currently hold a visa?"
YES_NO = ("Yes", "No")


def net_vectors():
    return json.loads((FIXTURES / "broad_net.json").read_text(encoding="utf-8"))["vectors"]


def box(label, option, description="", name="q", parent=""):
    return SchemaField(name=name, label=label, required=True, type=MULTI, options=(option,), description=description, parent=parent)


def yes_no(label, description="", name="q", parent=""):
    return SchemaField(name=name, label=label, required=True, type=SINGLE, options=YES_NO, description=description, parent=parent)


def library_row(fields, key, text="Answer", company=COMPANY, tags=()):
    """A saved answer, filed under the words the plan files this field under, so it is exactly the row a save would have made."""
    return answer(plan(BASE + fields).get(key).answer_key, text, company, tags)


class VectorTests(unittest.TestCase):
    def test_the_net_matches_the_shared_vectors_the_javascript_engine_also_runs(self):
        rows = net_vectors()
        self.assertGreaterEqual(len(rows), 120)
        for row in rows:
            self.assertEqual(list(net_topics(row["text"])), row["topics"], row["text"])
            self.assertEqual(possibly_sensitive(row["text"]), row["possibly_sensitive"], row["text"])
            self.assertEqual(never_storable(row["text"]), row["never_storable"], row["text"])

    def test_every_topic_and_every_never_storable_topic_is_covered_by_the_vectors(self):
        seen = {topic for row in net_vectors() for topic in row["topics"]}
        self.assertEqual(seen, set(apply_classify.NET_TOPICS) | {"adult"})
        self.assertTrue(set(apply_classify.NEVER_STORABLE_TOPICS) <= set(apply_classify.NET_TOPICS))
        self.assertEqual(set(apply_classify.NEVER_STORABLE_TOPICS), {"criminal", "demographic", "money", "security"})

    def test_it_reads_stems_and_short_phrases_not_sentence_shapes(self):
        for text in ("visa", "VISA'S", "h-1b", "H1B", "f 1", "U.S. person", "date of birth", "e-sign", "Statement"):
            self.assertTrue(possibly_sensitive(text), text)
        for text in ("", None, "  ", "optional", "option", "manage a team", "image", "cybersecurity"):
            self.assertFalse(possibly_sensitive(text), repr(text))

    def test_an_eighteen_or_older_wording_is_not_a_demographic_one(self):
        self.assertEqual(net_topics("Are you at least 18 years of age?"), ("adult",))
        self.assertEqual(net_topics("Must be eighteen years of age"), ("adult",))
        self.assertFalse(never_storable("Are you 18 or older?"))
        self.assertIn("demographic", net_topics("Are you over 18 and a veteran?"))
        self.assertIn("demographic", net_topics("What is your age?"))

    def test_ordinary_questions_are_untouched(self):
        for text in ("Why are you interested in this role?", "Tell us about yourself", "Describe a time you worked on a team", "Are you willing to relocate?",
                     "What is your expected graduation date?", "LinkedIn profile", "Portfolio URL", "What programming languages do you know?"):
            self.assertEqual(net_topics(text), (), text)


class OrdinaryQuestionsFillOnlyAtTheirOwnCompanyTests(unittest.TestCase):
    ORDINARY = ("Tell us about yourself", "Describe a time you worked on a team", "Are you willing to relocate?",
                "What is your expected graduation date?", "What programming languages do you know?")

    def test_an_answer_saved_at_this_company_fills_an_ordinary_question_and_one_saved_elsewhere_never_does(self):
        for index, label in enumerate(self.ORDINARY):
            with self.subTest(label=label):
                field = F("q", label, "textarea", parent="Resume/CV")
                got = plan(BASE + [field], sources(answers=[answer(label, "Answer", COMPANY, ["reusable"])])).get("q")
                self.assertEqual((got.net, got.net_never, got.net_company, got.context_dependent), ((), (), False, False), label)
                self.assertEqual((got.source.kind, got.value, got.source.reusable), ("answer", "Answer", False), label)
                for row in (answer(label, "Answer", OTHER, ["reusable"]), answer(label, "Answer", "", ["reusable"]), answer(label, "Answer", OTHER)):
                    away = plan(BASE + [field], sources(answers=[row])).get("q")
                    self.assertEqual((away.source.kind, away.value, away.problem_kind), ("none", None, "missing_answer"), label)

    def test_this_role_is_already_company_bound_by_the_older_wording_rule_and_the_net_adds_nothing(self):
        field = F("q", "Why are you interested in this role?", "textarea", parent="Resume/CV")
        got = plan(BASE + [field]).get("q")
        self.assertEqual((got.net, got.net_company, got.context_dependent), ((), False, True))

    def test_profile_links_come_from_the_profile_whatever_stands_above_them(self):
        field = F("q", "LinkedIn profile", "input_text", parent=FELONY)
        facts = copy.deepcopy(apply_helpers.FACTS)
        facts["contact"]["linkedin"] = "https://linkedin.example/in/sam"
        got = plan(BASE + [yes_no(FELONY, name="p", parent="Resume/CV"), field], sources(facts=facts)).get("q")
        self.assertEqual((got.source.kind, got.net_never), ("profile", ()))


class WorkAuthorizationAndVisaWordingTests(unittest.TestCase):
    """Wordings the precise lists missed: never from a row saved elsewhere, and never offered as reusable."""

    WORDINGS = (
        "Are you permitted to work in the United States?", "Do you have unrestricted work rights in the United States?", "US work eligibility",
        "Will you be able to provide proof of employment eligibility?", "Are you able to work in the United States without restriction?",
        "Are you allowed to work in the US?", "Can you work in the U.S. without restrictions?", "Do you have permission to work in the United States?",
        "Are you currently able to work in the US full time?", "Please confirm your employment eligibility status",
        "Your visa's expiration date", "Your visa’s expiration date", "Visa's expiration date", "When does your visa's validity end?",
        "Please enter your visa's end date",
    )

    def test_none_of_them_is_read_from_a_row_saved_for_another_company_or_for_none(self):
        for label in self.WORDINGS:
            for kind in ("input_text", SINGLE):
                with self.subTest(label=label, kind=kind):
                    options = YES_NO if kind == SINGLE else ()
                    field = F("q", label, kind, options=options, parent="Resume/CV")
                    tagged = answer(label, "Yes" if options else "Answer", OTHER, ["reusable"])
                    blank = answer(label, "Yes" if options else "Answer", "", ["reusable"])
                    for row in (tagged, blank):
                        got = plan(BASE + [field], sources(answers=[row])).get("q")
                        self.assertNotEqual(got.source.kind, "answer", "a row saved elsewhere carried over")
                        # A Yes/No question that also reads as agreeing ("permission", "confirm") is left for the student outright.
                        self.assertIn(got.problem_kind, ("missing_answer", "sensitive_never"))
                        if got.problem_kind == "missing_answer":
                            self.assertTrue(got.context_dependent, "so the view offers no Use for any company")
                            self.assertTrue(got.net_company)
                        else:
                            self.assertIn("agreement", got.net_never)
                    # The student's own answer for this company still fills it, unless it is left for her outright.
                    here = plan(BASE + [field], sources(answers=[answer(label, "Yes" if options else "Answer", COMPANY)])).get("q")
                    if here.problem_kind != "sensitive_never":
                        self.assertEqual((here.source.kind, here.value), ("answer", "Yes" if options else "Answer"))

    def test_the_view_offers_no_tick_for_any_company(self):
        for label in self.WORDINGS:
            with self.subTest(label=label):
                got = plan(BASE + [F("q", label, "input_text", parent="Resume/CV")]).get("q")
                action = apply_preflight._action(got, {})
                self.assertEqual(action["type"], "answer")
                self.assertNotIn("reusable_allowed", action)


class FollowUpUnderASensitiveQuestionTests(unittest.TestCase):
    CRIMINAL_CHILDREN = ("Please tell us what happened", "Tell us about it", "Sentence received and date of release", "When?", "Details",
                         "Year", "Location", "County", "Court", "Nature of charge", "What was the outcome?", "Describe the circumstances")
    VISA_CHILDREN = ("What is the expiration date of your current status?", "Your visa's expiration date", "Expiration date", "Type", "Category",
                     "Which one?", "When does it expire?", "Valid until")

    def form(self, parent_label, child_label, kind="textarea"):
        parent = yes_no(parent_label, name="p", parent="Resume/CV")
        return [parent, F("q", child_label, kind, parent=parent_label)]

    def test_a_follow_up_under_a_felony_question_is_never_saved_or_filled_from_the_library(self):
        for label in self.CRIMINAL_CHILDREN:
            with self.subTest(label=label):
                fields = self.form(FELONY, label)
                keyed = plan(BASE + fields).get("q").answer_key
                rows = [answer(keyed, "Details", COMPANY), answer(keyed, "Details", OTHER, ["reusable"]), answer(keyed, "Details", "", ["reusable"]),
                        answer(label, "Details", COMPANY)]
                for company in (COMPANY, OTHER):
                    got = plan(BASE + fields, sources(answers=rows), company=company).get("q")
                    self.assertNotEqual(got.source.kind, "answer", "never from the library, at any company")
                    # The precise rules already read most of them as the felony question's continuation (sensitive, never stored);
                    # what they miss the net reads: it takes the parent's criminal topic whatever the child says.
                    self.assertTrue(got.sensitive is not None or "criminal" in got.net_never, label)
                    self.assertEqual((got.problem_kind, got.disposition, got.value), ("sensitive_never", "blank", None))
                    self.assertTrue(got.sensitive is not None or "criminal history" in got.problem)
                    action = apply_preflight._action(got, {})
                    self.assertEqual(action["type"], "manual", "no form to save it")
        # These two are the ones only the net catches.
        for label in ("Please tell us what happened", "Sentence received and date of release"):
            self.assertIsNone(plan(BASE + self.form(FELONY, label)).get("q").sensitive, label)
            self.assertIn("criminal", plan(BASE + self.form(FELONY, label)).get("q").net_never, label)

    def test_a_follow_up_under_a_visa_question_is_company_only(self):
        for label in self.VISA_CHILDREN:
            with self.subTest(label=label):
                fields = self.form(VISA, label, "input_text")
                got = plan(BASE + fields).get("q")
                if got.sensitive is not None:
                    # The precise rules read it as the visa question's continuation: it goes through the store, never the library.
                    self.assertEqual(apply_preflight._action(got, {})["type"], "manual")
                else:
                    self.assertTrue(got.context_dependent)
                    self.assertNotIn("reusable_allowed", apply_preflight._action(got, {}))
                keyed = got.answer_key
                for elsewhere in (answer(keyed, "F-1", OTHER, ["reusable"]), answer(keyed, "F-1", "", ["reusable"]), answer(label, "F-1", OTHER, ["reusable"])):
                    other = plan(BASE + fields, sources(answers=[elsewhere]), company=COMPANY).get("q")
                    self.assertNotEqual(other.source.kind, "answer", "a row saved elsewhere carried over")

    def test_a_question_that_only_comes_after_a_sensitive_one_takes_its_topics_but_passes_none_on(self):
        parent = yes_no(FELONY, name="p", parent="Resume/CV")
        next_one = F("a", "Tell us about a project you are proud of", "textarea", parent=FELONY)
        after = F("b", "What programming languages do you know?", "textarea", parent="Tell us about a project you are proud of")
        got = plan(BASE + [parent, next_one, after])
        self.assertIn("criminal", got.get("a").net_never, "right after a felony question: left for the student")
        self.assertEqual((got.get("b").net, got.get("b").net_never, got.get("b").context_dependent), ((), (), False))

    def test_a_follow_up_chain_keeps_the_first_questions_topics(self):
        parent = yes_no(FELONY, name="p", parent="Resume/CV")
        child = F("a", "If yes, please explain", "textarea", parent=FELONY)
        grandchild = F("b", "Please provide more details", "textarea", parent="If yes, please explain")
        got = plan(BASE + [parent, child, grandchild])
        for key in ("a", "b"):
            self.assertTrue(got.get(key).sensitive is not None or "criminal" in got.get(key).net_never, key)
            self.assertNotEqual(got.get(key).source.kind, "answer")


class NeverStorableTopicTests(unittest.TestCase):
    WORDINGS = (
        ("What sentence were you given?", "criminal"), ("Are you currently on probation or parole?", "criminal"), ("Do you have any pending cases?", "criminal"),
        ("Court date", "criminal"), ("What is your date of birth?", "demographic"), ("How old are you?", "demographic"),
        ("Military service", "demographic"), ("Are you okay with the stipend?", "money"), ("Wage requirements", "money"),
        ("Are you interested in security engineering?", "security"), ("Polygraph", "security"),
    )

    def test_a_question_that_hits_a_never_storable_topic_is_never_read_from_the_library_even_here(self):
        for label, topic in self.WORDINGS:
            with self.subTest(label=label):
                field = F("q", label, "input_text", parent="Resume/CV")
                if apply_classify.classify_sensitive(label):
                    continue   # the precise classifier already sends it through the store, and the store never holds it
                got = plan(BASE + [field], sources(answers=[answer(label, "Answer", COMPANY), answer(label, "Answer", OTHER, ["reusable"])])).get("q")
                self.assertEqual((got.source.kind, got.problem_kind, got.value), ("none", "sensitive_never", None))
                self.assertIn(topic, got.net_never)
                self.assertIsNone(got.sensitive, "the net never marks a question sensitive by itself")

    def test_an_optional_one_is_a_note_not_a_problem(self):
        field = F("q", "Are you okay with the stipend?", "input_text", required=False, parent="Resume/CV")
        result = plan(BASE + [field])
        self.assertEqual(kinds(result), {})
        self.assertEqual((result.get("q").disposition, result.get("q").source.kind), ("blank", "none"))
        self.assertIn("never saves an answer", result.get("q").note)

    def test_a_select_option_list_is_read_only_for_the_topics_a_status_is_answered_in(self):
        team = F("q", "Which team are you most interested in?", SINGLE, options=("Platform", "Security", "Data"), parent="Resume/CV")
        got = plan(BASE + [team], sources(answers=[answer("Which team are you most interested in?", "Platform", COMPANY)])).get("q")
        self.assertEqual((got.net_never, got.source.kind), ((), "answer"), "a team called Security is not a clearance question")
        status = F("s", "Please select one", SINGLE, options=("Veteran", "Not a veteran"), parent="Resume/CV")
        self.assertIn("demographic", plan(BASE + [status]).get("s").net_never)


class AgreementQuestionTests(unittest.TestCase):
    """A box or Yes/No question that agrees to something in words the classifier has no list for is never ticked from the library."""

    BOXES = (
        ("Code of Ethics", "I will comply with the Code of Ethics"), ("Declaration", "I hereby declare that the information above is true"),
        ("Your rights", "I have been informed of my rights under state law"), ("Code of Conduct", "I have reviewed and will abide by the Code of Conduct"),
        ("Reference check", "I authorize Example Robotics to contact my references and former employers"),
        ("Data", "Example Robotics may keep my application on file for two years"), ("Candidate Notice", "Yes"),
        ("Arbitration", "I agree to the arbitration rules"), ("Arbitration Program", "I will be bound by the Mutual Arbitration Program"),
    )
    QUESTIONS = (
        "Do you agree to arbitrate disputes?", "Do you confirm the information you provided is true?",
        "Do you give permission for us to keep your resume on file?", "Do you accept the Code of Business Conduct?", "Do you agree to our Candidate Terms?",
    )

    def test_a_box_that_agrees_in_any_words_is_never_filled_from_the_library(self):
        for label, option in self.BOXES:
            with self.subTest(label=label, option=option):
                field = box(label, option, "<p>The notice is at <a href=\"https://example-robotics.test/notice-2026\">this page</a>.</p>", parent="Resume/CV")
                keyed = plan(BASE + [field]).get("q").answer_key
                rows = [answer(keyed, "Yes", COMPANY), answer(label, "Yes", COMPANY), answer(keyed, "Yes", OTHER, ["reusable"]), answer(keyed, "Yes", "", ["reusable"])]
                for company in (COMPANY, OTHER):
                    got = plan(BASE + [field], sources(answers=rows), company=company).get("q")
                    self.assertNotEqual(got.source.kind, "answer", f"ticked from the library at {company}")
                    self.assertNotEqual(got.value, True)
                    if got.sensitive is None:
                        self.assertIn("agreement", got.net_never, label)
                        self.assertEqual(apply_preflight._action(got, {})["type"], "manual")

    def test_a_yes_no_question_that_agrees_in_any_words_is_never_filled_from_the_library(self):
        for label in self.QUESTIONS:
            with self.subTest(label=label):
                field = yes_no(label, parent="Resume/CV")
                keyed = plan(BASE + [field]).get("q").answer_key
                rows = [answer(keyed, "Yes", COMPANY), answer(label, "Yes", COMPANY), answer(keyed, "Yes", OTHER, ["reusable"]), answer(keyed, "Yes", "", ["reusable"])]
                for company in (COMPANY, OTHER):
                    got = plan(BASE + [field], sources(answers=rows), company=company).get("q")
                    self.assertNotEqual(got.source.kind, "answer", f"answered from the library at {company}")
                    self.assertNotEqual(got.value, "Yes")

    def test_a_marketing_box_is_ordinary_but_a_box_is_never_ticked_from_the_library_and_a_plain_preference_fills_only_here(self):
        marketing = box("Updates", "Keep me informed about future openings at Example Robotics", parent="Resume/CV")
        got = plan(BASE + [marketing], sources(answers=[answer(plan(BASE + [marketing]).get("q").answer_key, "Yes", COMPANY)])).get("q")
        self.assertEqual((got.sensitive, got.net_never, got.source.kind, got.value), (None, ("tick",), "none", None), "the accepted cost: ordinary boxes are left for the student")
        relocate = yes_no("Are you willing to relocate?", parent="Resume/CV")
        keyed = plan(BASE + [relocate]).get("q").answer_key
        got = plan(BASE + [relocate], sources(answers=[answer(keyed, "Yes", OTHER, ["reusable"])])).get("q")
        self.assertEqual((got.source.kind, got.value), ("none", None))
        got = plan(BASE + [relocate], sources(answers=[answer(keyed, "Yes", COMPANY)])).get("q")
        self.assertEqual((got.source.kind, got.value), ("answer", "Yes"))

    def test_only_an_exact_stored_statement_ticks_it(self):
        field = box("Code of Ethics", "I will comply with the Code of Ethics", parent="Resume/CV")
        statement = apply_classify.statement_of(field, "checkbox", "acknowledgment", plan(BASE + [field]).get("q").answer_key)
        store = apply_helpers.Store(apply_helpers.entry("acknowledgment", statement, "checked", "checkbox", company_key=employer_key(COMPANY)))
        got = plan(BASE + [field], sources(allowed=["acknowledgment"], store=store)).get("q")
        self.assertEqual((got.sensitive, got.source.kind, got.value), ("acknowledgment", "sensitive", True))


class StorePlanCase(apply_helpers.StoreCase):
    """A real store, and a plan built against it (the same helpers PlanFromTheStoreTests uses)."""

    def sources(self):
        src = apply_policy.sources_for(self.conn, USER, "job-a", company=COMPANY, key=apply_helpers.KEY)
        src.facts, src.resume, src.cover_letter = copy.deepcopy(apply_helpers.FACTS), dict(apply_helpers.RESUME_OK), dict(apply_helpers.LETTER_NONE)
        return src

    def plan(self, fields, mode="submit", company=COMPANY):
        return apply_helpers.plan(fields, self.sources(), mode, company=company)


class StatementCompanyRuleTests(StorePlanCase):
    """A statement that agrees to anything but a plain "what I wrote is true" is kept for one company (D9 B)."""

    ARBITRATION = ("I agree to the arbitration rules", "I agree to Orbit Systems' Candidate Code", "I agree to abide by Orbit's Applicant Conduct Rules",
                   "I accept Orbit Systems' Global Recruiting Principles", "I acknowledge Orbit Systems' Pay Transparency posters",
                   "I acknowledge receiving the Summary of Your Rights Under the FCRA", "I agree to the Candidate Terms",
                   "I consent to Example Robotics storing my application data for 365 days", "I understand that employment is at will")

    def test_none_of_them_is_stored_or_offered_for_any_company(self):
        self.allow("acknowledgment", "consent")
        for statement in self.ARBITRATION:
            with self.subTest(statement=statement):
                self.assertTrue(apply_sensitive.cites_document(statement), statement)
                category = "consent" if "consent" in statement else "acknowledgment"
                self.refused("never for any company", category=category, question=statement, answer="checked")
                field = box("Arbitration", statement)
                got = self.plan(apply_helpers.BASE + [field]).get("q")
                self.assertTrue(got.company_only, "no Use for any company")
                self.assertTrue(apply_preflight._sensitive_form(got, "sensitive_missing")["company_only"])
        self.assertEqual(self.rows(), [])

    def test_a_row_saved_for_any_company_by_another_route_is_ignored_at_every_company(self):
        self.allow("acknowledgment")
        field = box("Arbitration", "I agree to the arbitration rules")
        statement = apply_classify.statement_of(field, "checkbox")
        self.add(category="acknowledgment", question=statement, answer="checked", company=COMPANY)
        with self.conn:
            self.conn.execute("UPDATE apply_sensitive_answers SET company_key=''")
        for company in (COMPANY, OTHER):
            got = self.plan(apply_helpers.BASE + [field], company=company).get("q")
            self.assertEqual((got.problem_kind, got.source.kind, got.value), ("sensitive_missing", "none", None), company)

    def test_even_a_plain_certification_that_the_students_answers_are_true_is_kept_for_one_company(self):
        self.allow("acknowledgment")
        for statement in (apply_helpers.ACCURATE, "I certify that all of this is true", f"Certification {apply_helpers.ACCURATE}"):
            with self.subTest(statement=statement):
                self.assertFalse(apply_sensitive.cites_document(statement), "no document by the word test, and still one company only")
                self.refused("never for any company", category="acknowledgment", question=statement, answer="checked")
        saved = self.add(category="acknowledgment", question=apply_helpers.ACCURATE, answer="checked", company=COMPANY)
        self.assertFalse(saved["any_company"])
        # Any other word makes it something the app cannot prove is not a document.
        self.assertTrue(apply_sensitive.cites_document(apply_helpers.ACCURATE + " and I will follow the handbook"))
        self.assertTrue(apply_sensitive.cites_document(apply_helpers.ACCURATE + " and I agree to the arbitration rules"))
        # A statement about the student (work authorization) is not read as a document by this rule: it is not an agreement.
        self.assertFalse(apply_sensitive.cites_document("I am authorized to work in the United States", names=False))


class DemographicClaimTests(StorePlanCase):
    """A work-authorization, sponsorship or 18-or-older tick box whose statement also claims a demographic is never stored or ticked."""

    CLAIMS = (
        ("work_authorization", "Work eligibility", "I am authorized to work in the United States and I am a protected veteran"),
        ("work_authorization", "Work eligibility", "I am authorized to work in the United States and identify as Hispanic or Latino"),
        ("age_18", "Adult", "I confirm I am at least 18 years of age and I am female"),
        ("age_18", "Adult", "I confirm I am at least 18 years of age and have a disability"),
        ("sponsorship", "Sponsorship", "I will not require sponsorship and I am a military spouse"),
        ("work_authorization", "Work eligibility", "I am authorized to work in the United States and I am a woman"),
        ("work_authorization", "Work eligibility", "I am authorized to work in the United States, date of birth 1/1/2000"),
    )

    def test_the_plan_never_offers_it_and_the_store_never_holds_it(self):
        self.allow("work_authorization", "sponsorship", "age_18")
        for category, heading, option in self.CLAIMS:
            with self.subTest(option=option):
                field = box(heading, option)
                self.assertEqual(classify_item(field, "checkbox"), "uncategorized", "read as a personal question, not the kind it claims")
                got = self.plan(apply_helpers.BASE + [field]).get("q")
                self.assertEqual((got.sensitive, got.problem_kind, got.source.kind), ("uncategorized", "sensitive_never", "none"))
                self.assertEqual(apply_preflight._action(got, {})["type"], "manual", "no form")
                statement = apply_classify.statement_of(field, "checkbox", category)
                for from_form in (False, True):
                    for question in (statement, option):
                        with self.assertRaises(StoreRefused, msg=question):
                            self.add(category=category, question=question, answer="checked", answer_kind="checkbox", from_form=from_form)
        self.assertEqual(self.rows(), [])

    def test_a_legacy_row_for_any_company_is_not_ticked_at_another_employer(self):
        self.allow("work_authorization")
        option = "I am authorized to work in the United States and I am a protected veteran"
        field = box("Work eligibility", option)
        with self.conn:
            self.conn.execute(
                "INSERT INTO apply_sensitive_answers(id, user_id, category, question_text, question_key, question_hash, answer_kind, answer, company_key, "
                "statement_links_json, consent_scope, consented_at, created_at, updated_at) VALUES('legacy', ?, 'work_authorization', ?, ?, ?, 'checkbox', 'checked', '', '[]', "
                "'confirmed', '2026-09-01T00:00:00+00:00', '2026-09-01T00:00:00+00:00', '2026-09-01T00:00:00+00:00')",
                (USER, apply_classify.statement_of(field, "checkbox", "work_authorization"), question_key(apply_classify.statement_of(field, "checkbox", "work_authorization")),
                 __import__("hashlib").sha256(question_key(apply_classify.statement_of(field, "checkbox", "work_authorization")).encode()).hexdigest()),
            )
        got = self.plan(apply_helpers.BASE + [field], company=OTHER).get("q")
        self.assertNotEqual(got.source.kind, "sensitive")
        self.assertNotEqual(got.value, True)

    def test_the_plain_wordings_of_the_same_kinds_still_work(self):
        self.allow("work_authorization", "sponsorship", "age_18")
        for category, heading, option in (
            ("work_authorization", "Work eligibility", "I am authorized to work in the United States"),
            ("age_18", "Adult", "I confirm I am at least 18 years of age"),
            ("age_18", "Adult", "I am 18 years of age or older"),
            ("sponsorship", "Sponsorship", "I will not require visa sponsorship"),
        ):
            with self.subTest(option=option):
                field = box(heading, option)
                self.assertEqual(classify_item(field, "checkbox"), category)
                statement = apply_classify.statement_of(field, "checkbox", category)
                # A tick box is kept for one company: it is refused for any company and ticks only where it was saved.
                self.refused("never for any company", category=category, question=statement, answer="checked", answer_kind="checkbox")
                self.add(category=category, question=statement, answer="checked", answer_kind="checkbox", company=COMPANY)
                self.assertEqual(self.plan(apply_helpers.BASE + [field]).get("q").value, True)
                self.assertIsNone(self.plan(apply_helpers.BASE + [field], company=OTHER).get("q").value)
        self.assertEqual(len(self.rows()), 4)


class PreflightTests(apply_helpers.PolicyCase):
    """What the what's-missing view offers, and what answer_missing refuses, for a question the net leaves to the student or keeps for one company."""

    def listing(self, *questions):
        listing = copy.deepcopy(apply_helpers.SIMPLE)
        for index, (label, kind, values) in enumerate(questions, start=20):
            listing["questions"].append({"label": label, "required": True, "fields": [
                {"name": f"question_{index}", "type": kind, "values": [{"label": value, "value": number} for number, value in enumerate(values)]}]})
        self.client = apply_helpers.StaticClient(listing)
        self.role("gh-1")
        return [f"question_{index}" for index in range(20, 20 + len(questions))]

    def answer(self, key, text, reusable=False):
        return apply_preflight.answer_missing(self.conn, USER, "gh-1", key=key, answer=text, reusable=reusable, client=self.client, resume_root=self.resumes, now=self.at(0))

    def problem(self, key):
        return next(item for item in self.run_check()["problems"] if item["key"] == key)

    def test_a_never_storable_question_is_left_for_the_student_with_a_plain_reason_and_no_form(self):
        keys = self.listing(("Are you okay with the stipend?", "input_text", ()), (FELONY, SINGLE, YES_NO), ("Please tell us what happened", "textarea", ()))
        stipend, felony, what = keys
        for key in (stipend, what):
            problem = self.problem(key)
            self.assertEqual((problem["kind"], problem["action"]["type"]), ("sensitive_never", "manual"), key)
            self.assertIn("never saves an answer", problem["message"])
            with self.assertRaisesRegex(apply_preflight.AnswerRefused, "kind of question"):
                self.answer(key, "Anything")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM answer_library").fetchone()[0], 0)
        self.assertEqual(self.problem(felony)["action"]["type"], "manual")

    def test_a_company_only_question_offers_no_tick_and_refuses_it_and_is_saved_for_this_company(self):
        (key,) = self.listing(("Are you permitted to work in the United States?", SINGLE, YES_NO))
        problem = self.problem(key)
        self.assertEqual((problem["kind"], problem["action"]["type"], "reusable_allowed" in problem["action"]), ("missing_answer", "answer", False))
        with self.assertRaisesRegex(apply_preflight.AnswerRefused, "this company only"):
            self.answer(key, "Yes", reusable=True)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM answer_library").fetchone()[0], 0)
        self.answer(key, "Yes")
        self.assertEqual(json.loads(self.conn.execute("SELECT tags_json FROM answer_library").fetchone()[0]), [])
        # It fills at this company and is asked again at another one.
        self.assertNotIn(key, {item["key"] for item in self.run_check()["problems"]})
        other = self.role("gh-2", company=OTHER, job="4000000002")
        self.assertIn(key, {item["key"] for item in self.run_check(other)["problems"]})

    def test_a_reusable_row_saved_some_other_way_still_does_not_travel(self):
        (key,) = self.listing(("Do you have unrestricted work rights in the United States?", SINGLE, YES_NO))
        from opportunity_app.student import preparation
        preparation.save_answer(self.conn, "Do you have unrestricted work rights in the United States?", "Yes", apply_helpers.BLUEFIN, ["reusable"], user_id=USER)
        other = self.role("gh-2", company=OTHER, job="4000000002")
        problems = {item["key"]: item for item in self.run_check(other)["problems"]}
        self.assertEqual(problems[key]["kind"], "missing_answer")

    def test_an_ordinary_question_offers_no_tick_either_and_refuses_it(self):
        (key,) = self.listing(("Tell us about yourself", "textarea", ()))
        self.assertNotIn("reusable_allowed", self.problem(key)["action"])
        with self.assertRaisesRegex(apply_preflight.AnswerRefused, "this company only"):
            self.answer(key, "Anything", reusable=True)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM answer_library").fetchone()[0], 0)
        self.answer(key, "Anything")
        self.assertEqual(json.loads(self.conn.execute("SELECT tags_json FROM answer_library").fetchone()[0]), [])


# --- Round 2: the defaults are inverted so a wording no list recognizes cannot cause harm (spec 7.1 "As built") ---


class NoCrossCompanyReuseTests(unittest.TestCase):
    """A. In Apply for me an answer library row is used at the company it was saved for, and never at another, whatever it is tagged."""

    # Wordings no list in this repo recognizes as personal: the safety must not rest on a list.
    UNLISTED = ("Have you ever been refused a licence by a professional board?", "Have you ever been dismissed from a program for a rules violation?",
                "Is there anything in your past that could embarrass an employer?")

    def test_a_wording_no_list_recognizes_still_never_travels(self):
        for label in self.UNLISTED:
            with self.subTest(label=label):
                self.assertEqual(net_topics(label), (), "the lists really do not know it")
                self.assertIsNone(apply_classify.classify_sensitive(label))
                field = yes_no(label, parent="Resume/CV")
                for row in (answer(label, "No", OTHER, ["reusable"]), answer(label, "No", "", ["reusable"]), answer(label, "No", OTHER)):
                    got = plan(BASE + [field], sources(answers=[row])).get("q")
                    self.assertEqual((got.source.kind, got.value, got.problem_kind), ("none", None, "missing_answer"))
                # At the company it was saved for, it is that company's own answer (the residual case: docs/assisted-apply.md).
                here = plan(BASE + [field], sources(answers=[answer(label, "No", COMPANY)])).get("q")
                self.assertEqual((here.source.kind, here.value), ("answer", "No"))

    def test_the_reusable_tag_changes_nothing_in_the_plan(self):
        field = F("q", "What excites you about robotics?", "textarea", parent="Resume/CV")
        for tags in ([], ["reusable"], ["Reusable", "greenhouse"]):
            got = plan(BASE + [field], sources(answers=[answer("What excites you about robotics?", "Arms", OTHER, tags)])).get("q")
            self.assertEqual((got.source.kind, got.problem_kind, got.source.reusable), ("none", "missing_answer", False), tags)

    def test_profile_facts_are_unaffected_at_any_company(self):
        facts = copy.deepcopy(apply_helpers.FACTS)
        facts["contact"]["linkedin"] = "https://linkedin.example/in/sam"
        link = F("q", "LinkedIn profile", "input_text", parent="Resume/CV")
        for company in (COMPANY, OTHER):
            got = plan(BASE + [link], sources(facts=facts), company=company)
            self.assertEqual((got.get("q").source.kind, got.get("q").value), ("profile", "https://linkedin.example/in/sam"), company)
            self.assertEqual((got.get("first_name").value, got.get("email").value), ("Sam", "sam.rivera@example.test"), company)

    def test_the_view_never_offers_use_for_any_company_and_the_route_refuses_it(self):
        field = F("q", "What excites you about robotics?", "textarea", parent="Resume/CV")
        action = apply_preflight._action(plan(BASE + [field]).get("q"), {})
        self.assertEqual(sorted(action), ["answer_key", "control", "options", "type"])
        # Negative guards read every shipped script, so moving code out of app.js cannot turn them into no-ops.
        source = static_script_text()
        # The what's-missing form has no tick and sends no reusable flag. (The sensitive form keeps its own "Use for any company", which
        # is offered only for a select's exact option label or an EEO decline.)
        self.assertFalse("reusable_allowed" in source)
        self.assertFalse("reusable: Boolean" in source)
        self.assertFalse("action.reusable" in source)


class NoBoxOrAgreementFromTheLibraryTests(unittest.TestCase):
    """B. A checkbox, single or a group, and a select or radio whose options agree to something, is never filled from the answer library."""

    BOXES = (
        ("Code of Ethics", "I will adhere to the Code of Ethics at all times"), ("Candidate Code of Conduct", "I will follow the Candidate Code of Conduct during interviews"),
        ("Interview recording preferences for candidates", "I'm okay with my interviews being recorded"),
        ("How we use automated tools in hiring", "Allow Example Robotics to use AI tools to evaluate my application"),
        ("Future opportunities at the company", "Keep my information for future opportunities"), ("You may contact my references", "Yes"),
        ("Before you submit your application", "All information I have provided is complete and true"), ("Honesty", "I pledge that the information provided is true"),
        ("Confidentiality", "I will keep all interview materials confidential"), ("Skills", "I know Python"),
    )

    def rows(self, keyed):
        return [answer(keyed, "Yes", COMPANY), answer(keyed, "Yes", OTHER, ["reusable"]), answer(keyed, "Yes", "", ["reusable"]), answer(keyed, "true", COMPANY)]

    def test_no_single_box_is_ticked_from_the_library_at_any_company(self):
        for label, option in self.BOXES:
            with self.subTest(label=label):
                field = box(label, option, parent="Resume/CV")
                keyed = plan(BASE + [field]).get("q").answer_key
                for company in (COMPANY, OTHER):
                    got = plan(BASE + [field], sources(answers=self.rows(keyed) + [answer(label, "Yes", COMPANY)]), company=company).get("q")
                    self.assertNotEqual(got.source.kind, "answer", company)
                    self.assertNotEqual(got.value, True)
                    if got.sensitive is None:
                        self.assertIn("tick", got.net_never)
                        self.assertEqual(got.problem_kind, "sensitive_never")
                        self.assertEqual(apply_preflight._action(got, {})["type"], "manual", "no form offers to save it")

    def test_a_group_of_boxes_is_never_filled_from_the_library(self):
        options = ("I agree to the Terms", "I consent to the Privacy Notice", "I certify my answers are true")
        field = F("q", "Before submitting, tick every box that applies", MULTI, options=options, parent="Resume/CV")
        keyed = plan(BASE + [field]).get("q").answer_key
        rows = [answer(keyed, "\n".join(options), COMPANY), answer(keyed, "\n".join(options), OTHER, ["reusable"])]
        for company in (COMPANY, OTHER):
            got = plan(BASE + [field], sources(answers=rows), company=company).get("q")
            self.assertEqual((got.source.kind, got.value), ("none", None), company)
            self.assertIn("tick", got.net_never)
        skills = F("s", "Which languages have you used?", MULTI, options=("Python", "C++", "Rust"), parent="Resume/CV")
        got = plan(BASE + [skills], sources(answers=[answer("Which languages have you used?", "Python", COMPANY)])).get("s")
        self.assertEqual((got.value, got.problem_kind), (None, "sensitive_never"), "the accepted cost: an ordinary group of boxes is left for the student")

    def test_a_select_or_radio_whose_options_agree_to_something_is_never_filled_from_the_library(self):
        cases = (
            ("Do you accept the terms of this application?", ("I accept", "I do not accept")), ("Do you agree to our arbitration policy?", ("Yes", "No", "Not sure")),
            ("Do you agree to the Candidate Terms?", ("Y", "N")), ("Do you agree to arbitrate disputes?", ("Yes, I agree", "No, I do not agree")),
            ("Your understanding", ("I have read and understand", "I need more time")), ("Please choose one", ("I certify this is true", "I cannot certify")),
            ("Data use", ("I consent", "I do not consent")), ("Statement", ("I attest", "I decline")), ("Confirmation", ("I confirm", "I do not confirm")),
        )
        for label, options in cases:
            with self.subTest(label=label):
                field = F("q", label, SINGLE, options=options, parent="Resume/CV")
                keyed = plan(BASE + [field]).get("q").answer_key
                for company in (COMPANY, OTHER):
                    rows = [answer(keyed, options[0], COMPANY), answer(keyed, options[0], OTHER, ["reusable"])]
                    got = plan(BASE + [field], sources(answers=rows), company=company).get("q")
                    self.assertNotEqual((got.source.kind, got.value), ("answer", options[0]), company)
                    self.assertIsNone(got.value)
                    if got.sensitive is None:
                        self.assertIn("agreement", got.net_never)

    def test_a_typed_signature_is_never_filled_from_the_library(self):
        for label in ("Electronic signature (type your full legal name)", "Signature", "E-sign here"):
            with self.subTest(label=label):
                field = F("q", label, "input_text", parent="Resume/CV")
                got = plan(BASE + [field], sources(answers=[answer(label, "Sam Rivera", COMPANY)])).get("q")
                self.assertEqual((got.source.kind, got.value), ("none", None))
                self.assertIn("agreement", got.net_never)

    def test_a_select_whose_heading_agrees_but_whose_options_are_neutral_is_never_filled_from_the_library(self):
        cases = (
            ("Do you agree to our arbitration program?", ("Yes, please", "No, thanks")), ("Do you certify that your answers are true?", ("Yes I do", "No I do not")),
            ("Arbitration agreement", ("Opt in", "Opt out")), ("Please confirm you will abide by the code of conduct", ("Will do", "Will not")),
        )
        for label, options in cases:
            for kind in (SINGLE, MULTI):
                with self.subTest(label=label, kind=kind):
                    field = F("q", label, kind, options=options, parent="Resume/CV")
                    keyed = plan(BASE + [field]).get("q").answer_key
                    rows = [answer(keyed, options[0], COMPANY), answer(keyed, options[0], OTHER, ["reusable"]), answer(keyed, options[0], "", ["reusable"])]
                    for company in (COMPANY, OTHER):
                        got = plan(BASE + [field], sources(answers=rows), company=company).get("q")
                        self.assertEqual((got.source.kind, got.value), ("none", None), company)
                        self.assertIn("agreement", got.net_never)
                        self.assertEqual(got.problem_kind, "sensitive_never")
                        self.assertEqual(apply_preflight._action(got, {})["type"], "manual", "no form offers to save it")

    def test_an_agreement_word_in_a_selects_description_counts_like_one_in_its_heading(self):
        field = SchemaField(name="q", label="Talent pool", required=True, type=SINGLE, options=("Keep me in", "Take me out"),
                            description="<p>Please confirm you agree to keep your details on file.</p>", parent="Resume/CV")
        keyed = plan(BASE + [field]).get("q").answer_key
        got = plan(BASE + [field], sources(answers=[answer(keyed, "Keep me in", COMPANY)])).get("q")
        self.assertEqual((got.source.kind, got.value), ("none", None))
        self.assertIn("agreement", got.net_never)

    def test_typed_initials_are_a_signature_and_never_filled_from_the_library(self):
        for label in ("Type your initials to agree", "Initials (to show you accept the terms above)", "Your initials"):
            for kind in ("input_text", "textarea"):
                with self.subTest(label=label, kind=kind):
                    field = F("q", label, kind, parent="Resume/CV")
                    keyed = plan(BASE + [field]).get("q").answer_key
                    for row in (answer(keyed, "Sam Rivera", COMPANY), answer(label, "Sam Rivera", COMPANY), answer(keyed, "Sam Rivera", OTHER, ["reusable"])):
                        got = plan(BASE + [field], sources(answers=[row])).get("q")
                        self.assertEqual((got.source.kind, got.value), ("none", None))
                        self.assertIn("agreement", got.net_never)
                        self.assertEqual(apply_preflight._action(got, {})["type"], "manual")

    def test_a_select_with_one_option_works_as_a_tick_box_and_is_never_filled_from_the_library(self):
        for label, options in (("Work arrangement", ("Hybrid, three days on site",)), ("Interview format", ("Video call",)), ("Start", ("Noted",))):
            for kind in (SINGLE, MULTI):
                with self.subTest(label=label, kind=kind):
                    field = F("q", label, kind, options=options, parent="Resume/CV")
                    keyed = plan(BASE + [field]).get("q").answer_key
                    for company in (COMPANY, OTHER):
                        rows = [answer(keyed, options[0], COMPANY), answer(keyed, options[0], OTHER, ["reusable"]), answer(label, options[0], COMPANY)]
                        got = plan(BASE + [field], sources(answers=rows), company=company).get("q")
                        self.assertEqual((got.source.kind, got.value), ("none", None), company)
                        self.assertIn("tick", got.net_never)
                        self.assertEqual(apply_preflight._action(got, {})["type"], "manual", "no form offers to save it")

    def test_a_select_whose_options_or_heading_hit_the_agreement_topic_in_words_the_narrow_list_lacks_is_left_for_the_student(self):
        cases = (
            ("Code of conduct", ("I will comply", "I will not comply")), ("Handbook", ("I will abide by it", "I will not")),
            ("Waiver", ("I waive my right", "I keep my right")), ("Declaration", ("True", "Not true")),
            ("Please indicate your compliance with the code", ("Done", "Not yet")), ("Data sharing", ("I authorize this", "I do not")),
        )
        for label, options in cases:
            for kind in (SINGLE, MULTI):
                with self.subTest(label=label, kind=kind):
                    field = F("q", label, kind, options=options, parent="Resume/CV")
                    keyed = plan(BASE + [field]).get("q").answer_key
                    rows = [answer(keyed, options[0], COMPANY), answer(keyed, options[0], OTHER, ["reusable"])]
                    for company in (COMPANY, OTHER):
                        got = plan(BASE + [field], sources(answers=rows), company=company).get("q")
                        self.assertEqual((got.source.kind, got.value), ("none", None), company)
                        self.assertIn("agreement", got.net_never)
                        self.assertEqual(apply_preflight._action(got, {})["type"], "manual")

    def test_a_signature_line_worded_unusually_is_never_filled_from_the_library(self):
        for label, kind in (("Signed by", "input_text"), ("Sign below", "input_text"), ("Countersignature", "input_text"), ("Applicant declaration (full name)", "input_text"),
                            ("Acknowledged by (your name)", "input_text"), ("Name of signatory", "input_text"), ("Sign below to complete your application", "textarea")):
            with self.subTest(label=label):
                field = F("q", label, kind, parent="Resume/CV")
                keyed = plan(BASE + [field]).get("q").answer_key
                for row in (answer(keyed, "Sam Rivera", COMPANY), answer(label, "Sam Rivera", COMPANY), answer(keyed, "Sam Rivera", OTHER, ["reusable"])):
                    got = plan(BASE + [field], sources(answers=[row])).get("q")
                    self.assertEqual((got.source.kind, got.value), ("none", None))
                    self.assertTrue(got.sensitive is not None or "agreement" in got.net_never, "left for the student, by the classifier or the net")

    def test_ordinary_single_line_questions_and_longer_choice_lists_still_fill_at_their_own_company(self):
        for label, kind, options in (("Your favourite programming language", "input_text", ()), ("Preferred pronunciation of your name", "input_text", ()),
                                     ("Which team are you most interested in?", SINGLE, ("Perception", "Controls", "Planning")), ("Tell us about yourself", "textarea", ())):
            with self.subTest(label=label):
                field = F("q", label, kind, options=options, parent="Resume/CV")
                value = options[0] if options else "Answer"
                got = plan(BASE + [field], sources(answers=[answer(label, value, COMPANY)])).get("q")
                self.assertEqual((got.source.kind, got.value, got.net_never), ("answer", value, ()))

    def test_an_ordinary_select_still_fills_at_its_own_company(self):
        field = F("q", "Which team are you most interested in?", SINGLE, options=("Perception", "Controls"), parent="Resume/CV")
        got = plan(BASE + [field], sources(answers=[answer("Which team are you most interested in?", "Controls", COMPANY)])).get("q")
        self.assertEqual((got.source.kind, got.value, got.net_never), ("answer", "Controls", ()))

    def test_a_yes_no_question_that_agrees_or_is_yes_no_shaped_is_left_alone_in_any_words(self):
        for label in ("May we contact your current employer?", "Can we verify your employment history with past employers?", "Will you allow us to verify your degree?",
                      "Are you willing to sign an NDA?"):
            with self.subTest(label=label):
                field = yes_no(label, parent="Resume/CV")
                keyed = plan(BASE + [field]).get("q").answer_key
                for row in (answer(keyed, "Yes", OTHER, ["reusable"]), answer(keyed, "Yes", "", ["reusable"])):
                    self.assertEqual(plan(BASE + [field], sources(answers=[row])).get("q").value, None)

    def test_only_an_exact_stored_statement_ticks_a_box(self):
        field = box("Code of Ethics", "I will comply with the Code of Ethics", parent="Resume/CV")
        statement = apply_classify.statement_of(field, "checkbox", "acknowledgment", plan(BASE + [field]).get("q").answer_key)
        store = apply_helpers.Store(apply_helpers.entry("acknowledgment", statement, "checked", "checkbox", company_key=employer_key(COMPANY)))
        got = plan(BASE + [field], sources(allowed=["acknowledgment"], store=store)).get("q")
        self.assertEqual((got.sensitive, got.source.kind, got.value), ("acknowledgment", "sensitive", True))
        # A statement stored for another company, or one that is not word for word this one, ticks nothing.
        self.assertIsNone(plan(BASE + [field], sources(allowed=["acknowledgment"], store=store), company=OTHER).get("q").value)
        changed = box("Code of Ethics", "I will comply with the Code of Ethics at all times", parent="Resume/CV")
        self.assertIsNone(plan(BASE + [changed], sources(allowed=["acknowledgment"], store=store)).get("q").value)

    def test_a_box_the_classifier_does_not_read_as_an_agreement_is_left_for_the_student_even_with_a_stored_statement(self):
        # "adhere" is in no list, so the plan reads this box as an ordinary one, and no stored statement is looked up for it. The cost
        # is one more box the student ticks; nothing is ticked on a guess.
        field = box("Code of Ethics", "I will adhere to the Code of Ethics at all times", parent="Resume/CV")
        statement = apply_classify.statement_of(field, "checkbox", "acknowledgment", plan(BASE + [field]).get("q").answer_key)
        store = apply_helpers.Store(apply_helpers.entry("acknowledgment", statement, "checked", "checkbox", company_key=employer_key(COMPANY)))
        got = plan(BASE + [field], sources(allowed=["acknowledgment"], store=store)).get("q")
        self.assertEqual((got.sensitive, got.source.kind, got.value, got.problem_kind), (None, "none", None, "sensitive_never"))


class StoredStatementsAndTickBoxesArePerCompanyTests(StorePlanCase):
    """C. Every stored statement and every tick-box entry is kept for one company; only a select's exact option label, and an EEO decline, may be kept for any."""

    STATEMENTS = (
        ("acknowledgment", "I certify that the information I have provided is accurate"), ("acknowledgment", "I understand employment here is at will and may end at any time"),
        ("consent", "You may process my personal data for recruiting purposes for up to two years"), ("acknowledgment", "I agree to the arbitration rules"),
        ("consent", "I consent to Example Robotics storing my application data for 365 days"),
    )

    def test_no_statement_is_stored_or_offered_for_any_company(self):
        self.allow("acknowledgment", "consent")
        for category, statement in self.STATEMENTS:
            with self.subTest(statement=statement):
                self.refused("never for any company", category=category, question=statement, answer="checked")
                got = self.plan(apply_helpers.BASE + [box("Statement", statement)]).get("q")
                self.assertTrue(got.company_only)
                self.assertTrue(apply_preflight._sensitive_form(got, "sensitive_missing")["company_only"])
                saved = self.add(category=category, question=apply_classify.statement_of(box("Statement", statement), "checkbox", category), answer="checked", company=COMPANY)
                self.assertFalse(saved["any_company"])
        self.assertEqual(len(self.rows()), len(self.STATEMENTS))

    def test_an_eighteen_or_work_authorization_box_that_also_agrees_is_kept_for_one_company(self):
        self.allow("age_18", "work_authorization")
        for category, heading, option in (
            ("age_18", "Eligibility", "I am 18 years of age or older and agree to the arbitration rules"),
            ("work_authorization", "Eligibility", "I am authorized to work in the United States and I consent to a drug screen"),
            ("work_authorization", "Eligibility", "I am authorized to work in the United States and agree to complete an E-Verify check"),
            ("age_18", "Eligibility", "I am 18 or older and agree to receive text messages"),
            ("age_18", "Eligibility", "I am 18 or older and I understand that employment is at will"),
        ):
            with self.subTest(option=option):
                field = box(heading, option)
                statement = apply_classify.statement_of(field, "checkbox", category)
                self.refused("never for any company", category=category, question=statement, answer="checked", answer_kind="checkbox")
                got = self.plan(apply_helpers.BASE + [field]).get("q")
                self.assertTrue(got.company_only)
                self.assertTrue(apply_preflight._sensitive_form(got, "sensitive_missing")["company_only"])
                self.add(category=category, question=statement, answer="checked", answer_kind="checkbox", company=COMPANY)
                self.assertEqual(self.plan(apply_helpers.BASE + [field]).get("q").value, True)
                self.assertIsNone(self.plan(apply_helpers.BASE + [field], company=OTHER).get("q").value)

    def test_a_row_saved_for_any_company_by_another_route_is_used_at_no_company_for_a_tick_or_a_typed_answer(self):
        self.allow("work_authorization", "acknowledgment")
        for category, field in (("work_authorization", box("Work eligibility", "I am authorized to work in the United States")),
                               ("acknowledgment", box("Certification", apply_helpers.ACCURATE)),
                               ("work_authorization", F("q", "Are you authorized to work in the United States?", "input_text"))):
            with self.subTest(label=field.label):
                kind = "text" if field.type == "input_text" else "checkbox"
                text = "Yes" if kind == "text" else "checked"
                self.add(category=category, question=apply_classify.statement_of(field, control_of(field), category) if kind == "checkbox" else field.label,
                         answer=text, answer_kind=kind, company=COMPANY)
                with self.conn:
                    self.conn.execute("UPDATE apply_sensitive_answers SET company_key=''")
                for company in (COMPANY, OTHER):
                    got = self.plan(apply_helpers.BASE + [field], company=company).get("q")
                    self.assertNotEqual(got.source.kind, "sensitive", company)
                with self.conn:
                    self.conn.execute("DELETE FROM apply_sensitive_answers")

    def test_a_select_answer_matched_to_an_exact_option_label_may_still_be_kept_for_any_company(self):
        self.allow("work_authorization", "sponsorship", "age_18")
        for category, question, options, chosen in (
            ("work_authorization", "Are you legally authorized to work in the United States?", ("Yes", "No"), "Yes"),
            ("sponsorship", "Will you now or in the future require sponsorship for employment visa status?", ("Yes", "No"), "No"),
            ("age_18", "Are you at least 18 years of age?", ("Yes", "No"), "Yes"),
        ):
            with self.subTest(question=question):
                self.assertTrue(self.add(category=category, question=question, answer=chosen)["any_company"])
                field = F("q", question, SINGLE, options=options)
                for company in (COMPANY, OTHER):
                    got = self.plan(apply_helpers.BASE + [field], company=company).get("q")
                    self.assertEqual((got.value, got.source.kind, got.company_only), (chosen, "sensitive", False), company)

    def test_an_eeo_decline_is_still_kept_for_any_company(self):
        self.allow(*apply_sensitive.EEO_CATEGORIES)
        self.assertTrue(self.add(category="eeo_gender", question="Gender", answer="Decline To Self Identify")["any_company"])
        gender = F("gender", "Gender", SINGLE, required=False, options=("Male", "Female", "Decline To Self Identify"), section="compliance")
        for company in (COMPANY, OTHER):
            self.assertEqual(self.plan(apply_helpers.BASE + [gender], company=company).get("gender").value, "Decline To Self Identify", company)


control_of = apply_policy.control_of


class TickableEntriesNeverCarryANeverStorableClaimTests(StorePlanCase):
    """A work-authorization, sponsorship or 18+ entry never carries a demographic, criminal, pay or clearance claim, in its wording or in its answer."""

    BOXES = (
        ("work_authorization", "I am authorized to work in the United States and I am a person of color"),
        ("work_authorization", "I am authorized to work in the United States and I identify as Black"),
        ("sponsorship", "I will not require sponsorship and I was born in 1999"),
        ("age_18", "I am at least 18 years of age and I am over 40"),
        ("age_18", "I am at least 18 years of age and I have a medical condition"),
        ("work_authorization", "I am legally authorized to work in the United States and am not currently on probation or parole"),
        ("work_authorization", "I am authorized to work in the United States and my expected income is 90000"),
        ("age_18", "I am at least 18 years of age and I am on a sanctions list"),
    )

    def test_the_plan_leaves_it_for_the_student_and_the_store_never_holds_it(self):
        self.allow("work_authorization", "sponsorship", "age_18")
        for category, option in self.BOXES:
            with self.subTest(option=option):
                field = box("Eligibility", option)
                self.assertEqual(classify_item(field, "checkbox"), "uncategorized")
                got = self.plan(apply_helpers.BASE + [field]).get("q")
                self.assertEqual((got.sensitive, got.problem_kind, got.source.kind), ("uncategorized", "sensitive_never", "none"))
                statement = apply_classify.statement_of(field, "checkbox", category)
                for company in ("", COMPANY):
                    for from_form in (False, True):
                        with self.assertRaises(StoreRefused, msg=option):
                            self.add(category=category, question=statement, answer="checked", answer_kind="checkbox", company=company, from_form=from_form)
        self.assertEqual(self.rows(), [])

    def test_a_select_option_that_claims_a_demographic_is_refused_in_the_plan_and_in_the_store(self):
        self.allow("work_authorization")
        question = "Are you authorized to work in the United States?"
        field = F("q", question, SINGLE, options=("Yes, and I am a protected veteran", "No"))
        self.assertEqual(classify_item(field, "select"), "uncategorized")
        self.assertEqual(self.plan(apply_helpers.BASE + [field]).get("q").problem_kind, "sensitive_never")
        for company in ("", COMPANY):
            with self.assertRaises(StoreRefused):
                self.add(category="work_authorization", question=question, answer="Yes, and I am a protected veteran", answer_kind="option", company=company)
        self.assertEqual(self.rows(), [])
        self.assertTrue(self.add(category="work_authorization", question=question, answer="Yes")["any_company"])

    def test_an_acknowledgment_that_names_the_eeoc_poster_is_still_stored_for_one_company_and_ticked(self):
        self.allow("acknowledgment", "consent")
        for category, heading, statement in (
            ("acknowledgment", "Know Your Rights", "I acknowledge that I have reviewed the EEOC Know Your Rights poster"),
            ("consent", "Self-identification data", "I consent to Example Robotics processing my data, including any self-identification answers"),
        ):
            with self.subTest(statement=statement):
                field = box(heading, statement)
                got = self.plan(apply_helpers.BASE + [field]).get("q")
                self.assertEqual((got.sensitive, got.problem_kind), (category, "sensitive_missing"))
                self.add(category=category, question=got.statement, answer="checked", company=COMPANY, from_form=True)
                self.assertEqual(self.plan(apply_helpers.BASE + [field]).get("q").value, True)
                self.assertIsNone(self.plan(apply_helpers.BASE + [field], company=OTHER).get("q").value)


class BroadNetVocabularyTests(unittest.TestCase):
    """D. The net's best-effort refusal covers the wordings review round 2 found the lists missed; what it still misses is at worst one company's own answer."""

    WORDINGS = {
        "criminal": (
            "Have you ever pleaded guilty or no contest to a crime?", "Have you ever been found guilty of an offense?", "Do you have any outstanding warrants?",
            "Have you ever been charged with a DUI or DWI?", "Have you ever spent time in jail or prison?", "Have you ever received a police caution?",
            "Are there any legal proceedings pending against you?", "Have you received deferred adjudication?", "Have you ever been indicted?",
            "Did you plead nolo contendere?", "Have you ever been detained by law enforcement?", "Has any record been expunged or sealed?",
            "Do you have any unresolved legal matters?", "Are you subject to a restraining order?", "Has your license ever been suspended or revoked?",
            "Are you party to any pending litigation?", "Have you ever been cautioned or reprimanded by law enforcement?", "Are you a registered offender?",
        ),
        "demographic": (
            "Do you identify as Black, Indigenous, or a person of color?", "Are you Native American or Alaska Native?", "Are you from an underrepresented group?",
            "Are you a member of a minority group?", "Are you over 40?", "Do you have any medical conditions?", "Do you have a health condition that needs an accommodation?",
            "Are you in the National Guard or Reserves?", "Do you identify as neurodivergent?", "What year were you born?", "What is your national origin?",
            "Do you have a learning difference such as ADHD or dyslexia?", "Are you a parent or primary caregiver?", "Do you have any children?", "What is your caste?",
            "Do you identify as Aboriginal or Torres Strait Islander?", "Are you Deaf or hard of hearing?", "What is your first language?",
        ),
        "money": (
            "What is your desired annual income?", "How much do you currently make?", "What were you making at your last internship?", "What is your expected monthly income?",
            "What is your expected CTC?", "What is your target OTE?", "What is your price per hour?",
            "What is your current total comp?", "What is your desired comp range?", "What was your last drawn fixed component?", "What's your ask?",
            "Have you ever filed for bankruptcy?", "What is your credit score?",
        ),
        "security": (
            "Do you hold a TS/SCI clearance with polygraph?", "Do you hold a DoD Secret clearance?", "Do you hold a Public Trust?",
            "Will you consent to a government background investigation?", "Are you on any OFAC or sanctions list?",
            "Do you hold UK SC or DV vetting?", "Do you have CI poly?", "Do you have NATO Secret access?",
        ),
    }
    IMMIGRATION = (
        "Do you hold an EAD?", "Are you a foreign national?", "Are you lawfully present in the United States?", "Are you on an H-4 visa?", "Do you have DACA?", "Are you on TPS?",
        "Are you an asylee or refugee?", "Do you have the right to live and work in the UK?", "Do you have leave to remain?", "Do you have settled status?",
        "Do you have Australian working rights?", "Do you hold an EU Blue Card?", "Do you hold an Employment Pass?", "What is your status in the United States?",
    )

    def test_a_never_storable_wording_is_left_for_the_student_at_every_company(self):
        for topic, labels in self.WORDINGS.items():
            for label in labels:
                with self.subTest(topic=topic, label=label):
                    self.assertTrue(never_storable(label), label)
                    for kind, options in (("input_text", ()), (SINGLE, YES_NO)):
                        field = F("q", label, kind, options=options, parent="Resume/CV")
                        rows = [answer(label, "Yes" if options else "Answer", OTHER, ["reusable"]), answer(label, "Yes" if options else "Answer", COMPANY)]
                        for company in (COMPANY, OTHER):
                            got = plan(BASE + [field], sources(answers=rows), company=company).get("q")
                            self.assertNotEqual(got.source.kind, "answer", f"{company}: {label}")
                            if got.sensitive is None:
                                self.assertIn(topic, got.net_never)
                                self.assertEqual(apply_preflight._action(got, {})["type"], "manual")

    def test_an_immigration_wording_is_company_only_and_never_from_another_company(self):
        for label in self.IMMIGRATION:
            with self.subTest(label=label):
                self.assertTrue({"immigration", "work_authorization"} & set(net_topics(label)), label)
                field = F("q", label, "input_text", parent="Resume/CV")
                for row in (answer(label, "Yes", OTHER, ["reusable"]), answer(label, "Yes", "", ["reusable"])):
                    got = plan(BASE + [field], sources(answers=[row])).get("q")
                    self.assertNotEqual(got.source.kind, "answer")
                self.assertTrue(plan(BASE + [field]).get("q").net_company or plan(BASE + [field]).get("q").sensitive is not None)

    def test_a_pay_range_a_clearance_level_and_race_and_pronoun_lists_are_read_on_their_options(self):
        cases = (
            ("Which range best fits your expectations?", ("$20-25/hr", "$25-30/hr", "$30+/hr"), "money"),
            ("Please select your current level of access", ("None", "Confidential", "Secret", "Top Secret"), "security"),
            ("Which of these communities do you belong to?", ("Black", "Asian", "Indigenous", "White"), "demographic"),
            ("Which of the following best describes you?", ("Black or African American", "Asian", "White", "Two or more races"), "demographic"),
            ("How should we refer to you?", ("He/him", "She/her", "They/them"), "demographic"),
            ("How do you describe yourself?", ("Asian", "White", "Other"), "demographic"),
            ("Please pick one", ("$40,000-$50,000", "$50,000-$60,000"), "money"),
        )
        for label, options, topic in cases:
            with self.subTest(label=label):
                field = F("q", label, SINGLE, options=options, parent="Resume/CV")
                rows = [answer(label, options[1], OTHER, ["reusable"]), answer(label, options[1], COMPANY)]
                for company in (COMPANY, OTHER):
                    got = plan(BASE + [field], sources(answers=rows), company=company).get("q")
                    self.assertEqual((got.source.kind, got.value), ("none", None), company)
                    self.assertIn(topic, got.net_never)

    def test_common_prompts_that_share_a_word_with_the_wider_lists_are_not_caught(self):
        for label in ("What is your comp sci background?", "Describe your Docker registry experience", "Why do you want to work on computer vision?",
                      "Do you have a valid driver's license?", "Describe your comp bio coursework"):
            with self.subTest(label=label):
                self.assertEqual(net_topics(label), ())

    def test_a_plain_choice_list_is_not_read_as_pay_or_clearance(self):
        for label, options in (("Which team are you most interested in?", ("Platform", "Security", "Data")), ("Which kind of role do you want?", ("Paid", "Unpaid")),
                               ("What is your preferred start term?", ("Summer", "Fall"))):
            field = F("q", label, SINGLE, options=options, parent="Resume/CV")
            got = plan(BASE + [field], sources(answers=[answer(label, options[0], COMPANY)])).get("q")
            self.assertEqual(got.net_never, (), label)

    def test_a_description_is_read_for_the_question_it_may_hide(self):
        hidden = SchemaField(name="q", label="Anything else we should know about you?", required=True, type="textarea", parent="Resume/CV",
                             description="<p>Please list any criminal convictions or pending charges here.</p>")
        got = plan(BASE + [hidden], sources(answers=[answer(hidden.label, "None", COMPANY), answer(hidden.label, "None", OTHER, ["reusable"])])).get("q")
        self.assertEqual((got.source.kind, got.problem_kind), ("none", "sensitive_never"))
        self.assertIn("criminal", got.net_never)
        pay = SchemaField(name="q", label="Your expectations", required=True, type="input_text", parent="Resume/CV", description="<p>Please state your expected hourly pay in USD.</p>")
        got = plan(BASE + [pay], sources(answers=[answer(pay.label, "$30", COMPANY)])).get("q")
        self.assertEqual((got.source.kind, got.value), ("none", None))
        self.assertIn("money", got.net_never)
        # Help text that merely says "pay attention" or carries agreement boilerplate is not a question about pay or an agreement.
        plain = SchemaField(name="q", label="Tell us about a project", required=True, type="textarea", parent="Resume/CV",
                            description="<p>Pay attention to detail. See our privacy notice for how we use this.</p>")
        got = plan(BASE + [plain], sources(answers=[answer(plain.label, "A robot", COMPANY)])).get("q")
        self.assertEqual((got.net_never, got.source.kind, got.value), ((), "answer", "A robot"))


class FollowUpInheritanceTests(unittest.TestCase):
    """A question after a parent the net finds a topic in takes the parent's topics, whatever its own words say."""

    def form(self, parent_label, child_label, kind="textarea", grandchild=None):
        fields = [yes_no(parent_label, name="p", parent="Resume/CV"), F("q", child_label, kind, parent=parent_label)]
        if grandchild:
            fields.append(F("g", grandchild, "textarea", parent=child_label))
        return fields

    def test_a_non_follow_up_under_a_net_only_criminal_parent_is_left_for_the_student(self):
        for parent, children in (
            ("Are you currently on probation or parole?", ("Please tell us what happened", "How much longer is it expected to last?", "What conditions were imposed on you?")),
            ("Do you have any pending cases?", ("What was the final disposition of the matter?", "Which court is handling it?")),
        ):
            self.assertIsNone(apply_classify.classify_sensitive(parent), "the precise classifier does not know the parent")
            for child in children:
                with self.subTest(parent=parent, child=child):
                    fields = self.form(parent, child)
                    keyed = plan(BASE + fields).get("q").answer_key
                    rows = [answer(keyed, "Details", COMPANY), answer(keyed, "Details", OTHER, ["reusable"]), answer(child, "Details", COMPANY)]
                    for company in (COMPANY, OTHER):
                        got = plan(BASE + fields, sources(answers=rows), company=company).get("q")
                        self.assertNotEqual(got.source.kind, "answer", company)
                        self.assertTrue(got.sensitive is not None or "criminal" in got.net_never, child)
                        self.assertEqual(apply_preflight._action(got, {})["type"], "manual")

    def test_a_follow_up_under_a_net_only_work_authorization_parent_is_company_only(self):
        parent = "Are you permitted to work in the United States?"
        fields = self.form(parent, "What is the expiration date of your current status?", "input_text")
        got = plan(BASE + fields).get("q")
        self.assertTrue(got.sensitive is not None or "work_authorization" in got.net, got.net)

    def test_a_precisely_sensitive_parent_whose_words_miss_the_net_still_passes_its_topic_to_a_non_follow_up_child(self):
        # "Are you a TN holder?" is a sponsorship question the net's wording lists do not read; the category carries the topic.
        parent = "Are you a TN holder?"
        self.assertEqual(net_topics(parent), ())
        fields = self.form(parent, "What is the expiration date of your current status?", "input_text")
        got = plan(BASE + fields).get("q")
        self.assertEqual((got.net, got.net_company), (("immigration",), True), "the category's topic is passed on, so this is company-only")
        for row in (answer(got.answer_key, "2027", OTHER, ["reusable"]), answer(got.answer_key, "2027", "", ["reusable"])):
            self.assertNotEqual(plan(BASE + fields, sources(answers=[row])).get("q").source.kind, "answer")

    def test_a_three_level_chain_under_a_net_only_criminal_parent_keeps_the_topic_to_the_end(self):
        fields = self.form("Are you currently on probation or parole?", "If yes, please explain", grandchild="Please provide more details")
        keyed = plan(BASE + fields).get("g").answer_key
        got = plan(BASE + fields, sources(answers=[answer(keyed, "Details", COMPANY)])).get("g")
        self.assertIn("criminal", got.net_never)
        self.assertEqual((got.source.kind, got.problem_kind), ("none", "sensitive_never"))

    def test_a_never_storable_chain_runs_through_a_child_the_wording_alone_does_not_call_a_follow_up(self):
        """The shared chain vectors: the engine marks the same fields never storable (reusable_and_sensitive.mjs)."""
        chains = json.loads((FIXTURES / "net_chains.json").read_text(encoding="utf-8"))["chains"]
        for chain in chains:
            fields = [yes_no(chain["parent"], name="p", parent="Resume/CV")]
            above = chain["parent"]
            for index, label in enumerate(chain["children"]):
                fields.append(F(f"c{index}", label, "textarea", parent=above))
                above = label
            got = plan(BASE + fields)
            for index, label in enumerate(chain["children"]):
                with self.subTest(parent=chain["parent"], child=label):
                    entry = got.get(f"c{index}")
                    never = bool(entry.net_never) or entry.sensitive is not None
                    self.assertEqual(never, chain["never"][index])
                    if never:
                        rows = [answer(entry.answer_key, "Details", COMPANY), answer(label, "Details", COMPANY)]
                        filled = plan(BASE + fields, sources(answers=rows)).get(f"c{index}")
                        self.assertNotEqual(filled.source.kind, "answer")
                        self.assertEqual(apply_preflight._action(filled, {})["type"], "manual")

    def test_a_question_after_an_ordinary_one_takes_nothing(self):
        fields = [yes_no("Are you willing to relocate?", name="p", parent="Resume/CV"), F("q", "Please tell us more about your plans", "textarea", parent="Are you willing to relocate?")]
        self.assertEqual(plan(BASE + fields).get("q").net, ())


class AChoiceThatAlsoAgreesIsKeptForOneCompanyTests(StorePlanCase):
    """C. A work authorization, sponsorship or 18-or-older choice whose question or option also agrees to something is an agreement: one company's."""

    CASES = (
        ("age_18", "Are you at least 18 years old, and do you agree to the arbitration rules?", ("Yes", "No"), "Yes"),
        ("work_authorization", "Are you authorized to work in the United States?", ("Yes, and I agree to complete an E-Verify check", "No"), "Yes, and I agree to complete an E-Verify check"),
        ("sponsorship", "Will you require sponsorship? Please confirm your answer is accurate", ("Yes", "No"), "No"),
    )

    def test_the_store_refuses_any_company_and_the_plan_offers_no_tick_for_it(self):
        self.allow("age_18", "work_authorization", "sponsorship")
        for category, question, options, chosen in self.CASES:
            with self.subTest(question=question):
                self.refused("never for any company", category=category, question=question, answer=chosen)
                got = self.plan(apply_helpers.BASE + [F("q", question, SINGLE, options=options)]).get("q")
                self.assertTrue(got.company_only)
                self.assertTrue(self.add(category=category, question=question, answer=chosen, company=COMPANY)["company"])

    def test_a_row_saved_for_any_company_by_another_route_is_used_at_no_company(self):
        self.allow("age_18", "work_authorization", "sponsorship")
        for category, question, options, chosen in self.CASES:
            with self.subTest(question=question):
                self.add(category=category, question=question, answer=chosen, company=COMPANY)
                with self.conn:
                    self.conn.execute("UPDATE apply_sensitive_answers SET company_key=''")
                field = F("q", question, SINGLE, options=options)
                for company in (COMPANY, OTHER):
                    self.assertNotEqual(self.plan(apply_helpers.BASE + [field], company=company).get("q").source.kind, "sensitive", company)
                with self.conn:
                    self.conn.execute("DELETE FROM apply_sensitive_answers")


class CommonPromptsAreNotOverBlockedTests(unittest.TestCase):
    """D. Common CS and essay prompts are not read as criminal, security or pay questions, so they get a save form and fill at their own company."""

    PROMPTS = (
        "Tell us about a time you took charge of a project", "Summarize yourself in a sentence", "In 2-3 sentences, describe a project you are proud of",
        "Describe your experience with network security", "What security tools have you used?", "Describe your experience exporting data from SQL",
        "What is your hourly availability during the semester?",
    )
    STILL_BLOCKED = (
        "Were you charged with an offense?", "What sentence were you given after you were charged?", "Do you hold a security clearance for network security work?",
        "Have you exported controlled data under ITAR?", "What is your desired hourly rate?",
    )

    def test_an_ordinary_prompt_reads_no_topic_gets_a_save_form_and_fills_only_at_its_own_company(self):
        for label in self.PROMPTS:
            with self.subTest(label=label):
                self.assertEqual(net_topics(label), ())
                field = F("q", label, "textarea")
                got = plan(BASE + [field]).get("q")
                self.assertEqual((got.net, got.net_never), ((), ()))
                self.assertEqual(apply_preflight._action(got, {})["type"], "answer")
                rows = [answer(got.answer_key, "My answer", COMPANY)]
                filled = plan(BASE + [field], sources(answers=rows)).get("q")
                self.assertEqual((filled.source.kind, filled.value), ("answer", "My answer"))
                self.assertNotEqual(plan(BASE + [field], sources(answers=rows), company=OTHER).get("q").source.kind, "answer")

    def test_a_real_criminal_security_or_pay_question_is_still_never_storable(self):
        for label in self.STILL_BLOCKED:
            with self.subTest(label=label):
                self.assertTrue(never_storable(label))


if __name__ == "__main__":
    unittest.main()
