"""settings_payload computes each feature's requirement once, and every entry is what can_turn_on and requirement said.

It used to call can_turn_on (which reads the mode again and computes the requirement when the switch is not on) and
then requirement() a second time for the panel's own field. The reference below builds each entry from those two
public functions, which is what the old code did.
"""

import itertools
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from opportunity_app import automation
from opportunity_app.schema import connect_product
from opportunity_app.settings_store import get_setting, put_setting
from opportunity_app.timestamps import utc_now

from helpers_platform import build_and_migrate

USER = "local-user"
NOW = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)


def reference_payload(conn, now):
    current = automation.modes(conn, USER)
    features = []
    for feature in automation.FEATURES.values():
        allowed, reason = automation.can_turn_on(conn, USER, feature.key, now=now)
        features.append({
            "key": feature.key, "label": feature.label, "description": feature.description, "group": feature.group,
            "risk": feature.risk, "modes": list(feature.modes), "mode": current[feature.key],
            "shadow_since": get_setting(conn, USER, f"{feature.key}.shadow_since") if feature.shadow_capable else None,
            "can_turn_on": allowed, "can_turn_on_reason": reason,
            "requirement": automation.requirement(conn, USER, feature.key),
        })
    return {"paused": automation.paused(conn, USER), "features": features}


class SettingsPayloadParityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        _, platform_path = build_and_migrate(Path(self.tmp.name))
        self.conn = connect_product(platform_path)
        self.addCleanup(self.conn.close)

    def put(self, key, value):
        with self.conn:
            put_setting(self.conn, USER, key, value, utc_now())

    def test_the_payload_is_what_the_public_gates_say_in_every_mix_of_modes(self):
        keys = list(automation.FEATURES)
        self.assertGreater(len(keys), 10)
        for step, mode in enumerate(itertools.cycle(("off", "shadow", "on"))):
            if step > 8:
                break
            with self.subTest(everything=mode):
                for index, key in enumerate(keys):
                    feature = automation.FEATURES[key]
                    chosen = mode if mode in feature.modes else "off"
                    if (index + step) % 4 == 0:
                        chosen = "off"
                    self.put(key, chosen)
                    if chosen == "shadow":
                        self.put(f"{key}.shadow_since", (NOW - timedelta(hours=step * 20)).isoformat())
                self.put(automation.PAUSED_KEY, "on" if step % 2 else "off")
                self.assertEqual(automation.settings_payload(self.conn, USER, now=NOW), reference_payload(self.conn, NOW))

    def test_a_feature_that_is_on_keeps_its_reasons(self):
        for key in automation.FEATURES:
            self.put(key, "on")
        payload = automation.settings_payload(self.conn, USER, now=NOW)
        self.assertEqual(payload, reference_payload(self.conn, NOW))
        self.assertTrue(all(entry["can_turn_on"] and entry["can_turn_on_reason"] == "" for entry in payload["features"]
                            if entry["mode"] == "on"))

    def test_each_requirement_is_computed_once(self):
        calls = []
        real = automation.requirement

        def counting(conn, user_id, key):
            calls.append(key)
            return real(conn, user_id, key)

        with mock.patch.object(automation, "requirement", counting):
            automation.settings_payload(self.conn, USER, now=NOW)
        self.assertEqual(sorted(calls), sorted(automation.FEATURES))


if __name__ == "__main__":
    unittest.main()
