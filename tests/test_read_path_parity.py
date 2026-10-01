"""Optimised web read paths against the straightforward code they replaced, on the same generated rows.

Each class keeps a small reference copy of the old implementation and asserts the new one returns exactly the same thing: the
outreach list's recontact count (one query instead of a get_target per row), the filtered outreach list (ordered ids from SQL
instead of a second full build), the tag facets (read from the facet scan instead of a second view scan) and /stats. The
references are deliberately simple so they stay obviously right.
"""

import random
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi.testclient import TestClient

import helpers_platform
from helpers_platform import build_and_migrate
from opportunity_app import STATIC_DIR, outreach_recontact
from opportunity_app.api import create_app
from collections import defaultdict

from opportunity_app.company_tags import capture_visible_sql, tag_facets, tag_facets_for_keys, tags_for_companies
from opportunity_app.outreach import create_target, filtered_target_ids, get_target, list_targets
from opportunity_app.outreach_recontact import eligible_targets, upgradeable
from opportunity_app.legacy_sync import migrate_legacy_database
from opportunity_app.database import connect_product
from opportunity_app.timestamps import utc_now
from pipeline_core import OpportunityFilters, OpportunityRepository
from pipeline_core.read_model import _decode_list, _nocase_key

USER = "local-user"
OTHER = "student-b"


def seed_targets(conn: sqlite3.Connection, count: int = 100, seed: int = 11) -> None:
    """Targets covering every branch upgradeable() looks at, with a second student's rows alongside."""
    rnd = random.Random(seed)
    stamp = utc_now()
    conn.execute(
        "INSERT INTO users(id, email, display_name, role, created_at, updated_at) VALUES(?, 'b@example.com', 'B', 'student', ?, ?)",
        (OTHER, stamp, stamp),
    )
    statuses = ["not_started", "not_started", "drafted", "drafted", "sent", "followed_up", "replied", "declined", "paused", "no_response"]
    channels = ["email", "linkedin", "form", "referral"]
    emails = ["", "info@{h}.example.com", "person@{h}.example.com", "careers@{h}.example.com", "hello@{h}.example.com"]
    for i in range(count):
        host = f"co{i}"
        payload = {
            "company": f"Company {i // 2} {'Alpha' if i % 2 else 'beta'}",
            "channel": rnd.choice(channels),
            "website": rnd.choice(["", f"https://{host}.example.com"]),
            "contact_email": rnd.choice(emails).format(h=host),
            "priority": rnd.choice(["P1", "P2", "P3"]),
            "location": rnd.choice(["Austin, TX", "West Virginia", "Remote", "San Francisco, CA", ""]),
            "summary": rnd.choice(["Builds robots", "Machine learning tools", ""]),
            "notes": rnd.choice(["", "call them", "50%_done"]),
            "status": rnd.choice(statuses),
        }
        if payload["status"] in {"sent", "followed_up"}:
            payload["sent_at"] = "2026-09-20"
        target = create_target(conn, payload, user_id=USER)
        if rnd.random() < 0.2:
            conn.execute("UPDATE outreach_targets SET not_interested_at=? WHERE id=?", (stamp, target["id"]))
        if rnd.random() < 0.2:
            conn.execute("UPDATE outreach_targets SET draft_status='approved' WHERE id=?", (target["id"],))
    # The other student's rows must never show up in this student's selection.
    for i in range(10):
        create_target(conn, {"company": f"Other Co {i}", "website": f"https://other{i}.example.com"}, user_id=OTHER)
    conn.commit()


def reference_eligible(conn: sqlite3.Connection, *, user_id: str) -> list[str]:
    """The implementation before the change: rebuild every target in full, then test it."""
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT id FROM outreach_targets WHERE user_id=? ORDER BY company COLLATE NOCASE", (user_id,)).fetchall()
    return [row["id"] for row in rows if upgradeable(get_target(conn, row["id"], user_id=user_id))]


class RecontactSelectionParityTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        _, self.platform_path = build_and_migrate(Path(self.tempdir.name))
        self.conn = connect_product(self.platform_path)
        self.addCleanup(self.conn.close)
        seed_targets(self.conn)

    def test_eligible_targets_match_the_per_target_rebuild_in_the_same_order(self):
        expected = reference_eligible(self.conn, user_id=USER)
        self.assertGreater(len(expected), 5, "the generated rows must include upgradeable targets")
        self.assertLess(len(expected), 90, "...and some that are not")
        self.assertEqual(eligible_targets(self.conn, user_id=USER), expected)
        self.assertEqual(eligible_targets(self.conn, user_id=OTHER), reference_eligible(self.conn, user_id=OTHER))

    def test_the_chosen_ids_filter_keeps_company_order(self):
        everything = reference_eligible(self.conn, user_id=USER)
        picked = set(everything[::3]) | {"outreach-missing"}
        self.assertEqual(
            outreach_recontact._upgradeable_ids(self.conn, user_id=USER, chosen=picked),
            [target_id for target_id in everything if target_id in picked],
        )

    def test_selection_reads_only_the_columns_it_declares(self):
        # upgradeable() must stay a function of raw columns; a derived key would raise KeyError here instead of silently
        # changing what a pass looks at.
        row = {column: None for column in outreach_recontact._UPGRADEABLE_COLUMNS}
        row.update(status="not_started", website="https://x.example", contact_email="")
        self.assertTrue(upgradeable(row))
        self.assertEqual(eligible_targets(self.conn, user_id="nobody"), [])


class FilteredOutreachListParityTests(unittest.TestCase):
    """GET /outreach with a filter picks its items out of the full list; list_targets(filters) is the reference."""

    FILTERS = [
        {"status": "sent"},
        {"status": "not_started"},
        {"channel": "email"},
        {"channel": "Email"},
        {"query": "robot"},
        {"query": "ROBOT"},
        {"query": "%"},
        {"query": "50%_d"},
        {"query": "ch_n"},
        {"query": "  tied  "},
        {"query": "zzzz-no-such-text"},
        {"query": "   "},
        {"status": "drafted", "channel": "linkedin", "query": "co"},
        {"status": "sent", "channel": "form", "query": "call"},
    ]

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        _, self.platform_path = build_and_migrate(Path(self.tempdir.name))
        self.conn = connect_product(self.platform_path)
        self.addCleanup(self.conn.close)
        seed_targets(self.conn, count=90, seed=5)
        # Ties on every sort key, so the order has to come from the same ORDER BY and not from a re-sort.
        ids = [row[0] for row in self.conn.execute(
            "SELECT id FROM outreach_targets WHERE user_id=? AND company LIKE 'Company 1%' ORDER BY id", (USER,))]
        self.assertGreaterEqual(len(ids), 4)
        for target_id, name in zip(ids[:4], ["Tied Co", "tied co", "TIED CO", "Tied co"]):
            self.conn.execute(
                "UPDATE outreach_targets SET company=?, priority='P1', follow_up_at=NULL, contact_email='', contact_confidence='unknown',"
                " status='not_started', channel='email', summary='tie' WHERE id=?", (name, target_id))
        self.conn.commit()

    def test_picking_the_filtered_ids_out_of_the_full_list_equals_listing_with_the_filter(self):
        everything = list_targets(self.conn, user_id=USER)
        by_id = {item["id"]: item for item in everything}
        matched = 0
        for filters in self.FILTERS:
            with self.subTest(filters=filters):
                expected = list_targets(self.conn, user_id=USER, **filters)
                ids = filtered_target_ids(self.conn, user_id=USER, **filters)
                self.assertEqual([by_id[target_id] for target_id in ids], expected)
                matched += bool(expected)
        self.assertGreaterEqual(matched, 8, "most generated filters must match something or the comparison proves little")

    def test_the_http_list_with_filters_matches_the_reference_listing(self):
        root = Path(self.tempdir.name)
        app = create_app(
            db_path=self.platform_path, access_token="parity-owner", static_dir=STATIC_DIR, resume_storage=root / "r",
            capture_storage=root / "c", interview_storage=root / "i",
        )
        headers = {"Authorization": "Bearer parity-owner"}
        with TestClient(app) as client:
            for params in ({"status": "sent"}, {"channel": "email", "q": "robot"}, {"q": "tied"}, {"q": "%"}, {"q": "no-such"}):
                with self.subTest(params=params):
                    body = client.get("/api/v1/outreach", headers=headers, params=params).json()
                    expected = list_targets(
                        self.conn, user_id=USER, status=params.get("status", ""), channel=params.get("channel", ""), query=params.get("q", ""),
                    )
                    self.assertEqual([item["id"] for item in body["items"]], [item["id"] for item in expected])
                    self.assertEqual(body["total"], len(expected))
            full = client.get("/api/v1/outreach", headers=headers).json()
            self.assertEqual(full["recontact"]["eligible"], len(reference_eligible(self.conn, user_id=USER)))
            # The summary is of everything, whatever the filter.
            filtered = client.get("/api/v1/outreach", headers=headers, params={"status": "sent"}).json()
            self.assertEqual(full["summary"], filtered["summary"])


