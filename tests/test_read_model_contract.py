"""The read model's ordering, filtering and paging contract.

Written before the tenant read path was rewritten into SQL, to pin the
behaviour that rewrite must not change. There was no test asserting tenant
sort order at all, which is the seam a ranking regression would have shipped
through: nothing in the suite would have noticed cards reordering.

Every sort is asserted on *both* repository paths -- `user_id=None` (the CLI,
the agent, and the migration's target) and an authenticated user -- because the
two are separate implementations and the whole point is that they agree.
"""

from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent))

from opportunity_app.schema import connect_product, ensure_product_schema, sort_key
from pipeline_core import OpportunityFilters, OpportunityRepository

OWNER = "local-user"
OTHER = "other-user"
EPOCH = "2026-01-01T00:00:00+00:00"


class ReadModelContractTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.conn = connect_product(Path(temp.name) / "platform.db")
        self.addCleanup(self.conn.close)
        ensure_product_schema(self.conn)
        for user in (OWNER, OTHER):
            self.conn.execute(
                "INSERT OR IGNORE INTO users(id, email, display_name, role, created_at, updated_at) "
                "VALUES(?, NULL, 'Test', 'student', ?, ?)",
                (user, EPOCH, EPOCH),
            )
        self.conn.commit()

    # --- fixtures -----------------------------------------------------------

    def add(self, opportunity_id, **overrides):
        row = {
            "company": "Acme", "title": "Intern", "location": "Austin, TX",
            "region": "Austin", "role_type": "internship", "description": "desc",
            "posted_at": None, "posted_at_utc": None, "deadline_at": None,
            "first_seen_at": EPOCH, "last_seen_at": EPOCH, "active": 1,
            "duplicate_of": None,
        }
        row.update(overrides)
        self.conn.execute(
            """
            INSERT INTO opportunities(
                id, company, title, company_sort_key, title_sort_key, location, region,
                role_type, url, description,
                posted_at, posted_at_utc, deadline_at, first_seen_at, last_seen_at,
                active, fingerprint, content_fingerprint, duplicate_of, created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (opportunity_id, row["company"], row["title"],
             # Through the real function: leaving these empty makes every
             # company-sort assertion degenerate to id order and pass against
             # any implementation at all.
             sort_key(row["company"]), sort_key(row["title"]),
             row["location"], row["region"],
             row["role_type"], f"https://example.com/{opportunity_id}", row["description"],
             row["posted_at"], row["posted_at_utc"], row["deadline_at"],
             row["first_seen_at"], row["last_seen_at"], row["active"],
             opportunity_id, opportunity_id, row["duplicate_of"], EPOCH, EPOCH),
        )
        self.conn.execute(
            "INSERT INTO opportunity_sources(opportunity_id, source_key, source_name, external_id, "
            "source_url, first_seen_at, last_seen_at) VALUES(?, ?, ?, ?, ?, ?, ?)",
            (opportunity_id, overrides.get("source_key", "greenhouse:acme"),
             overrides.get("source_name", "Acme Board"), opportunity_id,
             f"https://example.com/{opportunity_id}", EPOCH, EPOCH),
        )
        self.conn.execute(
            "INSERT INTO opportunity_attributes(opportunity_id, remote_mode, terms_json, "
            "graduation_years_json, pay_min, pay_max, pay_period, currency, extracted_json, updated_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, '{}', ?)",
            (opportunity_id, overrides.get("remote_mode", "unknown"),
             overrides.get("terms_json", '[]'), overrides.get("graduation_years_json", '[]'),
             overrides.get("pay_min"), overrides.get("pay_max"),
             overrides.get("pay_period", ""), overrides.get("currency", ""), EPOCH),
        )
        for user in (OWNER, OTHER):
            self.conn.execute(
                "INSERT INTO fit_scores(opportunity_id, user_id, ruleset_version, score, "
                "explanation_json, created_at) VALUES(?, ?, 'legacy-v1', ?, '[]', ?)",
                (opportunity_id, user, overrides.get("score", 50), EPOCH),
            )
        self.conn.commit()

    def ordered(self, sort, user_id, **filters):
        items, _ = OpportunityRepository(self.conn, user_id=user_id).list(
            OpportunityFilters(sort=sort, limit=200, **filters)
        )
        return [item["id"] for item in items]

    def both_paths(self, sort, **filters):
        """(cli order, tenant order) -- these must be equal.

        The tenant is OTHER, never OWNER. `local-user` is the account baked
        into `opportunity_read_model`, so a tenant query that accidentally read
        the view's own score/status/intent columns would still agree with
        itself. OTHER's state is seeded separately and deliberately differs.
        """

        return self.ordered(sort, None, **filters), self.ordered(sort, OTHER, **filters)

    # --- sorts --------------------------------------------------------------

    def test_score_sort_breaks_ties_by_date_then_id(self):
        self.add("high", score=90)
        self.add("tie-b", score=50, posted_at_utc="2026-05-01T00:00:00.000000+00:00")
        self.add("tie-a", score=50, posted_at_utc="2026-05-01T00:00:00.000000+00:00")
        self.add("older", score=50, posted_at_utc="2026-04-01T00:00:00.000000+00:00")
        cli, tenant = self.both_paths("score")
        self.assertEqual(cli, ["high", "tie-a", "tie-b", "older"])
        self.assertEqual(cli, tenant)

    def test_newest_sort_falls_back_to_first_seen_when_no_date_was_stated(self):
        self.add("dated", posted_at_utc="2026-05-01T00:00:00.000000+00:00")
        self.add("undated", first_seen_at="2026-06-01T00:00:00+00:00")
        cli, tenant = self.both_paths("newest")
        self.assertEqual(cli, ["undated", "dated"])
        self.assertEqual(cli, tenant)

    def test_discovered_sort_uses_first_seen_and_ignores_the_posting_date(self):
        self.add("seen-late", first_seen_at="2026-06-01T00:00:00+00:00",
                 posted_at_utc="2026-01-01T00:00:00.000000+00:00")
        self.add("seen-early", first_seen_at="2026-02-01T00:00:00+00:00",
                 posted_at_utc="2026-12-01T00:00:00.000000+00:00")
        cli, tenant = self.both_paths("discovered")
        self.assertEqual(cli, ["seen-late", "seen-early"])
        self.assertEqual(cli, tenant)

    def test_deadline_sort_puts_undated_last_and_ties_break_by_score_then_id(self):
        self.add("soon", deadline_at="2026-03-01")
        self.add("later", deadline_at="2026-09-01")
        self.add("tie-low", deadline_at="2026-03-01", score=10)
        self.add("none", deadline_at=None)
        cli, tenant = self.both_paths("deadline")
        self.assertEqual(cli, ["soon", "tie-low", "later", "none"])
        self.assertEqual(cli, tenant)

    def test_an_empty_deadline_is_not_the_same_as_no_deadline(self):
        """Python treats only None as missing, so '' sorts as a real deadline.

        A cleanup that used NULLIF or truthiness would move it to the end and
        silently reorder the urgent view.
        """

        self.add("empty", deadline_at="")
        self.add("dated", deadline_at="2026-03-01")
        self.add("null", deadline_at=None)
        cli, tenant = self.both_paths("deadline")
        self.assertEqual(cli, ["empty", "dated", "null"])
        self.assertEqual(cli, tenant)

    def test_company_sort_is_case_insensitive_and_breaks_ties_by_title_then_id(self):
        self.add("b", company="acme", title="Zeta")
        self.add("a", company="Acme", title="Alpha")
        self.add("c", company="Borealis", title="Alpha")
        cli, tenant = self.both_paths("company")
        self.assertEqual(cli, ["a", "b", "c"])
        self.assertEqual(cli, tenant)

    def test_company_sort_agrees_across_paths_on_a_non_ascii_collision(self):
        """The adversarial case: casefold ties these, SQLite NOCASE does not.

        'Straße'.casefold() == 'strasse', so Python considers the two equal and
        the id breaks the tie. SQLite's NOCASE folds A-Z only, leaves U+00DF
        alone, and orders it after 's'. The two paths therefore disagreed, and
        whichever one a caller happened to use decided the order.
        """

        self.add("a", company="Straße")
        self.add("b", company="Strasse")
        cli, tenant = self.both_paths("company")
        # The expected order, not merely agreement: both paths read SORT_SQL
        # now, so "they agree" is structurally true and would survive a revert
        # to COLLATE NOCASE. casefold makes these two equal, so the id breaks
        # the tie and 'a' leads; NOCASE would order U+00DF after 's' and put
        # 'b' first.
        self.assertEqual(cli, ["a", "b"])
        self.assertEqual(cli, tenant)

    def test_company_sort_orders_a_non_ascii_name_by_its_folded_form(self):
        """Ø folds to ø, which sorts after 'b' and before 'z'.

        COLLATE NOCASE leaves U+00D8 alone and orders it after every ASCII
        letter, putting Ørsted last instead.
        """

        self.add("orsted", company="Ørsted")
        self.add("borealis", company="Borealis")
        self.add("zebra", company="Zebra")
        cli, tenant = self.both_paths("company")
        self.assertEqual(cli, ["borealis", "zebra", "orsted"])
        self.assertEqual(cli, tenant)

    # --- filters ------------------------------------------------------------

    def test_each_filter_selects_the_same_rows_on_both_paths(self):
        self.add("remote", remote_mode="remote", role_type="internship", region="Austin")
        self.add("onsite", remote_mode="onsite", role_type="co-op", region="Dallas")
        self.add("paid", remote_mode="hybrid", pay_min=30.0, pay_max=40.0, pay_period="hour")
        self.add("summer", terms_json='["summer 2027"]', graduation_years_json="[2027]",
                 deadline_at="2026-05-01", posted_at="2026-04-01T00:00:00+00:00",
                 posted_at_utc="2026-04-01T00:00:00.000000+00:00")
        # Every filter the API exposes, not just the convenient ones -- a filter
        # left out here is a filter whose SQL nobody checks.
        for label, filters in (
            ("role_type", {"role_type": "co-op"}),
            ("region", {"region": "Dallas"}),
            ("remote_mode", {"remote_mode": "remote"}),
            ("query", {"query": "Intern"}),
            ("min_hourly_pay", {"min_hourly_pay": 20.0}),
            ("source", {"source": "Acme Board"}),
            ("term", {"term": "summer 2027"}),
            ("graduation_year", {"graduation_year": 2027}),
            ("posted_since", {"posted_since": "2026-03-01"}),
            ("deadline_before", {"deadline_before": "2026-06-01"}),
            ("status", {"status": "discovered"}),
            ("intent_state", {"intent_state": "undecided"}),
            ("exclude_passed", {"exclude_passed": True}),
        ):
            with self.subTest(filter=label):
                cli, tenant = self.both_paths("score", **filters)
                self.assertEqual(cli, tenant)
                self.assertTrue(cli, f"the {label} filter matched nothing, so it proves nothing")

    def test_inactive_and_duplicate_rows_are_hidden_by_default(self):
        self.add("live")
        self.add("retired", active=0)
        self.add("dupe", duplicate_of="live")
        for user_id in (None, OWNER):
            with self.subTest(path="tenant" if user_id else "cli"):
                self.assertEqual(self.ordered("score", user_id), ["live"])

    # --- paging -------------------------------------------------------------

    def test_paging_walks_the_whole_set_without_gaps_or_repeats(self):
        for index in range(12):
            self.add(f"row-{index:02d}", score=100 - index)
        for user_id in (None, OWNER):
            with self.subTest(path="tenant" if user_id else "cli"):
                repo = OpportunityRepository(self.conn, user_id=user_id)
                collected, total = [], None
                for offset in range(0, 12, 5):
                    items, total = repo.list(OpportunityFilters(limit=5, offset=offset))
                    collected.extend(item["id"] for item in items)
                self.assertEqual(total, 12, "total must count the whole set, not the page")
                self.assertEqual(collected, [f"row-{i:02d}" for i in range(12)])

    def test_total_is_unaffected_by_limit(self):
        for index in range(7):
            self.add(f"row-{index}")
        for user_id in (None, OWNER):
            with self.subTest(path="tenant" if user_id else "cli"):
                repo = OpportunityRepository(self.conn, user_id=user_id)
                self.assertEqual(repo.list(OpportunityFilters(limit=1))[1], 7)
                self.assertEqual(repo.list(OpportunityFilters(limit=200))[1], 7)

    # --- aggregates over the unpaged set ------------------------------------

    def test_stats_count_the_whole_inventory_not_one_page(self):
        for index in range(9):
            self.add(f"row-{index}", score=index * 10)
        self.add("retired", active=0)
        for user_id in (None, OWNER):
            with self.subTest(path="tenant" if user_id else "cli"):
                stats = OpportunityRepository(self.conn, user_id=user_id).stats()
                self.assertEqual(stats["total"], 10)
                self.assertEqual(stats["active_unique"], 9)
                self.assertEqual(stats["top_score"], 80)

    def test_facets_cover_the_whole_inventory_not_one_page(self):
        self.add("a", region="Austin", role_type="internship", remote_mode="remote")
        self.add("b", region="Dallas", role_type="co-op", remote_mode="onsite")
        for index in range(60):
            self.add(f"filler-{index}", region="Houston")
        for user_id in (None, OWNER):
            with self.subTest(path="tenant" if user_id else "cli"):
                facets = OpportunityRepository(self.conn, user_id=user_id).facets()
                self.assertEqual(set(facets["regions"]), {"Austin", "Dallas", "Houston"})
                self.assertEqual(set(facets["role_types"]), {"internship", "co-op"})

    # --- gaps found by a mutation sweep -------------------------------------
    #
    # Each test below exists because a specific edit to the code survived the
    # whole suite: nothing failed when it was broken. They are written against
    # the behaviour, and each names the mutation it was written to kill.

    def test_score_ties_without_a_posting_date_break_on_when_last_seen(self):
        """Kills COALESCE -> NULLIF on the score sort's date fallback.

        Every earlier score-tie test gave both rows a posting date, so the
        last_seen_at fallback was never the deciding key.
        """

        self.add("seen-earlier", score=50, last_seen_at="2026-03-01T00:00:00+00:00")
        self.add("seen-later", score=50, last_seen_at="2026-06-01T00:00:00+00:00")
        cli, tenant = self.both_paths("score")
        self.assertEqual(cli, ["seen-later", "seen-earlier"])
        self.assertEqual(cli, tenant)

    def test_a_user_with_no_score_reads_zero_and_an_empty_ruleset(self):
        """Kills COALESCE -> NULLIF on the tenant's ruleset_version.

        Tenancy tests always seeded a score for every user, so the missing-score
        path -- which every new account starts on -- was never taken.
        """

        self.add("unscored")
        self.conn.execute("DELETE FROM fit_scores WHERE user_id=?", (OTHER,))
        self.conn.commit()
        repo = OpportunityRepository(self.conn, user_id=OTHER)
        for path, item in (
            ("get", repo.get("unscored")),
            ("list", repo.list(OpportunityFilters())[0][0]),
        ):
            with self.subTest(path=path):
                self.assertEqual(item["score"], 0)
                self.assertEqual(item["score_version"], "")
                self.assertEqual(item["reasons"], [])

    def test_facets_never_offer_an_empty_value(self):
        """Kills `and` -> `or` in the facet filter.

        The previous per-column queries excluded '' in SQL. An empty region
        rendered as a blank option in the filter dropdown.
        """

        self.add("blank-region", region="")
        self.add("named-region", region="Austin")
        for user_id in (None, OTHER):
            with self.subTest(path="tenant" if user_id else "cli"):
                facets = OpportunityRepository(self.conn, user_id=user_id).facets()
                self.assertEqual(facets["regions"], ["Austin"])

    def test_filter_boundaries_are_inclusive(self):
        """Kills >= -> > and <= -> < on the three range filters.

        Every filter test used a value comfortably inside the range, so an
        off-by-one at the boundary was invisible. A posting paying exactly the
        minimum, posted exactly on the cut-off, or due exactly on the date asked
        for must be included.
        """

        self.add("exact-pay", pay_min=20.0, pay_max=25.0, pay_period="hour")
        self.add("exact-posted", posted_at="2026-05-01T00:00:00+00:00")
        self.add("exact-deadline", deadline_at="2026-06-01")
        for label, filters, expected in (
            ("min_hourly_pay", {"min_hourly_pay": 25.0}, "exact-pay"),
            ("posted_since", {"posted_since": "2026-05-01T00:00:00+00:00"}, "exact-posted"),
            ("deadline_before", {"deadline_before": "2026-06-01"}, "exact-deadline"),
        ):
            with self.subTest(filter=label):
                cli, tenant = self.both_paths("score", **filters)
                self.assertIn(expected, cli)
                self.assertEqual(cli, tenant)

    def test_posted_since_falls_back_to_first_seen_for_undated_postings(self):
        """Kills COALESCE -> NULLIF on the posted_since filter.

        An undated posting first seen after the cut-off is recent and must be
        kept; without the fallback it compares NULL and silently disappears.
        """

        self.add("undated-recent", posted_at=None, first_seen_at="2026-08-01T00:00:00+00:00")
        self.add("undated-old", posted_at=None, first_seen_at="2026-01-01T00:00:00+00:00")
        cli, tenant = self.both_paths("score", posted_since="2026-05-01")
        self.assertEqual(cli, ["undated-recent"])
        self.assertEqual(cli, tenant)

    def test_compensation_is_only_known_when_a_figure_was_stated(self):
        """Kills `is not` -> `is` on compensation.known.

        The card uses this to decide between showing a figure and saying pay
        was not stated. Inverting it would present an absent figure as known.
        """

        self.add("paid", pay_min=30.0, pay_max=40.0, pay_period="hour")
        self.add("unstated")
        repo = OpportunityRepository(self.conn, user_id=OTHER)
        self.assertTrue(repo.get("paid")["compensation"]["known"])
        self.assertFalse(repo.get("unstated")["compensation"]["known"])

    def test_a_new_database_is_put_in_wal_mode(self):
        """Kills != -> == on the journal-mode check.

        connect_product now reads the mode before setting it, to avoid paying
        for the switch on every request. Inverting that check would mean a new
        database is never switched at all -- slower, and nothing would fail.
        """

        with TemporaryDirectory() as directory:
            fresh = connect_product(Path(directory) / "fresh.db")
            try:
                mode = fresh.execute("PRAGMA journal_mode").fetchone()[0]
            finally:
                fresh.close()
        self.assertEqual(str(mode).lower(), "wal")

    # --- tenancy ------------------------------------------------------------

    def test_one_users_state_never_reaches_another(self):
        self.add("shared")
        self.conn.execute(
            "INSERT INTO applications(id, opportunity_id, user_id, stage, notes, applied_at, "
            "follow_up_at, created_at, updated_at) VALUES('app', 'shared', ?, 'offer', 'mine', "
            "NULL, NULL, ?, ?)",
            (OWNER, EPOCH, EPOCH),
        )
        self.conn.execute(
            "INSERT INTO opportunity_interactions(opportunity_id, user_id, action, created_at) "
            "VALUES('shared', ?, 'saved', ?)",
            (OWNER, EPOCH),
        )
        self.conn.commit()
        owner = OpportunityRepository(self.conn, user_id=OWNER).get("shared")
        other = OpportunityRepository(self.conn, user_id=OTHER).get("shared")
        self.assertEqual(owner["status"], "offer")
        self.assertEqual(owner["notes"], "mine")
        self.assertEqual(owner["intent_state"], "saved")
        self.assertEqual(other["status"], "discovered")
        self.assertEqual(other["notes"], "")
        self.assertEqual(other["intent_state"], "")

    def test_a_tenant_never_inherits_the_local_owners_score(self):
        """The view carries local-user's score; an API caller must not see it."""

        self.add("scored", score=50)
        self.conn.execute(
            "UPDATE fit_scores SET score=99 WHERE opportunity_id='scored' AND user_id=?", (OWNER,)
        )
        self.conn.execute("DELETE FROM fit_scores WHERE opportunity_id='scored' AND user_id=?", (OTHER,))
        self.conn.commit()
        self.assertEqual(OpportunityRepository(self.conn, user_id=OWNER).get("scored")["score"], 99)
        self.assertEqual(OpportunityRepository(self.conn, user_id=OTHER).get("scored")["score"], 0)


if __name__ == "__main__":
    unittest.main()
