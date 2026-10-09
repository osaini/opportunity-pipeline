"""The two Lever switches (docs/phase5-lever-handoff-spec.md section 9): registered like apply_agent, off by default, and never on by themselves.

``apply_agent_lever`` lets Apply for me read a saved Lever role and needs ``apply_agent`` on. ``apply_lever_resume_upload`` is L1's
choice (the app may attach the résumé on Lever) and needs the first. Neither is turned on by code. No browser and no network.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import helpers_source
import realdata_guard

realdata_guard.install()

from opportunity_app.automation import ledger as automation

from helpers_apply import USER, ApplyCase, setUpModule, tearDownModule  # noqa: F401


class LeverSwitchTests(ApplyCase):
    def setUp(self):
        super().setUp()
        # What create_app does as it starts: apply_agent has a requirement of its own that a throwaway database cannot meet.
        self.enter = automation.REQUIREMENTS.get("apply_agent")
        automation.register_requirement("apply_agent", lambda conn, user_id: "")
        self.addCleanup(automation.register_requirement, "apply_agent", self.enter)

    def test_both_are_registered_external_features_in_the_applications_group_with_on_and_off(self):
        for key in ("apply_agent_lever", "apply_lever_resume_upload"):
            with self.subTest(key=key):
                feature = automation.FEATURES[key]
                self.assertEqual((feature.group, feature.risk, feature.modes), ("applications", "external", ("off", "on")))
                self.assertFalse(feature.shadow_capable)

    def test_they_are_off_for_a_student_who_never_touched_them(self):
        self.assertEqual(automation.modes(self.conn, USER, ["apply_agent", "apply_agent_lever", "apply_lever_resume_upload"]),
                         {"apply_agent": "off", "apply_agent_lever": "off", "apply_lever_resume_upload": "off"})

    def test_lever_cannot_be_turned_on_while_apply_for_me_is_off(self):
        allowed, why = automation.can_turn_on(self.conn, USER, "apply_agent_lever")
        self.assertEqual((allowed, why), (False, automation.LEVER_NEEDS_APPLY_AGENT))
        with self.assertRaises(automation.AutomationGateError):
            automation.set_mode(self.conn, USER, "apply_agent_lever", "on")
        self.assertEqual(automation.mode(self.conn, USER, "apply_agent_lever"), "off")

    def test_the_resume_choice_needs_lever_on_and_lever_needs_apply_for_me(self):
        automation.set_mode(self.conn, USER, "apply_agent", "on")
        allowed, why = automation.can_turn_on(self.conn, USER, "apply_lever_resume_upload")
        self.assertEqual((allowed, why), (False, automation.RESUME_UPLOAD_NEEDS_LEVER))
        automation.set_mode(self.conn, USER, "apply_agent_lever", "on")
        self.assertEqual(automation.can_turn_on(self.conn, USER, "apply_lever_resume_upload"), (True, ""))
        automation.set_mode(self.conn, USER, "apply_lever_resume_upload", "on")
        self.assertEqual(automation.mode(self.conn, USER, "apply_lever_resume_upload"), "on")

    def test_the_settings_payload_names_what_each_still_needs(self):
        payload = automation.settings_payload(self.conn, USER)
        rows = {row["key"]: row for row in payload["features"]}
        self.assertEqual(rows["apply_agent_lever"]["mode"], "off")
        self.assertEqual(rows["apply_agent_lever"]["requirement"], automation.LEVER_NEEDS_APPLY_AGENT)
        self.assertEqual(rows["apply_lever_resume_upload"]["requirement"], automation.RESUME_UPLOAD_NEEDS_LEVER)

    def test_the_resume_choice_is_worded_as_the_student_answered_l1(self):
        feature = automation.FEATURES["apply_lever_resume_upload"]
        self.assertIn("Let the app attach my résumé on Lever", feature.label)
        self.assertIn("Lever reads it as soon as it is attached", feature.description)
        self.assertIn("sent to Lever before you press Submit", feature.description)
        self.assertFalse(feature.description.startswith(feature.label), "its first sentence is not its title again")

    def test_apply_for_me_on_lever_says_it_now_lets_finish_in_browser_fill_the_form(self):
        description = automation.FEATURES["apply_agent_lever"].description
        self.assertIn("what is missing", description)
        self.assertIn("Finish in browser", description)
        self.assertIn("window", description)

    def test_no_code_path_turns_either_on(self):
        # The only writes of these keys in the product are the student's own settings requests (the automation routes).
        offenders = []
        for path, text in helpers_source.python_modules("*.py").items():
            if path == "automation/ledger.py":
                continue
            if ("apply_agent_lever" in text or "apply_lever_resume_upload" in text) and "set_mode(" in text and '"on"' in text:
                offenders.append(path)
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