def reference_location_region(text, regions):
    """location_region before its state patterns were compiled once: the same logic with re.search per call."""
    from opportunity_app.outreach_location import US_STATES, _region_states
    import re

    def mentions(lowered, term):
        needle = " ".join(str(term or "").casefold().split())
        return bool(needle) and re.search(rf"\b{re.escape(needle)}\b", lowered) is not None

    raw = str(text or "")
    lowered = " ".join(raw.casefold().split())
    if not lowered:
        return ""
    states = {code for code in re.findall(r",\s*([A-Z]{2})\b", raw) if code in US_STATES}
    states |= {code for code, name in US_STATES.items() if re.search(rf"\b{name}\b", lowered)}
    for region in regions:
        name = str(region.get("name") or "")
        if not name:
            continue
        own = _region_states(region)
        if states and own and not own & states:
            continue
        terms = [name, *(region.get("aliases") or [])]
        if states:
            terms += list(region.get("places") or [])
        if any(mentions(lowered, term) for term in terms):
            return name
    return ""


class LocationRegionParityTests(unittest.TestCase):
    REGIONS = [
        {"name": "Bay Area", "state_markers": ["ca", "california"], "aliases": ["bay area", "silicon valley"],
         "places": ["san francisco", "oakland", "palo alto"]},
        {"name": "Austin", "state_markers": ["tx", "texas"], "aliases": ["greater austin"], "places": ["austin", "round rock"]},
        {"name": "Virginia", "state_markers": ["va", "virginia"], "aliases": [], "places": ["norfolk"]},
        {"name": "Remote Friendly", "aliases": ["remote"], "places": []},
        {"name": "", "aliases": ["ghost"]},
    ]
    TEXTS = [
        "Austin, TX", "austin, texas", "Austin, MN", "Round Rock, TX", "San Francisco, CA", "SF Bay Area", "Palo Alto, California",
        "Oakland", "Norfolk, VA", "Norfolk, Virginia", "West Virginia", "Charleston, West Virginia", "Virginia Beach, VA",
        "Remote", "Remote - US", "Dublin, Ireland", "Silicon Valley", "Greater Austin area", "Washington, DC", "District of Columbia",
        "New York, New York", "Portland, OR", "", "   ", None, "UT Austin campus", "Salt Lake City, UT, Utah", "Ghost town",
    ]

    def test_precompiled_state_patterns_give_the_same_region_for_every_text(self):
        from opportunity_app.outreach_location import location_region

        for text in self.TEXTS:
            with self.subTest(text=text):
                self.assertEqual(location_region(text, self.REGIONS), reference_location_region(text, self.REGIONS))
        self.assertEqual(location_region("Charleston, West Virginia", self.REGIONS), "Virginia")


