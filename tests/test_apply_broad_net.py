"""Apply for me's broad net (apply_policy.NET_TOPICS, docs/phase5-apply-agent-spec.md 7.3 "As built").

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

from opportunity_app import apply_policy, apply_preflight, apply_sensitive
from opportunity_app.apply_checks import question_key
from opportunity_app.apply_policy import SchemaField, classify_item, net_topics, never_storable, possibly_sensitive
from opportunity_app.apply_sensitive import StoreRefused

import test_apply_policy as policy_tests
import test_apply_sensitive as sensitive_tests
from test_apply_policy import BASE, COMPANY, F, MULTI, OTHER, SINGLE, USER, answer, kinds, plan, sources

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "apply"
FELONY = "Have you ever been convicted of a felony?"
VISA = "Do you currently hold a visa?"
YES_NO = ("Yes", "No")


def setUpModule():
    policy_tests.setUpModule()


def tearDownModule():
    policy_tests.tearDownModule()


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
        self.assertEqual(seen, set(apply_policy.NET_TOPICS) | {"adult"})
        self.assertTrue(set(apply_policy.NEVER_STORABLE_TOPICS) <= set(apply_policy.NET_TOPICS))
        self.assertEqual(set(apply_policy.NEVER_STORABLE_TOPICS), {"criminal", "demographic", "money", "security"})

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


class OrdinaryQuestionsStayReusableTests(unittest.TestCase):
    ORDINARY = ("Tell us about yourself", "Describe a time you worked on a team", "Are you willing to relocate?",
                "What is your expected graduation date?", "What programming languages do you know?")

    def test_a_reusable_answer_saved_at_another_company_still_fills_an_ordinary_question(self):
        for index, label in enumerate(self.ORDINARY):
            with self.subTest(label=label):
                field = F("q", label, "textarea", parent="Resume/CV")
                got = plan(BASE + [field], sources(answers=[answer(label, "Answer", OTHER, ["reusable"])])).get("q")
                self.assertEqual((got.net, got.net_never, got.net_company, got.context_dependent), ((), (), False, False), label)
                self.assertEqual((got.source.kind, got.value), ("answer", "Answer"), label)

    def test_this_role_is_already_company_bound_by_the_older_wording_rule_and_the_net_adds_nothing(self):
        field = F("q", "Why are you interested in this role?", "textarea", parent="Resume/CV")
        got = plan(BASE + [field]).get("q")
        self.assertEqual((got.net, got.net_company, got.context_dependent), ((), False, True))

    def test_profile_links_come_from_the_profile_whatever_stands_above_them(self):
        field = F("q", "LinkedIn profile", "input_text", parent=FELONY)
        facts = copy.deepcopy(policy_tests.FACTS)
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

    def test_the_view_hides_the_tick_for_any_company(self):
        for label in self.WORDINGS:
            with self.subTest(label=label):
                got = plan(BASE + [F("q", label, "input_text", parent="Resume/CV")]).get("q")
                action = apply_preflight._action(got, {})
                self.assertEqual((action["type"], action["reusable_allowed"]), ("answer", False))


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
                    self.assertEqual(apply_preflight._action(got, {})["reusable_allowed"], False)
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
                if apply_policy.classify_sensitive(label):
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

    def test_a_marketing_box_or_a_plain_preference_stays_ordinary(self):
        marketing = box("Updates", "Keep me informed about future openings at Example Robotics", parent="Resume/CV")
        got = plan(BASE + [marketing], sources(answers=[answer(plan(BASE + [marketing]).get("q").answer_key, "Yes", COMPANY)])).get("q")
        self.assertEqual((got.net_never, got.source.kind, got.value), ((), "answer", True))
        relocate = yes_no("Are you willing to relocate?", parent="Resume/CV")
        keyed = plan(BASE + [relocate]).get("q").answer_key
        got = plan(BASE + [relocate], sources(answers=[answer(keyed, "Yes", OTHER, ["reusable"])])).get("q")
        self.assertEqual((got.source.kind, got.value), ("answer", "Yes"))

    def test_only_an_exact_stored_statement_ticks_it(self):
        field = box("Code of Ethics", "I will comply with the Code of Ethics", parent="Resume/CV")
        statement = apply_policy.statement_of(field, "checkbox", "acknowledgment", plan(BASE + [field]).get("q").answer_key)
        store = policy_tests.Store(policy_tests.entry("acknowledgment", statement, "checked", "checkbox", company_key=apply_policy.apply_sensitive.company_key(COMPANY)))
        got = plan(BASE + [field], sources(allowed=["acknowledgment"], store=store)).get("q")
        self.assertEqual((got.sensitive, got.source.kind, got.value), ("acknowledgment", "sensitive", True))


class StorePlanCase(sensitive_tests.StoreCase):
    """A real store, and a plan built against it (the same helpers PlanFromTheStoreTests uses)."""

    def sources(self):
        src = apply_policy.sources_for(self.conn, USER, "job-a", company=COMPANY, key=policy_tests.KEY)
        src.facts, src.resume, src.cover_letter = copy.deepcopy(policy_tests.FACTS), dict(policy_tests.RESUME_OK), dict(policy_tests.LETTER_NONE)
        return src

    def plan(self, fields, mode="submit", company=COMPANY):
        return policy_tests.plan(fields, self.sources(), mode, company=company)


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
                got = self.plan(sensitive_tests.BASE + [field]).get("q")
                self.assertTrue(got.company_only, "no Use for any company")
                self.assertTrue(apply_preflight._sensitive_form(got, "sensitive_missing")["company_only"])
        self.assertEqual(self.rows(), [])

    def test_a_row_saved_for_any_company_by_another_route_is_ignored_at_every_company(self):
        self.allow("acknowledgment")
        field = box("Arbitration", "I agree to the arbitration rules")
        statement = apply_policy.statement_of(field, "checkbox")
        self.add(category="acknowledgment", question=statement, answer="checked", company=COMPANY)
        with self.conn:
            self.conn.execute("UPDATE apply_sensitive_answers SET company_key=''")
        for company in (COMPANY, OTHER):
            got = self.plan(sensitive_tests.BASE + [field], company=company).get("q")
            self.assertEqual((got.problem_kind, got.source.kind, got.value), ("sensitive_missing", "none", None), company)

    def test_a_plain_certification_that_the_students_answers_are_true_may_still_be_kept_for_any_company(self):
        self.allow("acknowledgment")
        for statement in (sensitive_tests.ACCURATE, "I certify that all of this is true", f"Certification {sensitive_tests.ACCURATE}"):
            with self.subTest(statement=statement):
                self.assertFalse(apply_sensitive.cites_document(statement), statement)
        self.assertTrue(self.add(category="acknowledgment", question=sensitive_tests.ACCURATE, answer="checked")["any_company"])
        # Any other word makes it something the app cannot prove is not a document.
        self.assertTrue(apply_sensitive.cites_document(sensitive_tests.ACCURATE + " and I will follow the handbook"))
        self.assertTrue(apply_sensitive.cites_document(sensitive_tests.ACCURATE + " and I agree to the arbitration rules"))
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
                got = self.plan(sensitive_tests.BASE + [field]).get("q")
                self.assertEqual((got.sensitive, got.problem_kind, got.source.kind), ("uncategorized", "sensitive_never", "none"))
                self.assertEqual(apply_preflight._action(got, {})["type"], "manual", "no form")
                statement = apply_policy.statement_of(field, "checkbox", category)
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
                (USER, apply_policy.statement_of(field, "checkbox", "work_authorization"), question_key(apply_policy.statement_of(field, "checkbox", "work_authorization")),
                 __import__("hashlib").sha256(question_key(apply_policy.statement_of(field, "checkbox", "work_authorization")).encode()).hexdigest()),
            )
        got = self.plan(sensitive_tests.BASE + [field], company=OTHER).get("q")
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
                self.add(category=category, question=apply_policy.statement_of(field, "checkbox", category), answer="checked", answer_kind="checkbox")
        self.assertEqual(len(self.rows()), 4)


class PreflightTests(policy_tests.PolicyCase):
    """What the what's-missing view offers, and what answer_missing refuses, for a question the net leaves to the student or keeps for one company."""

    def listing(self, *questions):
        listing = copy.deepcopy(policy_tests.SIMPLE)
        for index, (label, kind, values) in enumerate(questions, start=20):
            listing["questions"].append({"label": label, "required": True, "fields": [
                {"name": f"question_{index}", "type": kind, "values": [{"label": value, "value": number} for number, value in enumerate(values)]}]})
        self.client = policy_tests.StaticClient(listing)
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
        self.assertEqual((problem["kind"], problem["action"]["type"], problem["action"]["reusable_allowed"]), ("missing_answer", "answer", False))
        with self.assertRaisesRegex(apply_preflight.AnswerRefused, "personal or legal"):
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
        from opportunity_app import preparation
        preparation.save_answer(self.conn, "Do you have unrestricted work rights in the United States?", "Yes", policy_tests.runs_tests.BLUEFIN, ["reusable"], user_id=USER)
        other = self.role("gh-2", company=OTHER, job="4000000002")
        problems = {item["key"]: item for item in self.run_check(other)["problems"]}
        self.assertEqual(problems[key]["kind"], "missing_answer")

    def test_an_ordinary_question_still_offers_the_tick(self):
        (key,) = self.listing(("Tell us about yourself", "textarea", ()))
        self.assertTrue(self.problem(key)["action"]["reusable_allowed"])


if __name__ == "__main__":
    unittest.main()
