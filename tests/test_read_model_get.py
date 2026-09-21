"""Contract tests for `OpportunityRepository.get()`.

`get()` used to reach one opportunity by materialising the whole inventory and
scanning it, which made a single detail-panel open cost the same as listing
everything. Replacing that with a single-row query is only safe if the parts
the old implementation got for free are still enforced:

  * source eligibility -- blocked and disabled sources hide an opportunity
  * cardinality -- several sources must not duplicate or change the row
  * tenancy -- one user never sees another's score, status, notes or intent
  * user-state rules -- intent, application precedence, and missing rows

Each of these is checked against `list()` as well, so the single-row path and
the list path cannot drift apart.
"""

import sqlite3
import tempfile
import unittest
from pathlib import Path

from pipeline_core import OpportunityFilters, OpportunityRepository
from opportunity_app.schema import connect_product

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers_platform import build_and_migrate  # noqa: E402


OTHER_USER = "other-user"
OWNER = "local-user"


class ReadModelGetTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="read-model-get-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        _, self.platform_path = build_and_migrate(self.root)
        self.conn = connect_product(self.platform_path)
        self.addCleanup(self.conn.close)
        self.conn.execute(
            "INSERT OR IGNORE INTO users(id, email, display_name, role, created_at, updated_at) "
            "VALUES(?, NULL, 'Other Student', 'student', '2026-09-01T00:00:00+00:00', '2026-09-01T00:00:00+00:00')",
            (OTHER_USER,),
        )
        self.conn.commit()

    def repo(self, user_id):
        return OpportunityRepository(self.conn, user_id=user_id)

    def listed_ids(self, user_id):
        items, _ = self.repo(user_id).list(OpportunityFilters(limit=200))
        return [item["id"] for item in items]

    # --- source eligibility -------------------------------------------------

    def _control(self, source_key, *, enabled=1, moderation_status="approved"):
        self.conn.execute(
            "INSERT INTO source_controls(source_key, enabled, moderation_status, note, updated_by, updated_at) "
            "VALUES(?, ?, ?, '', 'test', '2026-09-01T00:00:00+00:00') "
            "ON CONFLICT(source_key) DO UPDATE SET enabled=excluded.enabled, "
            "moderation_status=excluded.moderation_status",
            (source_key, enabled, moderation_status),
        )
        self.conn.commit()

    def test_blocked_only_source_hides_the_opportunity_from_get_and_list(self):
        self._control("greenhouse:acme", moderation_status="blocked")
        self.assertIsNone(self.repo(OWNER).get("job-a"))
        self.assertNotIn("job-a", self.listed_ids(OWNER))

    def test_disabled_only_source_hides_the_opportunity_from_get_and_list(self):
        self._control("greenhouse:acme", enabled=0)
        self.assertIsNone(self.repo(OWNER).get("job-a"))
        self.assertNotIn("job-a", self.listed_ids(OWNER))

    def test_sourceless_opportunity_is_invisible(self):
        self.conn.execute("DELETE FROM opportunity_sources WHERE opportunity_id='job-a'")
        self.conn.commit()
        self.assertIsNone(self.repo(OWNER).get("job-a"))
        self.assertNotIn("job-a", self.listed_ids(OWNER))

    def test_one_eligible_source_among_blocked_ones_keeps_it_visible(self):
        self.conn.execute(
            "INSERT INTO opportunity_sources(opportunity_id, source_key, source_name, external_id, "
            "source_url, first_seen_at, last_seen_at) VALUES('job-a', 'aggregator:spam', 'Spam Board', "
            "'x-1', 'https://example.com/spam', '2026-08-01T00:00:00+00:00', '2026-08-09T00:00:00+00:00')"
        )
        self.conn.commit()
        self._control("aggregator:spam", moderation_status="blocked")
        item = self.repo(OWNER).get("job-a")
        self.assertIsNotNone(item)
        # The blocked board must not become the displayed provenance.
        self.assertEqual(item["source_key"], "greenhouse:acme")

    def test_multiple_sources_do_not_duplicate_the_row(self):
        self.conn.execute(
            "INSERT INTO opportunity_sources(opportunity_id, source_key, source_name, external_id, "
            "source_url, first_seen_at, last_seen_at) VALUES('job-a', 'zz-later:acme', 'Later Board', "
            "'z-1', 'https://example.com/later', '2026-08-01T00:00:00+00:00', '2026-08-09T00:00:00+00:00')"
        )
        self.conn.commit()
        self.assertEqual(self.listed_ids(OWNER).count("job-a"), 1)
        self.assertEqual(self.repo(OWNER).get("job-a")["source_key"], "greenhouse:acme")

    def test_primary_source_is_chosen_by_key_not_by_insertion_order(self):
        # Inserted last but lexicographically first. An implementation that
        # simply took whichever row came back first would keep the original
        # source and still pass the test above, so this one inverts the two.
        self.conn.execute(
            "INSERT INTO opportunity_sources(opportunity_id, source_key, source_name, external_id, "
            "source_url, first_seen_at, last_seen_at) VALUES('job-a', 'aaa-earlier:acme', 'Earlier Board', "
            "'a-9', 'https://example.com/earlier', '2026-08-01T00:00:00+00:00', '2026-08-09T00:00:00+00:00')"
        )
        self.conn.commit()
        self.assertEqual(self.listed_ids(OWNER).count("job-a"), 1)
        item = self.repo(OWNER).get("job-a")
        self.assertEqual(item["source_key"], "aaa-earlier:acme")
        self.assertEqual(item["source_name"], "Earlier Board")

    def test_get_agrees_with_list_on_every_visible_field(self):
        items, _ = self.repo(OWNER).list(OpportunityFilters(limit=200))
        for listed in items:
            with self.subTest(opportunity=listed["id"]):
                self.assertEqual(self.repo(OWNER).get(listed["id"]), listed)

    def test_unknown_id_returns_none(self):
        self.assertIsNone(self.repo(OWNER).get("no-such-opportunity"))

    def test_inactive_opportunity_is_still_reachable_by_id(self):
        # list() hides inactive rows, but a deep link to one must still resolve
        # rather than 404 -- the old implementation passed active_only=False.
        self.conn.execute("UPDATE opportunities SET active=0 WHERE id='job-a'")
        self.conn.commit()
        self.assertNotIn("job-a", self.listed_ids(OWNER))
        self.assertIsNotNone(self.repo(OWNER).get("job-a"))

    # --- tenancy ------------------------------------------------------------

    def test_a_second_user_inherits_no_score_status_notes_or_intent(self):
        owner_view = self.repo(OWNER).get("job-b")
        other_view = self.repo(OTHER_USER).get("job-b")
        self.assertEqual(owner_view["status"], "applied")
        self.assertEqual(owner_view["notes"], "Applied on employer site")
        self.assertGreater(owner_view["score"], 0)

        self.assertEqual(other_view["status"], "discovered")
        self.assertEqual(other_view["notes"], "")
        self.assertEqual(other_view["score"], 0)
        self.assertEqual(other_view["intent_state"], "")
        self.assertIsNone(other_view["applied_at"])
        self.assertIsNone(other_view["follow_up_at"])
        # Same opportunity, so the inventory half must be identical.
        self.assertEqual(owner_view["company"], other_view["company"])
        self.assertEqual(owner_view["url"], other_view["url"])

    # --- user-state rules ---------------------------------------------------

    def _interact(self, action, *, user_id=OTHER_USER, created_at="2026-09-02T00:00:00+00:00"):
        self.conn.execute(
            "INSERT INTO opportunity_interactions(opportunity_id, user_id, action, created_at) "
            "VALUES('job-a', ?, ?, ?)",
            (user_id, action, created_at),
        )
        self.conn.commit()

    def test_saved_without_an_application_reads_as_shortlisted(self):
        self._interact("saved")
        item = self.repo(OTHER_USER).get("job-a")
        self.assertEqual(item["intent_state"], "saved")
        self.assertEqual(item["status"], "shortlisted")

    def test_passed_is_intent_but_not_a_status(self):
        self._interact("passed")
        item = self.repo(OTHER_USER).get("job-a")
        self.assertEqual(item["intent_state"], "passed")
        self.assertEqual(item["status"], "discovered")

    def test_non_intent_actions_leave_the_opportunity_undecided(self):
        for action in ("seen", "apply_opened", "undo"):
            with self.subTest(action=action):
                self.conn.execute("DELETE FROM opportunity_interactions WHERE user_id=?", (OTHER_USER,))
                self.conn.commit()
                self._interact(action)
                self.assertEqual(self.repo(OTHER_USER).get("job-a")["intent_state"], "")

    def test_latest_interaction_wins_by_id_not_by_timestamp(self):
        # The later row carries the OLDER timestamp. Ordering by created_at
        # would pick 'saved'; MAX(id) -- which is what the list path uses --
        # picks 'passed'. Equal timestamps would not tell the two apart.
        self._interact("saved", created_at="2026-09-09T00:00:00+00:00")
        self._interact("passed", created_at="2026-09-02T00:00:00+00:00")
        self.assertEqual(self.repo(OTHER_USER).get("job-a")["intent_state"], "passed")
        # ...and the list path must agree, or the two have drifted.
        listed = next(
            item
            for item in self.repo(OTHER_USER).list(OpportunityFilters(limit=200))[0]
            if item["id"] == "job-a"
        )
        self.assertEqual(listed["intent_state"], "passed")

    def test_every_intent_and_application_stage_combination_agrees_with_list(self):
        stages = ("applying", "applied", "interview", "offer", "rejected", "withdrawn", "archived")
        for stage in stages:
            for action in ("saved", "passed", "seen", "undo"):
                with self.subTest(stage=stage, action=action):
                    self.conn.execute("DELETE FROM applications WHERE user_id=?", (OTHER_USER,))
                    self.conn.execute("DELETE FROM opportunity_interactions WHERE user_id=?", (OTHER_USER,))
                    self.conn.execute(
                        "INSERT INTO applications(id, opportunity_id, user_id, stage, notes, applied_at, "
                        "follow_up_at, created_at, updated_at) VALUES('app-x', 'job-a', ?, ?, '', NULL, NULL, "
                        "'2026-09-01T00:00:00+00:00', '2026-09-01T00:00:00+00:00')",
                        (OTHER_USER, stage),
                    )
                    self.conn.commit()
                    self._interact(action)
                    item = self.repo(OTHER_USER).get("job-a")
                    # The stage always wins, whatever the interaction says.
                    self.assertEqual(item["status"], stage)
                    self.assertEqual(
                        item["intent_state"], action if action in {"saved", "passed"} else ""
                    )
                    listed = next(
                        entry
                        for entry in self.repo(OTHER_USER).list(
                            OpportunityFilters(active_only=False, limit=200)
                        )[0]
                        if entry["id"] == "job-a"
                    )
                    self.assertEqual(item, listed)

    def test_undo_after_saving_clears_intent(self):
        self._interact("saved", created_at="2026-09-02T00:00:00+00:00")
        self._interact("undo", created_at="2026-09-03T00:00:00+00:00")
        item = self.repo(OTHER_USER).get("job-a")
        self.assertEqual(item["intent_state"], "")
        self.assertEqual(item["status"], "discovered")

    def test_an_application_stage_outranks_a_saved_interaction(self):
        self.conn.execute(
            "INSERT INTO applications(id, opportunity_id, user_id, stage, notes, applied_at, "
            "follow_up_at, created_at, updated_at) VALUES('app-1', 'job-a', ?, 'interview', 'Onsite', "
            "'2026-09-01T00:00:00+00:00', NULL, '2026-09-01T00:00:00+00:00', '2026-09-01T00:00:00+00:00')",
            (OTHER_USER,),
        )
        self.conn.commit()
        self._interact("saved")
        item = self.repo(OTHER_USER).get("job-a")
        self.assertEqual(item["intent_state"], "saved")
        # Saving an opportunity again must never demote it out of interview.
        self.assertEqual(item["status"], "interview")
        self.assertEqual(item["notes"], "Onsite")

    def test_a_score_from_another_ruleset_is_not_adopted(self):
        self.conn.execute(
            "INSERT INTO fit_scores(opportunity_id, user_id, ruleset_version, score, "
            "explanation_json, created_at) VALUES('job-a', ?, 'experimental-v2', 99, '[]', "
            "'2026-09-01T00:00:00+00:00')",
            (OTHER_USER,),
        )
        self.conn.commit()
        self.assertEqual(self.repo(OTHER_USER).get("job-a")["score"], 0)

    # --- facets ordering ----------------------------------------------------

    def test_facet_ordering_matches_sqlite_nocase_for_non_ascii(self):
        """Facet order must not change when the sort moves into Python.

        SQLite's NOCASE folds A-Z only, so 'Ssource' < 'Test' < 'ssource'
        collapses to Ssource/ssource together and 'ßource' lands after 'Test'
        by code point. Python's casefold expands 'ß' to 'ss' and would move it
        before 'Test' instead -- a silent reordering of the region filter.
        """

        self.conn.execute("UPDATE opportunities SET region='Test' WHERE id='job-a'")
        self.conn.execute("UPDATE opportunities SET region='ßource' WHERE id='job-b'")
        self.conn.commit()

        expected = [
            row[0]
            for row in self.conn.execute(
                "SELECT DISTINCT region FROM opportunity_read_model "
                "WHERE active=1 AND duplicate_of IS NULL AND region <> '' "
                "ORDER BY region COLLATE NOCASE"
            )
        ]
        self.assertEqual(self.repo(OWNER).facets()["regions"], expected)
        self.assertIn("ßource", expected)
        # The exact hazard: casefold would put 'ßource' before 'Test'.
        self.assertLess(expected.index("Test"), expected.index("ßource"))

    def test_get_still_agrees_with_list_once_user_state_exists(self):
        self._interact("saved")
        listed = next(
            item
            for item in self.repo(OTHER_USER).list(OpportunityFilters(limit=200))[0]
            if item["id"] == "job-a"
        )
        self.assertEqual(self.repo(OTHER_USER).get("job-a"), listed)


if __name__ == "__main__":
    unittest.main()