def build_inventory_database(root: Path, jobs: int = 60) -> Path:
    """A migrated database with tagged companies, inactive rows, duplicates, and one capture only student B may see."""
    legacy = root / "pipeline.db"
    conn = sqlite3.connect(legacy)
    conn.executescript(helpers_platform.LEGACY_SCHEMA)
    rnd = random.Random(3)
    words = ["Robotics", "Drone", "Space", "Systems", "Labs", "Bank", "Energy"]
    rows = []
    for i in range(jobs):
        rows.append((
            f"job-{i}", f"greenhouse:c{i}", f"Board {i % 7}", f"x-{i}", f"Firm {i % 23} {words[i % len(words)]}", f"Intern {i}",
            rnd.choice(["Austin, TX", "Remote", "Boston, MA", ""]), rnd.choice(["internship", "co-op", "research"]),
            f"https://example.com/jobs/{i}", f"Robots, rockets and machine learning work {i}. " * 5,
            "2026-08-08T00:00:00+00:00", "2026-08-08T01:00:00+00:00", "2026-09-29T01:00:00+00:00", 0 if i % 11 == 0 else 1,
            f"fp-{i}", f"cfp-{i}", f"job-{i - 1}" if i % 13 == 5 else None, rnd.randint(10, 99), '["35 base"]',
            rnd.choice(["discovered", "shortlisted", "applied"]), "", None, None,
        ))
    rows.append((
        "job-cap", "greenhouse:cap", "Board cap", "x-cap", "Capture Drone Labs", "Intern capture", "Austin, TX", "internship",
        "https://example.com/jobs/cap", "Drones and drone flight.", "2026-08-08T00:00:00+00:00", "2026-08-08T01:00:00+00:00",
        "2026-09-29T01:00:00+00:00", 1, "fp-cap", "cfp-cap", None, 50, '["35 base"]', "discovered", "", None, None,
    ))
    conn.executemany("INSERT INTO jobs VALUES(" + ",".join("?" * 23) + ")", rows)
    conn.commit()
    conn.close()
    platform = root / "platform.db"
    with helpers_platform.fast_throwaway_databases():
        migrate_legacy_database(legacy, platform, helpers_platform.build_profile(root))
    stamp = utc_now()
    with closing(connect_product(platform)) as db:
        db.execute(
            "INSERT INTO users(id, email, display_name, role, created_at, updated_at) VALUES(?, 'b@example.com', 'B', 'student', ?, ?)",
            (OTHER, stamp, stamp),
        )
        db.execute("UPDATE opportunity_sources SET source_key='manual:capture', external_id='cap-1' WHERE opportunity_id='job-cap'")
        db.execute(
            "INSERT INTO applications(id, opportunity_id, user_id, stage, created_at, updated_at) VALUES('app-cap', 'job-cap', ?, 'applying', ?, ?)",
            (OTHER, stamp, stamp),
        )
        db.execute(
            "INSERT INTO opportunity_captures(id, user_id, source_type, application_id, created_at) VALUES('cap-1', ?, 'url', 'app-cap', ?)",
            (OTHER, stamp),
        )
        db.execute(
            "INSERT OR IGNORE INTO applications(id, opportunity_id, user_id, stage, created_at, updated_at) VALUES('app-1', 'job-1', ?, 'applied', ?, ?)",
            (USER, stamp, stamp),
        )
        db.commit()
    return platform


# What tag_facets read before the facet scan supplied its keys: the view's own active, non-duplicate, capture-visible rows.
OLD_TAG_FACET_KEYS = (
    "SELECT DISTINCT o.company_sort_key FROM opportunity_read_model o WHERE o.active = 1 AND o.duplicate_of IS NULL"
    " AND (NOT EXISTS (SELECT 1 FROM opportunity_sources s WHERE s.opportunity_id = o.id AND s.source_key = 'manual:capture')"
    " OR EXISTS (SELECT 1 FROM opportunity_sources s JOIN opportunity_captures c ON c.id = s.external_id"
    " JOIN applications a ON a.id = c.application_id WHERE s.opportunity_id = o.id AND s.source_key = 'manual:capture'"
    " AND a.opportunity_id = o.id AND c.user_id = ?))"
)


def reference_facets(conn, user_id):
    """REFERENCE: OpportunityRepository.facets as it was before it shared a scan with the tag facets (frozen).

    The live facets() now delegates to facets_with_company_keys(), so comparing the two would compare the code with
    itself; this copy shares only the unchanged helpers.
    """
    repo = OpportunityRepository(conn, user_id=user_id)
    columns = {"role_types": "role_type", "statuses": "status", "regions": "region", "sources": "source_name", "remote_modes": "remote_mode"}
    collected = {key: set() for key in columns}
    terms = set()
    if user_id is None:
        cursor = conn.execute(
            "SELECT role_type, status, region, source_name, remote_mode, terms_json "
            "FROM opportunity_read_model WHERE active=1 AND duplicate_of IS NULL"
        )
    else:
        cursor = conn.execute(
            *repo._tenant_sql(
                OpportunityFilters(),
                "tenant.role_type, tenant.status, tenant.region, tenant.source_name, tenant.remote_mode, tenant.terms_json",
            )
        )
    for row in cursor:
        for key, column in columns.items():
            value = row[column]
            if value is not None and str(value) != "":
                collected[key].add(str(value))
        terms.update(str(term) for term in _decode_list(row["terms_json"]))
    result = {key: sorted(values, key=_nocase_key) for key, values in collected.items()}
    result["terms"] = sorted(terms, key=str.casefold)
    return result


