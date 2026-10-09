"""The file name Lever's own page posts for a file (docs/phase5-lever-handoff-spec.md 3.8): the rule, and every place the app uses it.

Lever's /js/parseResume.js posts the file under ``sanitizeFilename(file.name)`` (read 2026-10-08). The request rules accept the student's own file
only under the name that rule gives, so a rule that drifts refuses a common file name or lets a script's name through. No network; names are fictional.
"""

import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent))

import realdata_guard

realdata_guard.install()

from opportunity_app.apply import agent as apply_agent, lever
from opportunity_app.apply.ats import REGISTRY
from opportunity_app.apply.checks import LEVER_ROUTE_POLICY, PHASE_FILL, PHASE_STUDENT, Allow, resume_post_decision
from opportunity_app.apply.lever_adapter import LeverAdapter

from helpers_apply import setUpModule, tearDownModule  # noqa: F401
from test_apply_lever_policy import VALUES, resume_body, resume_request, state

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "apply" / "lever" / "parseResume.js"

# (the name the browser has, the name Lever's page posts)
CASES = [
    ("Sam Rivera Resume.pdf", "Sam_Rivera_Resume.pdf"),
    ("Resume (1).pdf", "Resume_(1).pdf"),
    ("Résumé.pdf", "Résumé.pdf"),
    ("Résumé (1).pdf", "Résumé_(1).pdf"),
    ("Sam's resume.pdf", "Sam's_resume.pdf"),
    ("My Résumé (final).pdf", "My_Résumé_(final).pdf"),
    ("cv, 2026 & more+#1.pdf", "cv,_2026_&_more+#1.pdf"),
    ("履歴書.pdf", "履歴書.pdf"),
    ('a<b>c:d"e/f\\g|h?i*j.pdf', "a_b_c_d_e_f_g_h_i_j.pdf"),
    ("a   b\t\nc.pdf", "a_b_c.pdf"),
    ("a b　c﻿d.pdf", "a_b_c_d.pdf"),
    ("..hidden.pdf..", "hidden.pdf"),
    ("__draft__.pdf__", "draft_.pdf"),
    ("a__b___c.pdf", "a_b_c.pdf"),
    (" resume.pdf ", "resume.pdf"),
    ("...", "untitled"),
    ("   ", "untitled"),
    ("", "untitled"),
]


class RuleTests(unittest.TestCase):
    def test_the_rule_is_the_one_in_levers_page(self):
        for browser, posted in CASES:
            with self.subTest(name=browser):
                self.assertEqual(lever.posted_file_name(browser), posted)

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_the_rule_gives_what_the_stand_in_page_posts_for_the_same_name(self):
        """The stand-in's ``safeName`` is written to Lever's script; this holds the two to one answer, name for name."""
        source = FIXTURE.read_text(encoding="utf-8")
        start = source.index("function safeName")
        end = source.index("\n  }\n", start) + 4
        script = source[start:end] + "\nconst names = JSON.parse(require('fs').readFileSync(0, 'utf8'));\nprocess.stdout.write(JSON.stringify(names.map(safeName)));\n"
        names = [browser for browser, _ in CASES]
        done = subprocess.run(["node", "-e", script], input=json.dumps(names).encode("utf-8"), capture_output=True, timeout=60, check=True)
        self.assertEqual(json.loads(done.stdout.decode("utf-8")), [lever.posted_file_name(name) for name in names])

    def test_levers_policy_carries_the_rule_and_no_other_ats_has_one(self):
        self.assertIs(LEVER_ROUTE_POLICY.resume_post_name, lever.posted_file_name)
        self.assertEqual([spec.key for spec in REGISTRY if spec.route_policy.resume_post_name], [lever.ATS_LEVER])


class ListenerBindingTests(unittest.TestCase):
    """The listener reports the browser's own name, percent-encoded; the app applies the page's rule to it."""

    def agent(self):
        agent = apply_agent.ApplyAgent(mode="handoff", adapter=LeverAdapter())
        agent._phase = apply_agent.PHASE_STUDENT
        return agent

    def test_a_name_with_accents_parentheses_and_apostrophes_opens_the_choice_under_the_name_the_page_posts(self):
        for browser, posted in CASES:
            if not browser:
                continue
            with self.subTest(name=browser):
                agent = self.agent()
                agent._on_binding({"name": apply_agent.PRESS_BINDING, "payload": "file:1:" + quote(browser, safe="!*'()~")})
                self.assertEqual((agent._state.student_files_chosen, agent._state.student_file_name), (1, posted))

    def test_the_listener_encodes_the_name_and_rewrites_nothing_itself(self):
        source = apply_agent.press_listener(["jobs.lever.co"], "button", ["jobs.lever.co"], 'input[name="resume"]', 100)
        self.assertIn("encodeURIComponent(String(file.name", source)
        self.assertNotIn("A-Za-z0-9._-", source)
        self.assertIn(f"named.length > {apply_agent.FILE_NAME_REPORT_CAP}", source)

    def test_a_name_that_does_not_decode_is_ignored(self):
        agent = self.agent()
        agent._on_binding({"name": apply_agent.PRESS_BINDING, "payload": "file:1:a%FF.pdf"})
        self.assertEqual((agent._state.student_files_chosen, agent._open_choices), (0, []))

    def test_a_policy_with_no_rule_keeps_the_name_as_it_is(self):
        agent = self.agent()
        with mock.patch.object(agent, "_policy", mock.Mock(resume_post_path="/x", resume_post_name=None)):
            agent._on_binding({"name": apply_agent.PRESS_BINDING, "payload": "file:1:My%20cv.pdf"})
        self.assertEqual(agent._state.student_file_name, "My cv.pdf")


class RequestRuleTests(unittest.TestCase):
    def decide(self, phase, filename, **fields):
        return resume_post_decision(phase, resume_request(body=resume_body(filename=filename)), state(**fields), LEVER_ROUTE_POLICY)

    def test_the_students_own_file_passes_under_the_name_lever_posts_for_it(self):
        for browser, posted in CASES:
            if browser:
                with self.subTest(name=browser):
                    decision = self.decide(PHASE_STUDENT, posted, student_file_name=posted)
                    self.assertIsInstance(decision, Allow, getattr(decision, "reason", ""))

    def test_the_name_the_old_rewriting_gave_is_not_the_name_lever_posts(self):
        # "Résumé (1).pdf" used to be reported as R_sum_1_.pdf; the page does not post that, so a read under the old name is not the student's.
        decision = self.decide(PHASE_STUDENT, "Résumé_(1).pdf", student_file_name="R_sum_1_.pdf")
        self.assertEqual(decision.rule, "resume_post_name")

    def test_the_fill_lets_the_attached_name_pass_as_lever_posts_it_and_reads_any_other(self):
        attached = "Résumé (1) 555-0100.pdf"
        posted = lever.posted_file_name(attached)
        self.assertEqual(posted, "Résumé_(1)_555-0100.pdf")
        values = {**VALUES, "phone": "555-0100"}
        for name in (attached, posted):
            with self.subTest(name=name):
                self.assertIsInstance(self.decide(PHASE_FILL, name, values=values, resume_file_name=attached), Allow)
        other = self.decide(PHASE_FILL, "Résumé_(1)_555-0100_2.pdf", values=values, resume_file_name=attached)
        self.assertEqual(other.rule, "value_guard")


if __name__ == "__main__":
    unittest.main()