def reference_tag_facets(conn, user_id):
    """REFERENCE: company_tags.tag_facets as it was before it handed its keys to tag_facets_for_keys (frozen)."""
    rows = conn.execute(
        f"""
        SELECT DISTINCT o.company_sort_key AS company_key
        FROM opportunity_read_model o
        WHERE o.active = 1 AND o.duplicate_of IS NULL AND {capture_visible_sql("o")}
        """,
        [user_id],
    ).fetchall()
    counts = defaultdict(int)
    for tags in tags_for_companies(conn, (row["company_key"] for row in rows), user_id=user_id).values():
        for item in tags:
            counts[item["tag"]] += 1
    return [{"tag": tag, "companies": count} for tag, count in sorted(counts.items())]


class FacetsAndStatsParityTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.platform_path = build_inventory_database(self.root)
        self.conn = connect_product(self.platform_path, read_only=True)
        self.addCleanup(self.conn.close)

    def test_tag_facets_from_the_facet_scan_equal_the_separate_view_scan_for_every_student(self):
        for user_id in (USER, OTHER, "nobody-at-all"):
            with self.subTest(user=user_id):
                repo = OpportunityRepository(self.conn, user_id=user_id)
                facets, company_keys = repo.facets_with_company_keys()
                self.assertEqual(facets, reference_facets(self.conn, user_id))
                self.assertEqual(repo.facets(), reference_facets(self.conn, user_id))
                reference_tags = reference_tag_facets(self.conn, user_id)
                self.assertEqual(tag_facets_for_keys(self.conn, company_keys, user_id=user_id), reference_tags)
                self.assertEqual(tag_facets(self.conn, user_id=user_id), reference_tags)
                old_keys = {row[0] for row in self.conn.execute(OLD_TAG_FACET_KEYS, (user_id,)) if row[0]}
                self.assertEqual(company_keys, old_keys)
        # The private capture's company (and its drone tag) reaches its owner only.
        owner_tags = {entry["tag"]: entry["companies"] for entry in tag_facets(self.conn, user_id=USER)}
        other_tags = {entry["tag"]: entry["companies"] for entry in tag_facets(self.conn, user_id=OTHER)}
        self.assertEqual(other_tags["drone"], owner_tags["drone"] + 1)
        self.assertGreaterEqual(len(owner_tags), 3)

    def test_the_unscoped_repository_reports_no_company_keys_and_unchanged_facets(self):
        repo = OpportunityRepository(self.conn)
        facets, company_keys = repo.facets_with_company_keys()
        self.assertEqual(company_keys, set())
        self.assertEqual(facets, reference_facets(self.conn, None))
        self.assertEqual(repo.facets(), facets)
        self.assertTrue(facets["regions"] or facets["role_types"])

    def test_the_http_facets_and_stats_equal_the_two_step_reference(self):
        app = create_app(
            db_path=self.platform_path, access_token="parity-owner", static_dir=STATIC_DIR, resume_storage=self.root / "r",
            capture_storage=self.root / "c", interview_storage=self.root / "i",
        )
        headers = {"Authorization": "Bearer parity-owner"}
        repo = OpportunityRepository(self.conn, user_id=USER)
        with TestClient(app) as client:
            facets = client.get("/api/v1/facets", headers=headers).json()
            expected = dict(reference_facets(self.conn, USER))
            expected["tags"] = reference_tag_facets(self.conn, USER)
            self.assertEqual(facets, expected)
            self.assertEqual(list(facets), list(expected), "key order is part of the response")
            stats = client.get("/api/v1/stats", headers=headers).json()
            applications = self.conn.execute("SELECT COUNT(*) FROM applications WHERE user_id=?", (USER,)).fetchone()[0]
            self.assertEqual(stats, {**repo.stats(), "applications": int(applications)})
            self.assertGreaterEqual(stats["applications"], 1)


if __name__ == "__main__":
    unittest.main()
