import contextlib
import io
import json
import os
import re
import sqlite3
import sys
import unittest
import unittest.mock
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

import pipeline

FIXTURES = Path(__file__).parent / "fixtures"

# A realistic mechanical-engineering internship body. Length matters for the
# SimHash tests: similarity is measured over 3-token shingles, so on a very
# short body a one-line boilerplate difference moves a large share of the
# shingles and a genuine repost scores below CROSSLIST_THRESHOLD. Real postings
# run several hundred words, which is what these tests need to reflect.
SAMPLE_JD = (
    "Design and analyze mechanical assemblies using SolidWorks and Fusion 360. "
    "Support CNC machining, tolerance stack-up analysis, and prototype builds for "
    "flight hardware. Collaborate with manufacturing engineers on design for "
    "manufacturability reviews and iterate on test feedback. You will own small "
    "subsystems end to end, from concept sketches through detailed drawings, "
    "vendor quoting, and first-article inspection. Expect to spend time on the "
    "shop floor working alongside technicians to debug fixtures and improve "
    "assembly ergonomics. Responsibilities include producing GD&T-compliant "
    "drawings, running finite element studies on load-bearing brackets, and "
    "documenting test procedures for repeatability. You will present findings at "
    "weekly design reviews and incorporate feedback from systems and quality "
    "engineering. Qualifications: currently pursuing a bachelor's degree in "
    "mechanical or aerospace engineering, coursework in statics, dynamics, and "
    "materials science, and hands-on experience with machine shop tooling. "
    "Familiarity with Python or MATLAB for data reduction is a plus. This is a "
    "full-time summer position based on site with housing assistance available."
)


def load_fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class PipelineTests(unittest.TestCase):
    def test_strip_html(self):
        self.assertEqual(pipeline.strip_html("<p>Build &amp; test</p>"), "Build & test")

    def test_role_classification(self):
        self.assertEqual(pipeline.classify_role("Mechanical Engineering Intern", ""), "internship")
        self.assertEqual(pipeline.classify_role("Spring Co-op", ""), "co-op")
        self.assertEqual(pipeline.classify_role("Undergraduate Research Assistant", ""), "research")
        self.assertEqual(
            pipeline.classify_role("New Graduate Mechanical Engineer", "Our interns do great work."),
            "early_career",
        )

    def test_canonical_url_removes_tracking(self):
        value = pipeline.canonical_url(
            "https://example.com/jobs/1/?utm_source=test&gh_src=abc&keep=yes"
        )
        self.assertEqual(value, "https://example.com/jobs/1?keep=yes")

    def test_canonical_url_removes_linkedin_tracking(self):
        value = pipeline.canonical_url(
            "https://www.linkedin.com/comm/jobs/view/4012345678/"
            "?trackingId=abc123&refId=xyz&trk=eml-jobs&keep=yes"
        )
        self.assertEqual(
            value, "https://www.linkedin.com/comm/jobs/view/4012345678?keep=yes"
        )

    def test_score_is_transparent_and_rewards_fit(self):
        profile = {
            "preferred_role_types": ["internship"],
            "degree_keywords": ["mechanical engineering", "mechanical"],
            "interest_keywords": ["manufacturing", "CAD"],
            "skills": ["SolidWorks"],
            "preferred_locations": ["Austin"],
            "remote_ok": True,
            "max_years_experience": 1,
        }
        job = {
            "title": "Mechanical Engineering Intern",
            "description": "Use SolidWorks and CAD for manufacturing prototypes.",
            "role_type": "internship",
            "location": "Austin, TX",
            "posted_at": None,
        }
        score, reasons = pipeline.score_job(job, profile)
        self.assertGreaterEqual(score, 80)
        self.assertTrue(any("skills" in reason for reason in reasons))
        self.assertTrue(any("location" in reason for reason in reasons))

    def test_region_matching_requires_state_for_ambiguous_cities(self):
        regions = json.loads(
            (Path(__file__).resolve().parent / "fixtures" / "profile_student.json").read_text(
                encoding="utf-8"
            )
        )["regions"]

        def matched(location):
            hit = pipeline.match_region(location, regions)
            return hit["region"]["name"] if hit else None

        self.assertEqual(matched("Austin, TX"), "Austin")
        self.assertEqual(matched("Round Rock, Texas"), "Austin")
        self.assertEqual(matched("Sunnyvale, CA"), "Bay Area")
        self.assertEqual(matched("San Francisco Bay Area"), "Bay Area")
        # Same city names, wrong state: these must not read as Bay Area.
        self.assertIsNone(matched("Newark, NJ"))
        self.assertIsNone(matched("Dublin, Ireland"))
        self.assertIsNone(matched("Richmond, VA"))
        # Outside both regions entirely.
        self.assertIsNone(matched("Seattle, WA"))
        self.assertIsNone(matched(""))

    def test_out_of_region_is_penalised_and_remote_survives(self):
        profile = {
            "preferred_role_types": [],
            "degree_keywords": [],
            "interest_keywords": [],
            "skills": [],
            "remote_ok": True,
            "max_years_experience": 1,
            "out_of_region_penalty": 40,
            "regions": [
                {
                    "name": "Austin",
                    "radius": "close",
                    "bonus": 15,
                    "state_markers": ["tx", "texas"],
                    "places": ["austin"],
                }
            ],
        }
        # Scored high enough that the out-of-region penalty lands in full rather
        # than being absorbed by the 0..100 clamp in score_job.
        base = {
            "title": "Mechanical Engineering Intern",
            "description": "Hands-on internship for a mechanical engineering student.",
            "role_type": "internship",
            "posted_at": None,
        }
        profile["degree_keywords"] = ["mechanical engineering"]
        in_region, in_reasons = pipeline.score_job({**base, "location": "Austin, TX"}, profile)
        remote, _ = pipeline.score_job({**base, "location": "Remote - US"}, profile)
        outside, out_reasons = pipeline.score_job({**base, "location": "Seattle, WA"}, profile)
        unknown, _ = pipeline.score_job({**base, "location": ""}, profile)

        self.assertGreater(in_region, remote)
        self.assertGreater(remote, unknown)
        self.assertEqual(outside, unknown - 40)
        self.assertEqual(in_region, unknown + 15)
        self.assertTrue(any("close radius" in reason for reason in in_reasons))
        self.assertTrue(any("outside target regions" in reason for reason in out_reasons))

    def test_region_label_buckets_for_display(self):
        profile = {
            "regions": [
                {"name": "Austin", "state_markers": ["tx"], "places": ["austin"]},
            ]
        }
        self.assertEqual(pipeline.region_label("Austin, TX", profile), "Austin")
        self.assertEqual(pipeline.region_label("Remote - US", profile), "Remote")
        self.assertEqual(pipeline.region_label("Seattle, WA", profile), "Other")
        self.assertEqual(pipeline.region_label("3 Locations", profile), "Unknown")
        self.assertEqual(pipeline.region_label("", profile), "Unknown")

    def test_placeholder_location_is_not_penalised(self):
        profile = {
            "preferred_role_types": [],
            "degree_keywords": [],
            "interest_keywords": [],
            "skills": [],
            "remote_ok": True,
            "max_years_experience": 1,
            "regions": [
                {"name": "Austin", "bonus": 15, "state_markers": ["tx"], "places": ["austin"]}
            ],
        }
        base = {"title": "Engineer", "description": "", "role_type": "internship", "posted_at": None}
        # "2 Locations" is a Workday multi-site placeholder, not a real elsewhere.
        placeholder, reasons = pipeline.score_job({**base, "location": "2 Locations"}, profile)
        blank, _ = pipeline.score_job({**base, "location": ""}, profile)
        self.assertEqual(placeholder, blank)
        self.assertFalse(any("outside target regions" in reason for reason in reasons))

    def test_seniority_penalty(self):
        profile = {
            "preferred_role_types": [],
            "degree_keywords": [],
            "interest_keywords": [],
            "skills": [],
            "preferred_locations": [],
            "remote_ok": False,
            "max_years_experience": 1,
        }
        senior = {
            "title": "Senior Mechanical Engineer",
            "description": "Requires 8+ years.",
            "role_type": "other",
            "location": "",
            "posted_at": None,
        }
        score, reasons = pipeline.score_job(senior, profile)
        self.assertEqual(score, 0)
        self.assertTrue(any("seniority" in reason for reason in reasons))

    def test_short_skill_terms_do_not_match_inside_words(self):
        self.assertEqual(pipeline.term_hits("Mechanical design", ["C"]), [])
        self.assertEqual(pipeline.term_hits("C and CAD experience", ["C", "CAD"]), ["C", "CAD"])

    def _dedup_conn(self, rows):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.row_factory = sqlite3.Row
        conn.execute(
            """
            CREATE TABLE jobs (
                id TEXT, fingerprint TEXT, active INTEGER, duplicate_of TEXT,
                status TEXT, source_key TEXT, description TEXT,
                company TEXT, title TEXT, location TEXT,
                content_fingerprint TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.executemany(
            "INSERT INTO jobs (id, fingerprint, active, duplicate_of, status, source_key,"
            " description, company, title, location) VALUES (?, ?, 1, NULL, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        return conn

    def test_deduplication_preserves_tracked_copy(self):
        conn = self._dedup_conn(
            [
                (
                    "ats",
                    "same",
                    "discovered",
                    "greenhouse:company",
                    "long official description",
                    "Acme",
                    "Intern",
                    "Austin, TX",
                ),
                ("manual", "same", "applied", "manual:csv", "short", "Acme", "Intern", "Austin, TX"),
            ]
        )
        pipeline.deduplicate(conn)
        ats = conn.execute("SELECT duplicate_of FROM jobs WHERE id='ats'").fetchone()[0]
        manual = conn.execute("SELECT duplicate_of FROM jobs WHERE id='manual'").fetchone()[0]
        self.assertEqual(ats, "manual")
        self.assertIsNone(manual)

    def test_deduplication_links_same_role_across_differing_location_formats(self):
        """The real cross-channel case: one ATS row, one LinkedIn row.

        Greenhouse packs two cities into one field; LinkedIn abbreviates a
        single one. Pass 1 cannot match them, so without pass 2 the same job
        shows up twice on the dashboard.
        """
        conn = self._dedup_conn(
            [
                (
                    "ats",
                    "fp-ats",
                    "discovered",
                    "greenhouse:neuralink",
                    "x" * 400,
                    "Neuralink",
                    "Mechanical Engineering Intern, Brain Interfaces",
                    "Austin, Texas, United States; South San Francisco, California, United States",
                ),
                (
                    "li",
                    "fp-li",
                    "discovered",
                    "agent:linkedin",
                    "x" * 300,
                    "Neuralink",
                    "Mechanical Engineering Intern, Brain Interfaces",
                    "Austin, TX",
                ),
                # Same company and title, genuinely different city -> must stay separate.
                (
                    "other-city",
                    "fp-other",
                    "discovered",
                    "agent:linkedin",
                    "x" * 300,
                    "Neuralink",
                    "Mechanical Engineering Intern, Robotics",
                    "Boston, MA",
                ),
                (
                    "other-city-ats",
                    "fp-other-ats",
                    "discovered",
                    "greenhouse:neuralink",
                    "x" * 400,
                    "Neuralink",
                    "Mechanical Engineering Intern, Robotics",
                    "South San Francisco, California, United States",
                ),
            ]
        )
        pipeline.deduplicate(conn)
        result = dict(conn.execute("SELECT id, duplicate_of FROM jobs").fetchall())
        # The ATS row wins canonical (real source, longer description).
        self.assertIsNone(result["ats"])
        self.assertEqual(result["li"], "ats")
        # Non-overlapping cities are left as distinct postings.
        self.assertIsNone(result["other-city"])
        self.assertIsNone(result["other-city-ats"])

    def test_locations_compatible_treats_blank_as_unknown_not_conflicting(self):
        self.assertTrue(pipeline.locations_compatible("Austin, TX", ""))
        self.assertTrue(
            pipeline.locations_compatible(
                "Austin, Texas, United States; South San Francisco, California, United States",
                "Austin, TX",
            )
        )
        self.assertFalse(pipeline.locations_compatible("Austin, TX", "Boston, MA"))
        self.assertEqual(pipeline.location_cities("Austin, TX; Boston, MA"), {"austin", "boston"})

    def test_display_reasons_excludes_base_and_caps_length(self):
        reasons = ["35 base", "r1", "r2", "r3", "r4", "r5", "r6"]
        self.assertEqual(pipeline.display_reasons(reasons), ["r1", "r2", "r3", "r4", "r5"])
        self.assertEqual(pipeline.display_reasons(reasons, limit=2), ["r1", "r2"])

    def _sample_dashboard_jobs(self, title="Mechanical Engineering Intern"):
        return [
            {
                "id": "job1",
                "title": title,
                "company": "Acme Robotics",
                "location": "Austin, TX",
                "role_type": "internship",
                "status": "discovered",
                "score": 88,
                "reasons": ["role type match", "skills: SolidWorks"],
                "source_name": "greenhouse:acme",
                "url": "https://example.com/jobs/1",
                "first_seen_at": "2026-07-20T00:00:00+00:00",
                "last_seen_at": "2026-07-23T00:00:00+00:00",
                "posted_at": "2026-07-18T00:00:00+00:00",
                "freshness": "1d since checked",
            }
        ]

    def test_build_dashboard_html_embeds_valid_json(self):
        jobs = self._sample_dashboard_jobs()
        doc = pipeline.build_dashboard_html(jobs, "2026-07-24T00:00:00+00:00")
        match = re.search(
            r'<script id="job-data" type="application/json">(.*?)</script>', doc, re.S
        )
        self.assertIsNotNone(match)
        embedded = json.loads(match.group(1).replace("<\\/", "</"))
        self.assertEqual(embedded, jobs)

    def test_build_dashboard_html_escapes_script_injection(self):
        malicious_title = "Intern</script><script>alert(1)</script>"
        jobs = self._sample_dashboard_jobs(title=malicious_title)
        doc = pipeline.build_dashboard_html(jobs, "2026-07-24T00:00:00+00:00")
        self.assertNotIn("</script><script>", doc)
        match = re.search(
            r'<script id="job-data" type="application/json">(.*?)</script>', doc, re.S
        )
        self.assertIsNotNone(match)
        embedded = json.loads(match.group(1).replace("<\\/", "</"))
        self.assertEqual(embedded[0]["title"], malicious_title)

    def test_ashby_jobs_filters_and_normalizes(self):
        with unittest.mock.patch.object(
            pipeline, "request_json", return_value=load_fixture("ashby_board.json")
        ):
            jobs = pipeline.ashby_jobs({"company": "Acme", "board": "acme"}, ["intern"])
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["external_id"], "abc123")
        self.assertEqual(jobs[0]["title"], "Mechanical Engineering Intern")
        self.assertEqual(jobs[0]["description"], "Build and test hardware prototypes.")

    def test_smartrecruiters_jobs_paginates_and_fetches_detail(self):
        listing = load_fixture("smartrecruiters_postings.json")
        detail = load_fixture("smartrecruiters_posting_detail.json")
        with unittest.mock.patch.object(pipeline, "request_json", side_effect=[listing, detail]):
            jobs = pipeline.smartrecruiters_jobs(
                {"company": "Acme", "company_id": "Acme"}, ["intern"]
            )
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["external_id"], "987")
        self.assertEqual(jobs[0]["location"], "Plano, TX, us")
        self.assertEqual(jobs[0]["description"], "Design and test mechanical assemblies.")

    def test_workday_jobs_searches_per_term_and_stops_when_total_reached(self):
        page = load_fixture("workday_jobs_page1.json")
        with unittest.mock.patch.object(pipeline, "request_json_post", side_effect=[page, page]) as mock_post:
            jobs = pipeline.workday_jobs(
                {"company": "NVIDIA", "tenant": "nvidia", "datacenter": "wd5", "site": "NVIDIAExternalCareerSite"},
                ["intern", "co-op"],
            )
        # One search call per discovery term (2 terms), each stopping after its single page.
        self.assertEqual(mock_post.call_count, 2)
        searched_terms = [call.args[1]["searchText"] for call in mock_post.call_args_list]
        self.assertEqual(searched_terms, ["intern", "co-op"])
        # The same posting appears in both term searches but is de-duped by externalPath.
        self.assertEqual(len(jobs), 1)
        self.assertEqual(
            jobs[0]["url"],
            "https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite"
            "/job/Santa-Clara-CA/Mechanical-Engineering-Intern_JR1234567",
        )
        self.assertIsNone(jobs[0]["posted_at"])

    def test_discovery_terms_match_on_word_boundaries(self):
        terms = ["intern", "co-op", "student", "undergraduate", "extern"]
        for title in (
            "Mechanical Engineering Intern",
            "Summer Internship 2027",
            "Engineering Interns",
            "Manufacturing Co-Op",
            "Student Trainee (Engineering)",
            "Undergraduate Research Assistant",
            "Externship Program",
        ):
            self.assertTrue(pipeline.is_discovery_candidate(title, terms), title)
        # "intern" inside "Internal" is the failure that matters: the VA posts
        # hundreds of internal-medicine roles to USAJOBS, and substring matching
        # pulled every one of them in as an internship.
        for title in (
            "Physician (Internal Medicine)",
            "Chiropractor (Internal to North Texas VA Health Care System)",
            "SUPERVISORY MECHANICAL INTERNAL SHOP MANAGER",
            "Internal Medicine Internist",
            "Internally Posted Analyst",
        ):
            self.assertFalse(pipeline.is_discovery_candidate(title, terms), title)

    def test_adzuna_jobs_requires_both_credentials(self):
        # Adzuna issues app_id and app_key as a pair and rejects a request
        # carrying only one, so either half missing has to fail before the call.
        for env in ({}, {"ADZUNA_APP_ID": "id"}, {"ADZUNA_APP_KEY": "key"}):
            with unittest.mock.patch.dict("os.environ", env, clear=True):
                with self.assertRaises(ValueError):
                    pipeline.adzuna_jobs({"company": "Adzuna"}, ["intern"])

    def test_adzuna_jobs_normalizes_results(self):
        env = {"ADZUNA_APP_ID": "id", "ADZUNA_APP_KEY": "key"}
        page = load_fixture("adzuna_search.json")
        with unittest.mock.patch.dict("os.environ", env, clear=True):
            with unittest.mock.patch.object(pipeline, "request_json", return_value=page):
                jobs = pipeline.adzuna_jobs(
                    {"company": "Adzuna", "what": "engineering intern"}, ["intern", "co-op"]
                )
        # The facilities manager fails the title filter; the other two survive.
        self.assertEqual([job["external_id"] for job in jobs], ["4411", "4413"])
        first = jobs[0]
        self.assertEqual(first["title"], "Mechanical Engineering Intern")
        self.assertEqual(first["company"], "Lone Star Machine Works")
        self.assertEqual(first["location"], "Austin, Travis County")
        self.assertEqual(first["url"], "https://www.adzuna.com/land/ad/4411")
        self.assertEqual(first["posted_at"], "2026-07-20T09:14:00Z")
        # Adzuna embeds match highlighting and entities in both title and
        # description, so both are stripped rather than stored as markup.
        self.assertNotIn("<strong>", first["title"])
        self.assertNotIn("<b>", first["description"])
        self.assertIn("fixture design", first["description"])
        self.assertIn("…", first["description"])
        # Adzuna redacts the employer on some listings; the source label is the
        # only honest fallback, since inventing a company name would be worse.
        self.assertEqual(jobs[1]["company"], "Adzuna")

    def test_adzuna_jobs_builds_query_per_spec_and_stops_on_short_page(self):
        env = {"ADZUNA_APP_ID": "id", "ADZUNA_APP_KEY": "key"}
        page = load_fixture("adzuna_search.json")
        source = {
            "company": "Adzuna",
            "country": "us",
            "queries": [
                {
                    "what": "mechanical engineering intern",
                    "where": "Austin, Texas",
                    "distance_km": 60,
                    "posted_within_days": 45,
                },
                {"what": "engineering intern", "what_exclude": "sales", "where": "San Jose"},
            ],
        }
        with unittest.mock.patch.dict("os.environ", env, clear=True):
            with unittest.mock.patch.object(
                pipeline, "request_json", return_value=page
            ) as mock_get:
                jobs = pipeline.adzuna_jobs(source, ["intern", "co-op"])
        # A page shorter than results_per_page is the last page, so each query
        # costs exactly one request rather than walking to the page cap.
        self.assertEqual(mock_get.call_count, 2)
        urls = [call.args[0] for call in mock_get.call_args_list]
        queries = [
            dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query)) for url in urls
        ]
        self.assertTrue(urls[0].startswith("https://api.adzuna.com/v1/api/jobs/us/search/1?"))
        self.assertEqual(queries[0]["what"], "mechanical engineering intern")
        self.assertEqual(queries[0]["where"], "Austin, Texas")
        self.assertEqual(queries[0]["distance"], "60")
        self.assertEqual(queries[0]["max_days_old"], "45")
        # Query 2 must not inherit query 1's location or radius.
        self.assertEqual(queries[1]["what_exclude"], "sales")
        self.assertEqual(queries[1]["where"], "San Jose")
        self.assertNotIn("distance", queries[1])
        self.assertNotIn("max_days_old", queries[1])
        # Both queries returned the same fixture; each posting is stored once.
        self.assertEqual([job["external_id"] for job in jobs], ["4411", "4413"])

    def test_usajobs_jobs_requires_api_key(self):
        with unittest.mock.patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(ValueError):
                pipeline.usajobs_jobs({"company": "USAJOBS"}, ["intern"])

    def test_usajobs_jobs_requires_contact_email(self):
        # USAJOBS rejects a request whose User-Agent is not the registered
        # address, so a key on its own is not enough to make a live call.
        with unittest.mock.patch.dict("os.environ", {"USAJOBS_API_KEY": "k"}, clear=True):
            with self.assertRaises(ValueError):
                pipeline.usajobs_jobs({"company": "USAJOBS"}, ["intern"])

    def test_usajobs_jobs_normalizes_results(self):
        env = {"USAJOBS_API_KEY": "test-key", "USAJOBS_CONTACT_EMAIL": "me@example.com"}
        page1 = load_fixture("usajobs_search.json")
        page2 = load_fixture("usajobs_search_page2.json")
        with unittest.mock.patch.dict("os.environ", env, clear=True):
            with unittest.mock.patch.object(
                pipeline, "_http_json", side_effect=[page1, page2]
            ):
                jobs = pipeline.usajobs_jobs({"company": "USAJOBS"}, ["intern", "student trainee"])
        # The budget analyst on page 1 fails the title filter; both engineering
        # rows survive, so paging is what makes the page-2 row reachable at all.
        self.assertEqual([job["external_id"] for job in jobs], ["111", "333"])
        first = jobs[0]
        # Announcement id, not PositionID: it is what the posting URL is keyed on.
        self.assertEqual(first["company"], "NASA Johnson Space Center")
        self.assertEqual(first["location"], "Houston, Texas; Cleveland, Ohio")
        self.assertEqual(first["url"], "https://www.usajobs.gov/job/111")
        self.assertEqual(first["posted_at"], "2026-06-01T00:00:00Z")
        # Fields=Full detail beats the qualification blurb, and HTML is stripped.
        self.assertTrue(first["description"].startswith("Support thermal and structural design"))
        self.assertNotIn("<p>", first["description"])
        # MajorDuties is documented as a string but comes back as a list of
        # strings; both shapes have to flatten rather than raise mid-fetch.
        self.assertIn("Run CAD models and test fixtures. Document results.", first["description"])

    def test_usajobs_description_tolerates_field_shapes(self):
        self.assertEqual(pipeline._usajobs_text("plain"), "plain")
        self.assertEqual(pipeline._usajobs_text(["a", "b"]), "a b")
        self.assertEqual(pipeline._usajobs_text({"Content": "boxed"}), "boxed")
        self.assertEqual(pipeline._usajobs_text([{"Content": "a"}, "b"]), "a b")
        self.assertEqual(pipeline._usajobs_text(None), "")
        self.assertEqual(pipeline._usajobs_text(17), "")

    def test_usajobs_jobs_runs_each_query_with_its_own_filters(self):
        # The API ANDs its filters, and HiringPath combined with Keyword returns
        # almost nothing, so a hiring-path sweep has to issue an unkeyworded
        # request rather than inheriting the other query's keywords.
        env = {"USAJOBS_API_KEY": "test-key", "USAJOBS_CONTACT_EMAIL": "me@example.com"}
        source = {
            "company": "USAJOBS",
            "fields": "Full",
            "queries": [
                {"hiring_paths": ["student", "graduates"]},
                {"keywords": ["student trainee"], "location_names": ["Austin, Texas"], "radius": 50},
            ],
        }
        pages = [
            load_fixture("usajobs_search.json"),
            load_fixture("usajobs_search_page2.json"),
            load_fixture("usajobs_search.json"),
            load_fixture("usajobs_search_page2.json"),
        ]
        with unittest.mock.patch.dict("os.environ", env, clear=True):
            with unittest.mock.patch.object(pipeline, "_http_json", side_effect=pages) as mock_get:
                jobs = pipeline.usajobs_jobs(source, ["intern", "student trainee"])
        queries = [
            dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(call.args[0]).query))
            for call in mock_get.call_args_list
        ]
        self.assertEqual(mock_get.call_count, 4)
        # Query 1: hiring paths, no Keyword at all.
        self.assertEqual(queries[0]["HiringPath"], "student;graduates")
        self.assertNotIn("Keyword", queries[0])
        self.assertNotIn("Keyword", queries[1])
        # Query 2: keyword and location, and none of query 1's hiring paths.
        self.assertEqual(queries[2]["Keyword"], "student trainee")
        self.assertEqual(queries[2]["LocationName"], "Austin, Texas")
        self.assertEqual(queries[2]["Radius"], "50")
        self.assertNotIn("HiringPath", queries[2])
        # `fields` on the source carries into every query.
        self.assertEqual({query["Fields"] for query in queries}, {"Full"})
        # The same announcements came back from both queries; each is stored once.
        self.assertEqual([job["external_id"] for job in jobs], ["111", "333"])

    def test_usajobs_jobs_walks_pages_and_dedupes_across_keywords(self):
        env = {"USAJOBS_API_KEY": "test-key", "USAJOBS_CONTACT_EMAIL": "me@example.com"}
        source = {
            "company": "USAJOBS",
            "keywords": ["mechanical engineering", "student trainee"],
            "hiring_paths": ["student", "graduates"],
            "job_category_codes": ["0899", "0830"],
            "posted_within_days": 30,
        }
        pages = [
            load_fixture("usajobs_search.json"),
            load_fixture("usajobs_search_page2.json"),
            load_fixture("usajobs_search.json"),
            load_fixture("usajobs_search_page2.json"),
        ]
        with unittest.mock.patch.dict("os.environ", env, clear=True):
            with unittest.mock.patch.object(pipeline, "_http_json", side_effect=pages) as mock_get:
                jobs = pipeline.usajobs_jobs(source, ["intern", "student trainee"])
        # Two keywords x two pages each.
        self.assertEqual(mock_get.call_count, 4)
        queries = [
            dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(call.args[0]).query))
            for call in mock_get.call_args_list
        ]
        self.assertEqual([query["Page"] for query in queries], ["1", "2", "1", "2"])
        self.assertEqual(
            [query["Keyword"] for query in queries],
            ["mechanical engineering", "mechanical engineering", "student trainee", "student trainee"],
        )
        # Multi-value filters use the API's semicolon separator.
        self.assertEqual(queries[0]["HiringPath"], "student;graduates")
        self.assertEqual(queries[0]["JobCategoryCode"], "0899;0830")
        self.assertEqual(queries[0]["DatePosted"], "30")
        self.assertEqual(queries[0]["Fields"], "Full")
        self.assertEqual(queries[0]["ResultsPerPage"], str(pipeline.USAJOBS_RESULTS_PER_PAGE))
        # The same announcement matched both keywords but is stored once.
        self.assertEqual([job["external_id"] for job in jobs], ["111", "333"])

    def test_usajobs_jobs_stops_on_empty_page(self):
        # A page count the API reports but cannot fill must not spin forever.
        env = {"USAJOBS_API_KEY": "test-key", "USAJOBS_CONTACT_EMAIL": "me@example.com"}
        empty = {"SearchResult": {"SearchResultItems": [], "UserArea": {"NumberOfPages": "9"}}}
        with unittest.mock.patch.dict("os.environ", env, clear=True):
            with unittest.mock.patch.object(pipeline, "_http_json", return_value=empty) as mock_get:
                jobs = pipeline.usajobs_jobs({"company": "USAJOBS"}, ["intern"])
        self.assertEqual(mock_get.call_count, 1)
        self.assertEqual(jobs, [])

    def test_usajobs_location_truncates_long_duty_station_lists(self):
        descriptor = {
            "PositionLocation": [
                {"LocationName": f"City {index}, Texas"} for index in range(10)
            ]
        }
        location = pipeline._usajobs_location(descriptor)
        self.assertTrue(location.endswith("(+4 more)"))
        self.assertIn("City 0, Texas", location)
        self.assertNotIn("City 6, Texas", location)

    def test_load_env_file_fills_gaps_without_overriding_environment(self):
        with TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text(
                "# comment\n"
                "\n"
                "USAJOBS_API_KEY=from-file\n"
                'USAJOBS_CONTACT_EMAIL="quoted@example.com"\n'
                "MALFORMED\n",
                encoding="utf-8",
            )
            with unittest.mock.patch.dict(
                "os.environ", {"USAJOBS_API_KEY": "from-shell"}, clear=True
            ):
                pipeline.load_env_file(env_path)
                # A real environment variable stays authoritative.
                self.assertEqual(os.environ["USAJOBS_API_KEY"], "from-shell")
                self.assertEqual(os.environ["USAJOBS_CONTACT_EMAIL"], "quoted@example.com")
                self.assertNotIn("MALFORMED", os.environ)

    def test_load_env_file_missing_file_is_not_an_error(self):
        with TemporaryDirectory() as tmp:
            pipeline.load_env_file(Path(tmp) / "absent.env")

    def test_import_emails_upserts_and_extracts_job_id(self):
        with TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "pipeline.db"
            with unittest.mock.patch.object(pipeline, "DB_PATH", db_path):
                conn = pipeline.connect()
                count = pipeline.import_emails(conn, FIXTURES / "linkedin_emails_sample.json")
                self.assertEqual(count, 1)
                row = conn.execute(
                    "SELECT source_key, external_id, posted_at, company, title FROM jobs"
                ).fetchone()
                self.assertEqual(row["source_key"], "manual:linkedin-email")
                self.assertEqual(row["external_id"], "4012345678")
                self.assertEqual(row["posted_at"], "2026-07-20T00:00:00+00:00")
                self.assertEqual(row["company"], "Acme Robotics")
                conn.close()

    def test_import_discovered_normalizes_and_partitions_by_channel(self):
        with TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "pipeline.db"
            with unittest.mock.patch.object(pipeline, "DB_PATH", db_path):
                conn = pipeline.connect()
                count = pipeline.import_discovered(
                    conn, FIXTURES / "discovered_jobs_sample.json"
                )
                # 7 records in, 4 skipped: unknown channel, badge-only title,
                # relative URL off LinkedIn, and a missing title.
                self.assertEqual(count, 3)

                rows = {
                    row["company"]: row
                    for row in conn.execute(
                        "SELECT company, title, url, source_key, source_name, external_id,"
                        " posted_at FROM jobs"
                    )
                }
                self.assertEqual(set(rows), {"Apptronik", "Redwood Materials", "Firefly Aerospace"})

                linkedin = rows["Apptronik"]
                # Badge chrome stripped, and the site-relative href absolutized
                # before canonical_url() drops the trailing slash.
                self.assertEqual(linkedin["title"], "Mechanical Design Intern")
                self.assertEqual(
                    linkedin["url"], "https://www.linkedin.com/jobs/view/4432396869"
                )
                # The numeric job id is reused so a posting found by search and by
                # job-alert email fingerprints to the same opportunity.
                self.assertEqual(linkedin["external_id"], "4432396869")
                self.assertEqual(linkedin["source_key"], "agent:linkedin")
                self.assertEqual(linkedin["posted_at"], "2026-07-21T00:00:00+00:00")

                # Each channel lands under its own source_key.
                self.assertEqual(rows["Redwood Materials"]["source_key"], "agent:exa")
                self.assertEqual(rows["Firefly Aerospace"]["source_key"], "agent:github")
                self.assertEqual(
                    rows["Redwood Materials"]["posted_at"], "2026-07-18T00:00:00+00:00"
                )
                conn.close()

    def test_parse_linkedin_job_posting_stops_before_other_companies_jobs(self):
        """Guards against the real failure mode in get_job_details output.

        The blob ends with a "More jobs" carousel of other employers' postings.
        If that survives into the description, an unrelated company's keywords
        score this posting.
        """
        blob = load_fixture("linkedin_job_detail.json")["sections"]["job_posting"]
        parsed = pipeline.parse_linkedin_job_posting(blob)

        self.assertEqual(parsed["company"], "Neuralink")
        self.assertEqual(parsed["title"], "Mechanical Engineering Intern, Brain Interfaces")
        self.assertEqual(parsed["location"], "Austin, TX")

        description = parsed["description"]
        # The genuine posting body survives...
        self.assertIn("Brain Interfaces Mechanical Engineering Team", description)
        self.assertIn("Mechanical Engineering or a related field", description)
        # ...and every trailing-chrome section is gone, including the other
        # employers advertised in the carousel.
        for leaked in (
            "Try Premium",
            "Benefits found in job post",
            "About the company",
            "More jobs",
            "Amazon",
            "Zipline",
            "Parker Hannifin",
        ):
            self.assertNotIn(leaked, description, f"chrome leaked into description: {leaked}")
        self.assertLess(len(description), len(blob) / 2)

    def test_parse_linkedin_job_posting_abstains_on_unrecognised_shape(self):
        parsed = pipeline.parse_linkedin_job_posting("Some Company\n\nSome Title\n\nOn a Tuesday")
        # "On a Tuesday" is not a place, so location stays blank rather than
        # inheriting the out-of-region penalty on a bad guess.
        self.assertEqual(parsed["location"], "")
        self.assertEqual(parsed["description"], "")
        self.assertEqual(pipeline.parse_linkedin_job_posting("")["company"], "")

    def test_import_discovered_derives_fields_from_raw_posting(self):
        with TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "pipeline.db"
            payload = Path(tmp) / "discovered.json"
            blob = load_fixture("linkedin_job_detail.json")["sections"]["job_posting"]
            payload.write_text(
                json.dumps(
                    [
                        {
                            "channel": "linkedin",
                            "url": "/jobs/view/4416596272/",
                            "raw_posting": blob,
                        },
                        {
                            "channel": "linkedin",
                            "url": "/jobs/view/4416596273/",
                            "company": "Explicit Co",
                            "location": "Cedar Park, TX",
                            "raw_posting": blob,
                        },
                    ]
                ),
                encoding="utf-8",
            )
            with unittest.mock.patch.object(pipeline, "DB_PATH", db_path):
                conn = pipeline.connect()
                with unittest.mock.patch.object(pipeline, "ROOT", Path(tmp)):
                    count = pipeline.import_discovered(conn, payload)
                self.assertEqual(count, 2)
                rows = {
                    row["external_id"]: row
                    for row in conn.execute(
                        "SELECT external_id, company, title, location, description,"
                        " role_type FROM jobs"
                    )
                }
                derived = rows["4416596272"]
                self.assertEqual(derived["company"], "Neuralink")
                self.assertEqual(derived["location"], "Austin, TX")
                self.assertIn("brain-computer interface", derived["description"])
                # A real description means role classification now has something
                # to work with instead of falling back to the title alone.
                self.assertEqual(derived["role_type"], "internship")

                # Explicit fields override the parsed blob.
                self.assertEqual(rows["4416596273"]["company"], "Explicit Co")
                self.assertEqual(rows["4416596273"]["location"], "Cedar Park, TX")
                conn.close()

    def test_import_discovered_channels_do_not_deactivate_each_other(self):
        """upsert_jobs deactivates rows absent from a batch, scoped to one source_key.

        Partitioning by channel is what keeps a LinkedIn-only import from
        retiring everything Exa found on a previous run.
        """
        with TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "pipeline.db"
            payload = Path(tmp) / "second.json"
            with unittest.mock.patch.object(pipeline, "DB_PATH", db_path):
                conn = pipeline.connect()
                pipeline.import_discovered(conn, FIXTURES / "discovered_jobs_sample.json")
                payload.write_text(
                    json.dumps(
                        [
                            {
                                "channel": "linkedin",
                                "title": "Different Intern Role",
                                "company": "Other Corp",
                                "url": "/jobs/view/555/",
                            }
                        ]
                    ),
                    encoding="utf-8",
                )
                with unittest.mock.patch.object(pipeline, "ROOT", Path(tmp)):
                    pipeline.import_discovered(conn, payload)

                active = dict(
                    conn.execute("SELECT company, active FROM jobs").fetchall()
                )
                # The stale LinkedIn row retires...
                self.assertEqual(active["Apptronik"], 0)
                self.assertEqual(active["Other Corp"], 1)
                # ...while the other channels are untouched.
                self.assertEqual(active["Redwood Materials"], 1)
                self.assertEqual(active["Firefly Aerospace"], 1)
                conn.close()

    def test_enrich_fills_thin_descriptions_and_respects_richer_text(self):
        with TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "pipeline.db"
            payload = Path(tmp) / "enrichment.json"
            with unittest.mock.patch.object(pipeline, "DB_PATH", db_path):
                conn = pipeline.connect()
                existing = "x" * (pipeline.THIN_DESCRIPTION_CHARS + 50)
                pipeline.upsert_jobs(
                    conn,
                    "agent:linkedin",
                    "LinkedIn",
                    [
                        {
                            "external_id": "1",
                            "company": "Thin Co",
                            "title": "Intern",
                            "location": "",
                            "url": "https://example.invalid/thin",
                            "description": "",
                        },
                        {
                            "external_id": "2",
                            "company": "Rich Co",
                            "title": "Intern",
                            "location": "Austin, TX",
                            "url": "https://example.invalid/rich",
                            "description": existing,
                        },
                    ],
                )
                payload.write_text(
                    json.dumps(
                        [
                            {
                                "url": "https://example.invalid/thin",
                                "description": "<p>Design fixtures in SolidWorks &amp; run GD&amp;T reviews.</p>",
                                "location": "Austin, TX",
                            },
                            {
                                "url": "https://example.invalid/rich",
                                "description": "should not overwrite richer ATS text",
                            },
                            {
                                "url": "https://example.invalid/missing",
                                "description": "no such posting",
                            },
                        ]
                    ),
                    encoding="utf-8",
                )
                with unittest.mock.patch.object(pipeline, "ROOT", Path(tmp)):
                    updated = pipeline.enrich_descriptions(conn, payload)
                self.assertEqual(updated, 1)

                rows = {
                    row["company"]: row
                    for row in conn.execute("SELECT company, description, location FROM jobs")
                }
                # HTML is stripped and entities decoded on the way in.
                self.assertEqual(
                    rows["Thin Co"]["description"],
                    "Design fixtures in SolidWorks & run GD&T reviews.",
                )
                # An empty location is backfilled...
                self.assertEqual(rows["Thin Co"]["location"], "Austin, TX")
                # ...and richer existing text survives.
                self.assertEqual(rows["Rich Co"]["description"], existing)

                with unittest.mock.patch.object(pipeline, "ROOT", Path(tmp)):
                    forced = pipeline.enrich_descriptions(conn, payload, force=True)
                self.assertEqual(forced, 2)
                self.assertEqual(
                    conn.execute(
                        "SELECT description FROM jobs WHERE company='Rich Co'"
                    ).fetchone()["description"],
                    "should not overwrite richer ATS text",
                )
                conn.close()

    def _record(self, **overrides):
        record = {
            "external_id": "1",
            "company": "Acme",
            "title": "Mechanical Engineering Intern",
            "location": "",
            "url": "https://example.com/jobs/1",
            "description": "",
            "posted_at": None,
        }
        record.update(overrides)
        return record

    def test_repeat_import_keeps_enriched_description(self):
        """A later thin sweep must not undo `enrich` on a posting it rediscovers."""
        enriched = "y" * (pipeline.THIN_DESCRIPTION_CHARS + 50)
        with TemporaryDirectory() as tmp:
            with unittest.mock.patch.object(pipeline, "DB_PATH", Path(tmp) / "pipeline.db"):
                conn = pipeline.connect()
                pipeline.upsert_jobs(conn, "agent:linkedin", "LinkedIn", [self._record()])
                conn.execute(
                    "UPDATE jobs SET description=?, location=?", (enriched, "Austin, TX")
                )

                # The same posting comes back from a search sweep with no detail.
                pipeline.upsert_jobs(conn, "agent:linkedin", "LinkedIn", [self._record()])

                row = conn.execute("SELECT description, location FROM jobs").fetchone()
                self.assertEqual(row["description"], enriched)
                self.assertEqual(row["location"], "Austin, TX")

                # A substantive description from the source still wins.
                fresh = "z" * (pipeline.THIN_DESCRIPTION_CHARS + 10)
                pipeline.upsert_jobs(
                    conn, "agent:linkedin", "LinkedIn", [self._record(description=fresh)]
                )
                self.assertEqual(
                    conn.execute("SELECT description FROM jobs").fetchone()["description"], fresh
                )
                conn.close()

    def test_dedupe_resolves_each_location_cluster(self):
        """A company/title group spanning cities needs one canonical per city.

        Measuring the whole group against a single winner leaves the duplicates
        in every other city visible.
        """
        with TemporaryDirectory() as tmp:
            with unittest.mock.patch.object(pipeline, "DB_PATH", Path(tmp) / "pipeline.db"):
                conn = pipeline.connect()
                # The two Austin rows are formatted differently on purpose, so
                # pass 1 cannot group them by fingerprint and the clustering in
                # pass 2 is what has to resolve them.
                for index, location in enumerate(
                    ["Boston, MA", "Austin, TX", "Austin, Texas, United States"], start=1
                ):
                    pipeline.upsert_jobs(
                        conn,
                        f"agent:linkedin{index}",
                        "LinkedIn",
                        [
                            self._record(
                                external_id=str(index),
                                location=location,
                                url=f"https://example.com/jobs/{index}",
                            )
                        ],
                    )
                # The Boston row is furthest along, so it wins the group outright.
                conn.execute(
                    "UPDATE jobs SET status='applied' WHERE location='Boston, MA'"
                )
                pipeline.deduplicate(conn)

                austin = conn.execute(
                    "SELECT id, duplicate_of FROM jobs WHERE location LIKE 'Austin%'"
                ).fetchall()
                # Exactly one Austin row survives as its own canonical.
                hidden = [row for row in austin if row["duplicate_of"] is not None]
                self.assertEqual(len(hidden), 1)
                # ...and it is hidden behind the other Austin row, not behind Boston.
                self.assertIn(hidden[0]["duplicate_of"], {row["id"] for row in austin})
                # Boston stays visible; it contradicts Austin.
                self.assertIsNone(
                    conn.execute(
                        "SELECT duplicate_of FROM jobs WHERE location='Boston, MA'"
                    ).fetchone()["duplicate_of"]
                )
                conn.close()

    def test_import_discovered_retires_channel_that_returned_nothing(self):
        """A channel going quiet is exactly when retirement matters."""
        with TemporaryDirectory() as tmp:
            payload = Path(tmp) / "discovered.json"
            with unittest.mock.patch.object(pipeline, "DB_PATH", Path(tmp) / "pipeline.db"):
                conn = pipeline.connect()
                pipeline.import_discovered(conn, FIXTURES / "discovered_jobs_sample.json")
                self.assertEqual(
                    conn.execute(
                        "SELECT COUNT(*) FROM jobs WHERE source_key='agent:exa' AND active=1"
                    ).fetchone()[0],
                    1,
                )

                payload.write_text(
                    json.dumps({"searched_channels": ["exa"], "postings": []}),
                    encoding="utf-8",
                )
                pipeline.import_discovered(conn, payload)

                self.assertEqual(
                    conn.execute(
                        "SELECT COUNT(*) FROM jobs WHERE source_key='agent:exa' AND active=1"
                    ).fetchone()[0],
                    0,
                )
                # Channels the session did not search are left alone.
                self.assertEqual(
                    conn.execute(
                        "SELECT COUNT(*) FROM jobs WHERE source_key='agent:linkedin' AND active=1"
                    ).fetchone()[0],
                    1,
                )
                conn.close()

    def test_enrich_reruns_dedupe_after_location_backfill(self):
        """A blank location matches anything, so backfilling one can unhide a row."""
        with TemporaryDirectory() as tmp:
            payload = Path(tmp) / "enrichment.json"
            with unittest.mock.patch.object(pipeline, "DB_PATH", Path(tmp) / "pipeline.db"):
                conn = pipeline.connect()
                pipeline.upsert_jobs(
                    conn,
                    "agent:exa",
                    "Exa",
                    [self._record(location="Austin, TX", description="a" * 300)],
                )
                pipeline.upsert_jobs(
                    conn,
                    "agent:linkedin",
                    "LinkedIn",
                    [self._record(external_id="2", url="https://example.com/jobs/2")],
                )
                blank = conn.execute(
                    "SELECT id, duplicate_of FROM jobs WHERE source_key='agent:linkedin'"
                ).fetchone()
                # The blank-location row starts out hidden behind the Austin one.
                self.assertIsNotNone(blank["duplicate_of"])

                payload.write_text(
                    json.dumps(
                        [
                            {
                                "id": blank["id"],
                                "location": "Boston, MA",
                                "description": "b" * 300,
                            }
                        ]
                    ),
                    encoding="utf-8",
                )
                pipeline.enrich_descriptions(conn, payload)

                row = conn.execute(
                    "SELECT location, duplicate_of, fingerprint FROM jobs WHERE id=?",
                    (blank["id"],),
                ).fetchone()
                self.assertEqual(row["location"], "Boston, MA")
                # Now that it names a conflicting city it is no longer hidden.
                self.assertIsNone(row["duplicate_of"])
                self.assertEqual(
                    row["fingerprint"],
                    pipeline.fingerprint("Acme", "Mechanical Engineering Intern", "Boston, MA"),
                )
                conn.close()

    def test_display_path_falls_back_for_files_outside_the_project(self):
        with TemporaryDirectory() as tmp:
            outside = Path(tmp).resolve() / "discovered.json"
            self.assertEqual(pipeline.display_path(outside), str(outside))
        # Built with Path rather than a literal "data/x.json": the relative
        # branch renders with the platform separator, so a hardcoded forward
        # slash asserts a POSIX detail the function never promised.
        self.assertEqual(
            pipeline.display_path(pipeline.ROOT / "data" / "x.json"),
            str(Path("data") / "x.json"),
        )

    def test_import_discovered_reports_paths_outside_the_project(self):
        """Formatting the success line must not fail a commit that already happened."""
        with TemporaryDirectory() as tmp:
            payload = Path(tmp).resolve() / "discovered.json"
            payload.write_text(
                json.dumps(
                    [
                        {
                            "channel": "linkedin",
                            "company": "Outside Corp",
                            "title": "Intern",
                            "url": "https://example.com/jobs/9",
                        }
                    ]
                ),
                encoding="utf-8",
            )
            with unittest.mock.patch.object(pipeline, "DB_PATH", Path(tmp) / "pipeline.db"):
                conn = pipeline.connect()
                self.assertEqual(pipeline.import_discovered(conn, payload), 1)
                conn.close()

    # --- repost flagging ----------------------------------------------------

    def test_role_key_ignores_cohort_markers(self):
        self.assertEqual(
            pipeline.role_key("Mechanical Engineering Intern (Summer 2027)"),
            pipeline.role_key("Mechanical Engineering Intern [Fall 2026]"),
        )
        self.assertEqual(
            pipeline.role_key("Design Intern 2027"), pipeline.role_key("Design Intern")
        )
        # Genuinely different roles stay different.
        self.assertNotEqual(
            pipeline.role_key("Mechanical Intern"), pipeline.role_key("Software Intern")
        )

    def _repost_conn(self, rows):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE jobs (id TEXT, company TEXT, title TEXT, url TEXT,"
            " first_seen_at TEXT, active INTEGER)"
        )
        conn.executemany("INSERT INTO jobs VALUES (?,?,?,?,?,?)", rows)
        return conn

    def test_repost_flags_a_role_relisted_at_a_new_url(self):
        recent = pipeline.now_iso()
        older = (
            pipeline.datetime.now(pipeline.timezone.utc) - pipeline.timedelta(days=30)
        ).isoformat()
        conn = self._repost_conn(
            [
                ("old", "Acme", "Mechanical Intern (Summer 2026)", "https://x/1", older, 0),
                ("new", "Acme", "Mechanical Intern (Summer 2027)", "https://x/2", recent, 1),
            ]
        )
        flags = pipeline.repost_flags(conn)
        self.assertIn("new", flags)
        self.assertNotIn("old", flags)
        self.assertEqual(flags["new"][0], 2)

    def test_repost_ignores_two_terms_advertised_at_once(self):
        # Both live: a company advertising the same internship for two terms is
        # normal, and flagging it would be noise.
        recent = pipeline.now_iso()
        conn = self._repost_conn(
            [
                ("a", "Acme", "Mechanical Intern (Summer 2027)", "https://x/1", recent, 1),
                ("b", "Acme", "Mechanical Intern (Fall 2027)", "https://x/2", recent, 1),
            ]
        )
        self.assertEqual(pipeline.repost_flags(conn), {})

    def test_repost_ignores_the_same_posting_going_inactive_and_back(self):
        older = (
            pipeline.datetime.now(pipeline.timezone.utc) - pipeline.timedelta(days=10)
        ).isoformat()
        # Same URL retired and relisted is one posting, not a repost.
        conn = self._repost_conn(
            [
                ("old", "Acme", "Mechanical Intern", "https://x/1", older, 0),
                ("new", "Acme", "Mechanical Intern", "https://x/1", pipeline.now_iso(), 1),
            ]
        )
        self.assertEqual(pipeline.repost_flags(conn), {})

    def test_repost_respects_the_window(self):
        ancient = (
            pipeline.datetime.now(pipeline.timezone.utc) - pipeline.timedelta(days=200)
        ).isoformat()
        conn = self._repost_conn(
            [
                ("old", "Acme", "Mechanical Intern", "https://x/1", ancient, 0),
                ("new", "Acme", "Mechanical Intern", "https://x/2", pipeline.now_iso(), 1),
            ]
        )
        self.assertEqual(pipeline.repost_flags(conn), {})

    def test_repost_flag_is_reported_without_changing_the_score(self):
        with TemporaryDirectory() as tmp:
            with unittest.mock.patch.object(pipeline, "DB_PATH", Path(tmp) / "pipeline.db"):
                conn = pipeline.connect()
                record = {
                    "external_id": "1",
                    "company": "Acme",
                    "title": "Mechanical Engineering Intern",
                    "location": "Austin, TX",
                    "url": "https://x/1",
                    "description": "SolidWorks and CAD work.",
                }
                pipeline.upsert_jobs(conn, "greenhouse:acme", "Acme", [record])
                profile = json.loads(
                    (
                        Path(__file__).resolve().parent / "fixtures" / "profile_student.json"
                    ).read_text(encoding="utf-8")
                )
                pipeline.score_all(conn, profile)
                baseline = conn.execute("SELECT score FROM jobs").fetchone()["score"]

                # Retire it, then relist the same role at a new URL.
                conn.execute(
                    "UPDATE jobs SET active=0, first_seen_at=?",
                    ((pipeline.datetime.now(pipeline.timezone.utc)
                      - pipeline.timedelta(days=20)).isoformat(),),
                )
                pipeline.upsert_jobs(
                    conn, "greenhouse:acme", "Acme", [{**record, "external_id": "2", "url": "https://x/2"}]
                )
                pipeline.score_all(conn, profile)
                row = conn.execute(
                    "SELECT score, score_explanation FROM jobs WHERE external_id='2'"
                ).fetchone()
                reasons = json.loads(row["score_explanation"])
                self.assertTrue(any("re-listed req" in reason for reason in reasons))
                # Informational only: the flag must not move the score.
                self.assertEqual(row["score"], baseline)
                conn.close()

    # --- application artifacts ----------------------------------------------

    RESUME = {
        "name": "Test Student",
        "contact": {"email": "t@example.com", "location": "Austin, TX"},
        "education": [
            {
                "school": "The University of Texas at Austin",
                "degree": "B.S. Mechanical Engineering",
                "graduation": "May 2030",
                "coursework": ["Statics", "Materials Science"],
            }
        ],
        "projects": [
            {"name": "Quadcopter build", "context": "Personal", "bullets": ["Tuned a Pixhawk stack"]},
            {"name": "Bookshelf", "context": "Personal", "bullets": ["Cut joinery by hand"]},
        ],
        "skills": {"CAD": ["Fusion 360", "SolidWorks"], "Software": ["Python"]},
    }

    def _job_row(self, title, description, company="Acme Robotics"):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE jobs (id TEXT, company TEXT, title TEXT, location TEXT,"
            " description TEXT, url TEXT)"
        )
        conn.execute(
            "INSERT INTO jobs VALUES ('abc123def456', ?, ?, 'Austin, TX', ?, 'https://x/1')",
            (company, title, description),
        )
        return conn.execute("SELECT * FROM jobs").fetchone()

    def test_term_matches_job_requires_whole_words(self):
        words = pipeline._job_keywords("We use Cadence for routing and layout.", "Engineer")
        # "CAD" must not match inside "cadence" -- substring matching would bold
        # much of the skills list on almost any posting.
        self.assertFalse(pipeline.term_matches_job("CAD", words))
        self.assertTrue(pipeline.term_matches_job("Cadence", words))
        # Multi-word terms need every word present.
        self.assertFalse(pipeline.term_matches_job("Fusion 360", words))
        self.assertTrue(
            pipeline.term_matches_job("routing layout", words),
            "word order should not matter, only presence",
        )
        self.assertFalse(pipeline.term_matches_job("", words))

    def test_resume_emphasises_only_matching_skills(self):
        job = self._job_row(
            "Mechanical Intern",
            "You will model parts in SolidWorks and iterate quickly on prototypes.",
        )
        markup = pipeline.build_resume_html(self.RESUME, job)
        self.assertIn('<span class="match">SolidWorks</span>', markup)
        # Present on the resume, absent from the posting: listed, never bolded.
        self.assertIn("Fusion 360", markup)
        self.assertNotIn('<span class="match">Fusion 360</span>', markup)
        # Emphasis must not invent: nothing outside resume.json appears.
        self.assertNotIn("CNC", markup)

    def test_resume_orders_matching_skills_first(self):
        job = self._job_row("Intern", "Model parts in SolidWorks daily.")
        markup = pipeline.build_resume_html(self.RESUME, job)
        skills = re.search(r"<h2>Skills</h2>(.*?)</section>", markup, re.S).group(1)
        self.assertLess(skills.index("SolidWorks"), skills.index("Fusion 360"))

    def test_resume_without_a_job_emphasises_nothing(self):
        markup = pipeline.build_resume_html(self.RESUME, None)
        self.assertNotIn('class="match"', markup)
        self.assertIn("SolidWorks", markup)

    def test_resume_omits_empty_sections_rather_than_padding(self):
        sparse = {"name": "Test Student", "contact": {}, "education": [], "skills": {}}
        markup = pipeline.build_resume_html(sparse, None)
        for heading in ("Experience", "Projects", "Skills", "Awards", "Summary"):
            self.assertNotIn(f"<h2>{heading}</h2>", markup)

    def test_resume_escapes_injected_markup(self):
        hostile = {
            "name": "<script>alert(1)</script>",
            "contact": {"email": "a@b.c"},
            "skills": {"CAD": ["<img onerror=alert(1)>"]},
        }
        markup = pipeline.build_resume_html(hostile, None)
        self.assertNotIn("<script>", markup)
        self.assertNotIn("<img onerror", markup)
        self.assertIn("&lt;script&gt;", markup)

    def test_resume_escapes_hostile_posting_text(self):
        # A posting is untrusted input: it reaches the resume through the
        # tailoring path and must never be able to inject markup.
        job = self._job_row("Intern", "<script>alert(1)</script> SolidWorks", company="<b>Evil</b>")
        markup = pipeline.build_resume_html(self.RESUME, job)
        self.assertNotIn("<script>", markup)

    def test_cover_letter_marks_everything_it_cannot_know(self):
        job = self._job_row(
            "Mechanical Intern",
            "Experience with SolidWorks required. Currently pursuing a B.S. in engineering.",
        )
        markup = pipeline.build_cover_letter_html(self.RESUME, job)
        self.assertIn("Acme Robotics", markup)
        self.assertIn("Mechanical Intern", markup)
        # Anything the tool cannot know is a visible TODO, never invented prose.
        self.assertIn('class="todo"', markup)
        self.assertIn("SolidWorks", markup)

    def test_cover_letter_escapes_hostile_posting_fields(self):
        # Company, title, and location all come from an untrusted posting and
        # all reach the letter's address block.
        job = self._job_row(
            "<script>alert(1)</script> Intern", "SolidWorks work.", company="<b>Evil</b> Corp"
        )
        markup = pipeline.build_cover_letter_html(self.RESUME, job)
        self.assertNotIn("<script>", markup)
        self.assertNotIn("<b>Evil</b>", markup)
        self.assertIn("&lt;b&gt;Evil&lt;/b&gt; Corp", markup)

    def test_cover_letter_uses_one_location_in_the_address_block(self):
        job = self._job_row("Intern", "SolidWorks work.")
        job = dict(job)
        job["location"] = "Austin, Texas, United States; South San Francisco, California"
        markup = pipeline.build_cover_letter_html(self.RESUME, job)
        self.assertIn("Austin, Texas, United States", markup)
        self.assertNotIn("South San Francisco", markup)

    def test_cover_letter_says_so_when_nothing_matches(self):
        job = self._job_row("Marketing Intern", "Run email campaigns and social ads.")
        markup = pipeline.build_cover_letter_html(self.RESUME, job)
        self.assertIn("No skill in your resume.json matched", markup)

    def test_job_requirement_lines_picks_requirement_shaped_sentences(self):
        lines = pipeline.job_requirement_lines(
            "About us: we build robots. Experience with SolidWorks and GD&T is required. "
            "Currently pursuing a degree in mechanical engineering. We offer free lunch."
        )
        self.assertTrue(any("SolidWorks" in line for line in lines))
        self.assertFalse(any("free lunch" in line for line in lines))

    def test_load_resume_points_at_the_example_when_absent(self):
        with TemporaryDirectory() as tmp:
            with self.assertRaises(SystemExit) as caught:
                pipeline.load_resume(Path(tmp) / "resume.json")
            self.assertIn("resume.example.json", str(caught.exception))

    def test_resume_example_renders(self):
        """The shipped template must produce a working document as-is."""
        example = json.loads(
            (Path(__file__).resolve().parents[1] / "config" / "resume.example.json").read_text(
                encoding="utf-8"
            )
        )
        markup = pipeline.build_resume_html(example, None)
        self.assertIn("<h2>Education</h2>", markup)
        self.assertIn("<h2>Skills</h2>", markup)
        # Placeholder entries are blank, so those sections drop out rather than
        # rendering an empty heading.
        self.assertNotIn("<h2>Experience</h2>", markup)

    def test_html_to_pdf_degrades_without_playwright(self):
        # Setting a module to None in sys.modules makes `from ... import` raise
        # ImportError, which is exactly the state on a machine that never
        # installed the optional dependency.
        with unittest.mock.patch.dict(sys.modules, {"playwright.sync_api": None}):
            self.assertFalse(pipeline.html_to_pdf(Path("x.html"), Path("x.pdf")))

    # --- console encoding ---------------------------------------------------

    def test_printed_output_survives_a_cp1252_console(self):
        """Every literal this tool prints must encode on a stock Windows console.

        cp1252 is the default there and has no mapping for U+2192, so a single
        arrow in a print() crashed `update` with a UnicodeEncodeError before it
        could confirm the status change.
        """
        source = (Path(__file__).resolve().parents[1] / "pipeline.py").read_text(
            encoding="utf-8"
        )
        # The smart-quote tables in normalize_for_match are data, never printed:
        # they exist precisely to fold these characters out of fetched pages.
        allowed = set("‘’ʼ′“”″´")
        offenders = {}
        for number, line in enumerate(source.splitlines(), 1):
            if "print(" not in line:
                continue
            for char in line:
                if char in allowed:
                    continue
                try:
                    char.encode("cp1252")
                except UnicodeEncodeError:
                    offenders.setdefault(f"U+{ord(char):04X}", []).append(number)
        self.assertEqual(offenders, {}, f"unencodable characters in print(): {offenders}")

    # --- ATS board discovery ------------------------------------------------

    def test_slug_candidates_are_ordered_and_url_safe(self):
        candidates = pipeline.slug_candidates("Firefly Aerospace")
        self.assertEqual(candidates[0], "fireflyaerospace")
        self.assertIn("firefly-aerospace", candidates)
        # Ashby boards are case-sensitive, so the CamelCase form is tried too.
        self.assertIn("FireflyAerospace", candidates)
        # The bare first word is a last resort, not a first guess.
        self.assertEqual(candidates[-1], "firefly")
        for candidate in candidates:
            self.assertRegex(candidate, pipeline.DISCOVERY_SLUG_RE.pattern)

    def test_slug_candidates_strip_unsafe_characters(self):
        self.assertEqual(pipeline.slug_candidates(""), [])
        self.assertEqual(pipeline.slug_candidates("!!!"), [])
        # Nothing that could alter the shape of the request URL survives.
        for candidate in pipeline.slug_candidates("Acme/../Robotics?x=1"):
            self.assertRegex(candidate, pipeline.DISCOVERY_SLUG_RE.pattern)

    def test_discover_ats_flags_a_board_belonging_to_another_company(self):
        """The impostor case config/sources.json warns about, in miniature.

        greenhouse/archer really is Archer Veterinary Clinic, so a board that
        returns JSON must never be taken as proof of whose board it is.
        """
        sources = {"ats_sources": [], "discovery_title_terms": ["intern"]}
        probe = unittest.mock.Mock(
            return_value={
                "board_name": "Archer Veterinary Clinic",
                "titles": ["Veterinary Intern"],
                "field": "token",
            }
        )
        with unittest.mock.patch.dict(pipeline.DISCOVERY_VENDORS, {"greenhouse": probe}):
            results = pipeline.discover_ats(
                ["Archer Aviation"], sources, ["intern"], vendors=["greenhouse"]
            )
        self.assertEqual(results[0]["status"], "resolved")
        self.assertEqual(results[0]["identity"], "review")

    def test_discover_ats_confirms_matching_board_name(self):
        sources = {"ats_sources": [], "discovery_title_terms": ["intern"]}
        probe = unittest.mock.Mock(
            return_value={
                "board_name": "Firefly Aerospace, Inc.",
                "titles": ["Mechanical Engineering Intern", "Senior Propulsion Engineer"],
                "field": "token",
            }
        )
        with unittest.mock.patch.dict(pipeline.DISCOVERY_VENDORS, {"greenhouse": probe}):
            results = pipeline.discover_ats(
                ["Firefly Aerospace"], sources, ["intern"], vendors=["greenhouse"]
            )
        # A corporate suffix is not a different company.
        self.assertEqual(results[0]["identity"], "confirmed")
        self.assertEqual(results[0]["matching"], ["Mechanical Engineering Intern"])
        self.assertEqual(results[0]["entry"]["kind"], "greenhouse")
        self.assertEqual(results[0]["entry"]["token"], "fireflyaerospace")

    def test_discover_ats_marks_ashby_and_lever_unverified(self):
        sources = {"ats_sources": [], "discovery_title_terms": ["intern"]}
        probe = unittest.mock.Mock(
            return_value={"board_name": None, "titles": ["Design Intern"], "field": "board"}
        )
        with unittest.mock.patch.dict(pipeline.DISCOVERY_VENDORS, {"ashby": probe}):
            results = pipeline.discover_ats(["Base Power"], sources, ["intern"], vendors=["ashby"])
        # These APIs expose no company name, so identity cannot be settled here.
        self.assertEqual(results[0]["identity"], "unverified")

    def test_discover_ats_skips_configured_and_empty_boards(self):
        sources = {
            "ats_sources": [{"kind": "greenhouse", "company": "SpaceX", "token": "spacex"}],
            "discovery_title_terms": ["intern"],
        }
        probe = unittest.mock.Mock()
        with unittest.mock.patch.dict(pipeline.DISCOVERY_VENDORS, {"greenhouse": probe}):
            results = pipeline.discover_ats(["SpaceX"], sources, ["intern"], vendors=["greenhouse"])
        self.assertEqual(results[0]["status"], "already-configured")
        probe.assert_not_called()

        # A board that exists but lists nothing is indistinguishable from a
        # parked slug and is not worth an entry.
        empty = unittest.mock.Mock(
            return_value={"board_name": "Ghost Co", "titles": [], "field": "token"}
        )
        with unittest.mock.patch.dict(pipeline.DISCOVERY_VENDORS, {"greenhouse": empty}):
            results = pipeline.discover_ats(
                ["Ghost Co"], {"ats_sources": []}, ["intern"], vendors=["greenhouse"]
            )
        self.assertEqual(results[0]["status"], "unresolved")

    def test_report_discovery_preview_writes_nothing(self):
        sources = {"ats_sources": [], "discovery_title_terms": ["intern"]}
        probe = unittest.mock.Mock(
            return_value={
                "board_name": "Acme Robotics",
                "titles": ["Mechanical Intern"],
                "field": "token",
            }
        )
        writer = unittest.mock.Mock()
        with unittest.mock.patch.dict(pipeline.DISCOVERY_VENDORS, {"greenhouse": probe}):
            with unittest.mock.patch.object(pipeline, "_write_discovered_sources", writer):
                pipeline.report_discovery(["Acme Robotics"], sources, write=False)
        writer.assert_not_called()

    def test_report_discovery_write_excludes_unconfirmed_identities(self):
        sources = {"ats_sources": [], "discovery_title_terms": ["intern"]}
        probe = unittest.mock.Mock(
            return_value={
                "board_name": "Archer Veterinary Clinic",
                "titles": ["Veterinary Intern"],
                "field": "token",
            }
        )
        writer = unittest.mock.Mock()
        with unittest.mock.patch.dict(pipeline.DISCOVERY_VENDORS, {"greenhouse": probe}):
            with unittest.mock.patch.object(pipeline, "_write_discovered_sources", writer):
                pipeline.report_discovery(["Archer Aviation"], sources, write=True)
        # A mismatched board name must never be written, even with --write.
        writer.assert_not_called()

    def test_write_discovered_sources_defaults_to_the_local_overlay(self):
        with TemporaryDirectory() as tmp:
            local = Path(tmp) / "sources.local.json"
            with unittest.mock.patch.object(pipeline, "SOURCES_LOCAL_PATH", local):
                written = pipeline._write_discovered_sources(
                    [{"kind": "ashby", "company": "Base Power", "board": "base-power"}]
                )
            self.assertEqual(written, local)
            self.assertEqual(
                json.loads(local.read_text(encoding="utf-8"))["ats_sources"][0]["board"],
                "base-power",
            )

    def test_merge_sources_layers_a_student_overlay_on_the_catalog(self):
        base = {
            "stale_after_days": 7,
            "discovery_title_terms": ["intern"],
            "ats_sources": [
                {"kind": "greenhouse", "company": "SpaceX", "token": "spacex"},
                {"kind": "greenhouse", "company": "Anduril", "token": "anduril"},
                {"kind": "usajobs", "company": "USAJOBS", "id": "federal", "enabled": False},
            ],
            "agent_discovery": {"linkedin": {"keywords": [], "locations": [], "enabled": True}},
            "manual_check_sources": [{"name": "NSF REU Directory"}],
        }
        local = {
            "stale_after_days": 3,
            "enabled_sources": ["usajobs:federal"],
            "disabled_sources": ["anduril"],
            "ats_sources": [
                {"kind": "greenhouse", "company": "SpaceX", "token": "spacex", "note": "retuned"},
                {"kind": "lever", "company": "Zipline", "site": "flyzipline"},
            ],
            "agent_discovery": {"linkedin": {"keywords": ["chemical engineering intern"]}},
            "manual_check_sources": [{"name": "NSF REU Directory"}, {"name": "My school portal"}],
        }
        merged = pipeline.merge_sources(base, local)
        by_key = {pipeline._source_merge_key(source): source for source in merged["ats_sources"]}
        self.assertEqual(merged["stale_after_days"], 3)
        self.assertEqual(len(merged["ats_sources"]), 4)
        self.assertEqual(by_key["greenhouse:spacex"]["note"], "retuned")
        self.assertFalse(by_key["greenhouse:anduril"]["enabled"])
        self.assertTrue(by_key["usajobs:federal"]["enabled"])
        self.assertIn("lever:flyzipline", by_key)
        linkedin = merged["agent_discovery"]["linkedin"]
        self.assertEqual(linkedin["keywords"], ["chemical engineering intern"])
        self.assertTrue(linkedin["enabled"])
        self.assertEqual(
            [item["name"] for item in merged["manual_check_sources"]],
            ["NSF REU Directory", "My school portal"],
        )
        # The base catalog itself is left untouched.
        self.assertNotIn("enabled", base["ats_sources"][1])

    def test_merge_sources_can_drop_the_shared_catalog(self):
        base = {"ats_sources": [{"kind": "greenhouse", "company": "SpaceX", "token": "spacex"}]}
        local = {
            "include_base_catalog": False,
            "ats_sources": [{"kind": "lever", "company": "Zipline", "site": "flyzipline"}],
        }
        merged = pipeline.merge_sources(base, local)
        self.assertEqual([source["company"] for source in merged["ats_sources"]], ["Zipline"])

    def test_load_sources_without_an_overlay_is_the_catalog(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp) / "sources.json"
            base.write_text(json.dumps({"ats_sources": []}), encoding="utf-8")
            self.assertEqual(
                pipeline.load_sources(base, Path(tmp) / "sources.local.json"), {"ats_sources": []}
            )

    def test_doctor_does_not_ask_for_locations_when_regions_are_set(self):
        profile = json.loads(
            (Path(__file__).resolve().parent / "fixtures" / "profile_student.json").read_text(encoding="utf-8")
        )
        profile["preferred_locations"] = []
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            pipeline.doctor(profile, {"ats_sources": []})
        self.assertNotIn("preferred_locations", output.getvalue())
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            pipeline.doctor({**profile, "regions": []}, {"ats_sources": []})
        self.assertIn("preferred_locations", output.getvalue())

    def test_missing_profile_points_at_setup(self):
        with TemporaryDirectory() as tmp:
            with self.assertRaises(SystemExit) as raised:
                pipeline.load_profile(Path(tmp) / "profile.json")
        self.assertIn("opportunity_app.setup init", str(raised.exception))

    def test_tracked_templates_are_valid_scoring_inputs(self):
        config = Path(__file__).resolve().parents[1] / "config"
        profile = json.loads((config / "profile.example.json").read_text(encoding="utf-8"))
        job = {
            "title": "Engineering Intern",
            "description": "",
            "role_type": "internship",
            "location": "Anywhere, USA",
            "posted_at": None,
        }
        score, reasons = pipeline.score_job(job, profile)
        self.assertTrue(0 <= score <= 100)
        overlay = json.loads((config / "sources.local.example.json").read_text(encoding="utf-8"))
        base = json.loads((config / "sources.json").read_text(encoding="utf-8"))
        merged = pipeline.merge_sources(base, overlay)
        self.assertGreaterEqual(len(merged["ats_sources"]), len(base["ats_sources"]))

    def test_write_discovered_sources_preserves_the_rest_of_the_file(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "sources.json"
            original = {
                "stale_after_days": 7,
                "discovery_title_terms": ["intern"],
                "ats_sources": [{"kind": "greenhouse", "company": "SpaceX", "token": "spacex"}],
                "rejected_sources": {"note": "keep me"},
            }
            path.write_text(json.dumps(original, indent=2) + "\n", encoding="utf-8")
            pipeline._write_discovered_sources(
                [{"kind": "ashby", "company": "Base Power", "board": "base-power"}], path
            )
            written = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(len(written["ats_sources"]), 2)
            self.assertEqual(written["rejected_sources"], {"note": "keep me"})
            self.assertEqual(written["stale_after_days"], 7)
            # No stray temp file left behind.
            self.assertEqual(list(Path(tmp).iterdir()), [path])

    # --- description fingerprinting -----------------------------------------

    def test_fingerprint_text_skips_unusable_bodies(self):
        self.assertEqual(pipeline.fingerprint_text(""), "")
        self.assertEqual(pipeline.fingerprint_text("Too short to be useful."), "")
        # Long enough by character count but a single token once normalized: an
        # unspaced body would otherwise hash to all zeros and then match every
        # other degenerate body at 1.0.
        self.assertEqual(pipeline.fingerprint_text("一" * 400), "")

    def test_fingerprint_text_is_stable_and_shaped(self):
        value = pipeline.fingerprint_text(SAMPLE_JD)
        self.assertRegex(value, r"^[0-9a-f]{16}$")
        self.assertEqual(value, pipeline.fingerprint_text(SAMPLE_JD))
        # HTML wrapping and entity noise must not change the fingerprint.
        self.assertEqual(value, pipeline.fingerprint_text(f"<div><p>{SAMPLE_JD}</p></div>"))

    def test_fingerprint_similarity_separates_reposts_from_unrelated_roles(self):
        # An aggregator re-post: same requirements text, boilerplate tail added.
        repost = SAMPLE_JD + " We are an equal opportunity employer."
        unrelated = (
            "Join our marketing team to run lifecycle email campaigns and paid social "
            "experiments. Own reporting dashboards, partner with brand designers on "
            "creative testing, and present growth results to leadership each month. "
            "Experience with attribution modelling and audience segmentation is "
            "preferred, along with strong written communication and a bias toward "
            "shipping. You will coordinate with sales on lead handoff, maintain the "
            "content calendar, and run monthly retrospectives on campaign performance."
        )
        self.assertGreaterEqual(
            pipeline.fingerprint_similarity(
                pipeline.fingerprint_text(SAMPLE_JD), pipeline.fingerprint_text(repost)
            ),
            pipeline.CROSSLIST_THRESHOLD,
        )
        self.assertLess(
            pipeline.fingerprint_similarity(
                pipeline.fingerprint_text(SAMPLE_JD), pipeline.fingerprint_text(unrelated)
            ),
            pipeline.CROSSLIST_THRESHOLD,
        )
        self.assertEqual(pipeline.fingerprint_similarity("", "abc"), 0.0)

    def test_dedupe_links_aggregator_repost_by_description(self):
        body = SAMPLE_JD
        with TemporaryDirectory() as tmp:
            with unittest.mock.patch.object(pipeline, "DB_PATH", Path(tmp) / "pipeline.db"):
                conn = pipeline.connect()
                pipeline.upsert_jobs(
                    conn,
                    "greenhouse:acme",
                    "Acme Robotics",
                    [
                        {
                            "external_id": "1",
                            "company": "Acme Robotics",
                            "title": "Mechanical Engineering Intern",
                            "location": "Austin, TX",
                            "url": "https://boards.example.com/acme/1",
                            "description": body,
                        }
                    ],
                )
                # Same posting via an aggregator: company restyled, title
                # rewritten, location reformatted. Passes 1 and 2 both key on
                # the company name, so only the body can reconcile these.
                pipeline.upsert_jobs(
                    conn,
                    "adzuna:austin-bay-mechanical",
                    "Adzuna",
                    [
                        {
                            "external_id": "9",
                            "company": "Acme Robotics Inc.",
                            "title": "Intern - Mechanical Engineering (Summer)",
                            "location": "Austin, Texas, United States",
                            "url": "https://adzuna.example.com/jobs/9",
                            "description": body + " We are an equal opportunity employer.",
                        }
                    ],
                )
                rows = {
                    row["source_key"]: row
                    for row in conn.execute(
                        "SELECT source_key, id, duplicate_of FROM jobs"
                    )
                }
                aggregated = rows["adzuna:austin-bay-mechanical"]
                direct = rows["greenhouse:acme"]
                # The employer's own board wins as canonical (longer body loses
                # to the real-source preference only when status ties, and here
                # the direct listing is the fuller record of the two).
                self.assertIsNotNone(aggregated["duplicate_of"] or direct["duplicate_of"])
                linked = {aggregated["duplicate_of"], direct["duplicate_of"]} - {None}
                self.assertEqual(len(linked), 1)
                conn.close()

    def test_dedupe_keeps_same_source_near_identical_reqs_separate(self):
        body = SAMPLE_JD
        with TemporaryDirectory() as tmp:
            with unittest.mock.patch.object(pipeline, "DB_PATH", Path(tmp) / "pipeline.db"):
                conn = pipeline.connect()
                # One employer posting two reqs off the same JD template is two
                # real opportunities, not a duplicate.
                pipeline.upsert_jobs(
                    conn,
                    "greenhouse:acme",
                    "Acme Robotics",
                    [
                        {
                            "external_id": "1",
                            "company": "Acme Robotics",
                            "title": "Mechanical Engineering Intern I",
                            "location": "Austin, TX",
                            "url": "https://boards.example.com/acme/1",
                            "description": body,
                        },
                        {
                            "external_id": "2",
                            "company": "Acme Robotics",
                            "title": "Mechanical Engineering Intern II",
                            "location": "Austin, TX",
                            "url": "https://boards.example.com/acme/2",
                            "description": body + " Second requisition.",
                        },
                    ],
                )
                duplicates = [
                    row["duplicate_of"]
                    for row in conn.execute("SELECT duplicate_of FROM jobs")
                ]
                self.assertEqual(duplicates, [None, None])
                conn.close()

    def test_content_fingerprint_follows_the_description_that_is_kept(self):
        """A thin re-import must not blank the fingerprint of a rich row.

        `upsert_jobs` keeps the richer of the stored and incoming descriptions,
        so the fingerprint has to be computed from the text that actually lands
        in the row -- not from the incoming payload.
        """
        with TemporaryDirectory() as tmp:
            with unittest.mock.patch.object(pipeline, "DB_PATH", Path(tmp) / "pipeline.db"):
                conn = pipeline.connect()
                record = {
                    "external_id": "1",
                    "company": "Acme Robotics",
                    "title": "Mechanical Engineering Intern",
                    "location": "Austin, TX",
                    "url": "https://boards.example/1",
                    "description": SAMPLE_JD,
                }
                pipeline.upsert_jobs(conn, "greenhouse:acme", "Acme", [record])
                rich = conn.execute("SELECT content_fingerprint FROM jobs").fetchone()[0]
                self.assertNotEqual(rich, "")

                # The source now returns a stub for the same posting.
                pipeline.upsert_jobs(
                    conn, "greenhouse:acme", "Acme", [{**record, "description": "See website."}]
                )
                after = conn.execute(
                    "SELECT description, content_fingerprint FROM jobs"
                ).fetchone()
                self.assertEqual(after["content_fingerprint"], rich)
                self.assertEqual(
                    after["content_fingerprint"], pipeline.fingerprint_text(after["description"])
                )

                # And a genuinely richer refresh does move it.
                pipeline.upsert_jobs(
                    conn,
                    "greenhouse:acme",
                    "Acme",
                    [{**record, "description": SAMPLE_JD + " Additional requirements apply here."}],
                )
                refreshed = conn.execute("SELECT content_fingerprint FROM jobs").fetchone()[0]
                self.assertNotEqual(refreshed, rich)
                conn.close()

    def test_dedupe_keeps_different_companies_sharing_boilerplate_separate(self):
        """The threshold's discriminating power, as a regression test.

        The same-source test above can't exercise this: pass 3 skips matching
        source_keys outright, so it would pass at any threshold. This is the
        case that actually matters -- two employers, two sources, heavily
        overlapping stock JD language. A false merge here silently hides one
        company's real posting behind another's.
        """
        shared = (
            "We are an equal opportunity employer and value diversity at our company. "
            "All qualified applicants will receive consideration for employment without "
            "regard to race, colour, religion, sex, national origin, disability status, "
            "or protected veteran status. Benefits include medical, dental and vision "
            "coverage, a commuter benefit, and paid parental leave. Applicants must be "
            "currently authorised to work in the United States on a full-time basis. "
            "This role is based on site and may require occasional weekend work. "
        )
        with TemporaryDirectory() as tmp:
            with unittest.mock.patch.object(pipeline, "DB_PATH", Path(tmp) / "pipeline.db"):
                conn = pipeline.connect()
                pipeline.upsert_jobs(
                    conn,
                    "greenhouse:acme",
                    "Acme",
                    [
                        {
                            "external_id": "1",
                            "company": "Acme Robotics",
                            "title": "Mechanical Engineering Intern",
                            "location": "Austin, TX",
                            "url": "https://a.example/1",
                            "description": shared + "You will design robotic arm linkages.",
                        }
                    ],
                )
                pipeline.upsert_jobs(
                    conn,
                    "ashby:beta",
                    "Beta Power",
                    [
                        {
                            "external_id": "2",
                            "company": "Beta Power",
                            "title": "Thermal Engineering Intern",
                            "location": "Austin, TX",
                            "url": "https://b.example/2",
                            "description": shared + "You will model battery thermal loops.",
                        }
                    ],
                )
                rows = conn.execute(
                    "SELECT company, content_fingerprint, duplicate_of FROM jobs"
                ).fetchall()
                similarity = pipeline.fingerprint_similarity(
                    rows[0]["content_fingerprint"], rows[1]["content_fingerprint"]
                )
                # Genuinely similar text -- this is the hard case, not a strawman.
                self.assertGreater(similarity, 0.6)
                self.assertLess(similarity, pipeline.CROSSLIST_THRESHOLD)
                self.assertEqual([row["duplicate_of"] for row in rows], [None, None])
                conn.close()

    def test_connect_adds_content_fingerprint_to_existing_databases(self):
        with TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "pipeline.db"
            # A database created before the column existed.
            legacy = sqlite3.connect(db_path)
            legacy.execute(
                """
                CREATE TABLE jobs (
                    id TEXT PRIMARY KEY, source_key TEXT NOT NULL,
                    source_name TEXT NOT NULL, external_id TEXT NOT NULL,
                    company TEXT NOT NULL, title TEXT NOT NULL,
                    location TEXT NOT NULL DEFAULT '', role_type TEXT NOT NULL DEFAULT 'other',
                    url TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
                    posted_at TEXT, first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1, fingerprint TEXT NOT NULL,
                    duplicate_of TEXT, score INTEGER NOT NULL DEFAULT 0,
                    score_explanation TEXT NOT NULL DEFAULT '[]',
                    status TEXT NOT NULL DEFAULT 'discovered', notes TEXT NOT NULL DEFAULT '',
                    applied_at TEXT, follow_up_at TEXT,
                    UNIQUE(source_key, external_id)
                )
                """
            )
            legacy.commit()
            legacy.close()

            with unittest.mock.patch.object(pipeline, "DB_PATH", db_path):
                conn = pipeline.connect()
                columns = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)")}
                self.assertIn("content_fingerprint", columns)
                # Idempotent: opening again must not fail on a duplicate column.
                conn.close()
                conn = pipeline.connect()
                conn.close()

    # --- posting liveness ---------------------------------------------------

    def test_liveness_reads_typographic_closure_banners(self):
        # The banner uses U+2019 and an accented "expirée". A pattern spelled
        # with an ASCII apostrophe only matches because the body is normalized
        # first, which is the bug this guard exists for.
        verdict = pipeline.classify_liveness(
            status=200,
            requested_url="https://example.com/jobs/1",
            final_url="https://example.com/jobs/1",
            body_text="Cette offre n’est plus disponible. " + "x" * 400,
        )
        self.assertEqual(verdict["result"], "expired")
        self.assertEqual(verdict["code"], "expired_body")

    def test_liveness_filled_pattern_ignores_application_forms(self):
        live = pipeline.classify_liveness(
            status=200,
            body_text=(
                "This position is open. Once the application form has been filled out "
                "we will review it. " + "x" * 400
            ),
        )
        self.assertNotEqual(live["result"], "expired")

        # Same guard without the trailing "out" -- the preceding word is what
        # rules it out here.
        still_live = pipeline.classify_liveness(
            status=200,
            body_text=(
                "About this role. Once the application form has been filled we respond. "
                + "x" * 400
            ),
        )
        self.assertNotEqual(still_live["result"], "expired")

        dead = pipeline.classify_liveness(
            status=200,
            body_text="The job you are trying to apply for has been filled. " + "x" * 400,
        )
        self.assertEqual(dead["result"], "expired")

    def test_liveness_treats_bot_challenge_as_uncertain_not_expired(self):
        # A Cloudflare interstitial is short and has no apply control, so
        # without the ordering guard it would fall through to
        # insufficient_content and permanently retire a live posting.
        verdict = pipeline.classify_liveness(
            status=200,
            body_text="Just a moment... Ray ID: 8f2b1c",
        )
        self.assertEqual(verdict["result"], "uncertain")
        self.assertEqual(verdict["code"], "bot_challenge")

    def test_liveness_treats_server_errors_as_uncertain(self):
        for status in (403, 500, 502, 503):
            with self.subTest(status=status):
                verdict = pipeline.classify_liveness(status=status, body_text="502 Bad Gateway")
                self.assertEqual(verdict["result"], "uncertain")

    def test_liveness_never_retires_a_posting_on_a_throttle(self):
        """A 429 says "ask again later", never "this posting is gone".

        Its body is typically a one-line notice, which fell through to the
        content-length heuristic and classified as expired -- retiring a live
        posting, which purge-expired then deletes permanently. Parallel
        fetching makes throttles more likely, so this is pinned rather than
        left to the heuristics.
        """

        for body in ("Too Many Requests", "Rate limit exceeded. Please retry later.", ""):
            with self.subTest(body=body):
                verdict = pipeline.classify_liveness(
                    status=429,
                    requested_url="https://example.com/jobs/1",
                    final_url="https://example.com/jobs/1",
                    body_text=body,
                )
                self.assertEqual(verdict["result"], "uncertain")
                self.assertEqual(verdict["code"], "rate_limited")

    def test_liveness_gone_statuses_expire(self):
        for status in (404, 410):
            with self.subTest(status=status):
                verdict = pipeline.classify_liveness(status=status, body_text="Not found")
                self.assertEqual(verdict["result"], "expired")
                self.assertEqual(verdict["code"], "http_gone")

    def test_liveness_ignores_apply_controls_after_redirect_off_posting(self):
        # A dead permalink that redirects to a listing page still renders Apply
        # buttons -- for other jobs. The lost job id is what gives it away.
        verdict = pipeline.classify_liveness(
            status=200,
            requested_url="https://careers.example.com/job/1234567",
            final_url="https://careers.example.com/search",
            body_text="x" * 500,
            controls=["Apply"],
        )
        self.assertEqual(verdict["result"], "uncertain")
        self.assertEqual(verdict["code"], "redirected_off_posting")

    def test_liveness_apply_control_marks_active(self):
        verdict = pipeline.classify_liveness(
            status=200,
            requested_url="https://example.com/jobs/1234567",
            final_url="https://example.com/jobs/1234567",
            body_text="short body",
            controls=["Bewerben"],
        )
        self.assertEqual(verdict["result"], "active")

    def test_liveness_thin_page_without_apply_control_expires(self):
        verdict = pipeline.classify_liveness(status=200, body_text="Home About Careers")
        self.assertEqual(verdict["result"], "expired")
        self.assertEqual(verdict["code"], "insufficient_content")

    def test_apply_controls_reads_buttons_anchors_and_unclosed_tags(self):
        controls = pipeline.apply_controls(
            '<a href="/x">Apply now</a>'
            '<input type="submit" value="Submit application">'
            '<button aria-label="Easy Apply"></button>'
            "<a>Trailing unclosed"
        )
        self.assertIn("Apply now", controls)
        self.assertIn("Submit application", controls)
        self.assertIn("Easy Apply", controls)
        # An unclosed final anchor still yields its label.
        self.assertIn("Trailing unclosed", controls)

    def test_check_liveness_retires_dead_rows_but_protects_applied_ones(self):
        with TemporaryDirectory() as tmp:
            with unittest.mock.patch.object(pipeline, "DB_PATH", Path(tmp) / "pipeline.db"):
                conn = pipeline.connect()
                pipeline.upsert_jobs(
                    conn,
                    "agent:exa",
                    "Agent: Exa semantic search",
                    [
                        {
                            "external_id": "dead",
                            "company": "Gone Corp",
                            "title": "Mechanical Intern",
                            "location": "Austin, TX",
                            "url": "https://example.com/jobs/dead",
                            "description": "",
                        },
                        {
                            "external_id": "tracked",
                            "company": "Applied Corp",
                            "title": "Design Intern",
                            "location": "Austin, TX",
                            "url": "https://example.com/jobs/tracked",
                            "description": "",
                        },
                    ],
                )
                conn.execute("UPDATE jobs SET status='applied' WHERE external_id='tracked'")

                def fake_request_text(url, retries=2):
                    return 404, url, "Not found"

                with unittest.mock.patch.object(pipeline, "request_text", fake_request_text):
                    tally = pipeline.check_liveness(conn)

                self.assertEqual(tally["expired"], 2)
                self.assertEqual(tally["retired"], 1)
                states = {
                    row["external_id"]: row["active"]
                    for row in conn.execute("SELECT external_id, active FROM jobs")
                }
                self.assertEqual(states["dead"], 0)
                # An applied posting stays visible even when its page is gone.
                self.assertEqual(states["tracked"], 1)
                conn.close()

    def test_purge_expired_deletes_retired_and_past_deadline_rows_but_keeps_applications(self):
        def record(external_id, description=""):
            return {
                "external_id": external_id,
                "company": f"{external_id.title()} Corp",
                "title": "Mechanical Intern",
                "location": f"{external_id.title()}ville, TX",
                "url": f"https://example.com/jobs/{external_id}",
                "description": description,
            }

        with TemporaryDirectory() as tmp:
            with unittest.mock.patch.object(pipeline, "DB_PATH", Path(tmp) / "pipeline.db"):
                conn = pipeline.connect()
                pipeline.upsert_jobs(
                    conn,
                    "agent:exa",
                    "Agent: Exa semantic search",
                    [
                        record("retired"),
                        record("closed", "Apply by September 1, 2026."),
                        record("closing-today", "Deadline: 2026-09-14"),
                        record("open", "Applications close 10/01/2026"),
                        record("undated"),
                        record("applied-retired"),
                        record("shortlisted-retired"),
                    ],
                )
                conn.execute(
                    "UPDATE jobs SET active=0 WHERE external_id IN "
                    "('retired', 'applied-retired', 'shortlisted-retired')"
                )
                conn.execute("UPDATE jobs SET status='applied' WHERE external_id='applied-retired'")
                conn.execute(
                    "UPDATE jobs SET status='shortlisted' WHERE external_id='shortlisted-retired'"
                )

                preview = pipeline.purge_expired(conn, today="2026-09-14", dry_run=True)
                self.assertEqual(preview["deleted"], 0)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 7)

                tally = pipeline.purge_expired(conn, today="2026-09-14")
                remaining = {
                    row["external_id"] for row in conn.execute("SELECT external_id FROM jobs")
                }
                self.assertEqual(tally["retired"], 3)
                self.assertEqual(tally["past_deadline"], 1)
                self.assertEqual(tally["kept"], 1)
                self.assertEqual(tally["deleted"], 3)
                # The deadline day is still open, and an unstated deadline is never guessed.
                self.assertEqual(remaining, {"closing-today", "open", "undated", "applied-retired"})
                conn.close()

    def test_purge_expired_backs_up_before_deleting_and_never_deletes_without_one(self):
        record = {
            "external_id": "gone",
            "company": "Gone Corp",
            "title": "Mechanical Intern",
            "location": "Austin, TX",
            "url": "https://example.com/jobs/gone",
            "description": "",
        }
        with TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "pipeline.db"
            with unittest.mock.patch.object(pipeline, "DB_PATH", db_path):
                conn = pipeline.connect()
                pipeline.upsert_jobs(conn, "agent:exa", "Agent: Exa semantic search", [record])
                conn.execute("UPDATE jobs SET active=0")
                conn.commit()
                backups = Path(tmp) / "backups"

                pipeline.purge_expired(conn, today="2026-09-14", dry_run=True)
                self.assertFalse(backups.exists(), "a dry run must not write a backup")

                with unittest.mock.patch.object(pipeline, "backup_sqlite", side_effect=OSError("disk full")):
                    with self.assertRaises(OSError):
                        pipeline.purge_expired(conn, today="2026-09-14")
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 1)

                tally = pipeline.purge_expired(conn, today="2026-09-14")
                self.assertEqual(tally["deleted"], 1)
                snapshots = list(backups.glob("pipeline-*.db"))
                self.assertEqual(len(snapshots), 1)
                snapshot = sqlite3.connect(snapshots[0])
                try:
                    self.assertEqual(
                        snapshot.execute("SELECT external_id FROM jobs").fetchall(), [("gone",)]
                    )
                finally:
                    snapshot.close()
                conn.close()

    def test_backup_sqlite_keeps_only_the_newest_snapshots(self):
        with TemporaryDirectory() as tmp:
            # SQLite reports the resolved path; on macOS the temp dir under
            # /var is a symlink to /private/var, so compare resolved paths.
            tmp = Path(tmp).resolve()
            conn = sqlite3.connect(Path(tmp) / "pipeline.db")
            try:
                conn.execute("CREATE TABLE t(x)")
                conn.commit()
                manual = Path(tmp) / "backups" / "pipeline-20260914-pre-purge.db"
                manual.parent.mkdir()
                manual.write_bytes(b"hand-made backup")
                created = [pipeline.backup_sqlite(conn, "pipeline", keep=3) for _ in range(5)]
            finally:
                conn.close()
            remaining = sorted((Path(tmp) / "backups").glob("pipeline-*.db"))
            self.assertEqual(remaining, sorted([manual, *created[-3:]]), "manual backups are never pruned")
            memory = sqlite3.connect(":memory:")
            try:
                self.assertIsNone(pipeline.backup_sqlite(memory, "memory"))
            finally:
                memory.close()

    def _backups_at(self, clock_times, keep):
        """Back up a scratch database once per clock reading; return the paths made and kept."""
        readings = iter(clock_times)

        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return next(readings)

        with TemporaryDirectory() as tmp:
            tmp = Path(tmp).resolve()
            conn = sqlite3.connect(tmp / "pipeline.db")
            try:
                conn.execute("CREATE TABLE t(x)")
                conn.commit()
                with unittest.mock.patch.object(pipeline, "datetime", Clock):
                    created = [pipeline.backup_sqlite(conn, "pipeline", keep=keep) for _ in clock_times]
                kept = sorted((tmp / "backups").glob("pipeline-*.db"))
                return created, kept, [path.exists() for path in created]
            finally:
                conn.close()

    def test_backups_made_in_one_clock_tick_do_not_overwrite_each_other(self):
        # A coarse clock can give consecutive backups the same reading, which
        # used to give them the same file name, so the second replaced the first.
        tick = datetime(2026, 9, 24, 19, 27, 30, 229556, tzinfo=timezone.utc)
        created, kept, _ = self._backups_at([tick, tick, tick], keep=5)
        self.assertEqual(len(set(created)), 3, "two backups were written to the same file")
        self.assertEqual(kept, created, "backups do not sort in the order they were made")

    def test_a_backup_made_after_the_clock_steps_back_is_not_pruned_at_once(self):
        # If the clock steps back, a new snapshot named by it sorts before the
        # existing ones and the keep-newest pruning deletes it straight away,
        # leaving the caller to go ahead with a destructive change unprotected.
        now = datetime(2026, 9, 24, 19, 0, 0, tzinfo=timezone.utc)
        created, kept, exists = self._backups_at([now, now + timedelta(seconds=1), now - timedelta(hours=1)], keep=2)
        self.assertTrue(exists[-1], "the backup just made was pruned")
        self.assertEqual(kept, created[-2:])

    def test_check_liveness_promotes_the_survivor_when_a_canonical_is_retired(self):
        """Retiring a canonical must not take its duplicates down with it.

        `report` selects `active=1 AND duplicate_of IS NULL`, so a duplicate
        still pointing at a retired canonical satisfies neither side and the
        opportunity disappears from the shortlist altogether.
        """
        record = {
            "company": "Acme Robotics",
            "title": "Mechanical Engineering Intern",
            "location": "Austin, TX",
            "description": SAMPLE_JD,
        }
        with TemporaryDirectory() as tmp:
            with unittest.mock.patch.object(pipeline, "DB_PATH", Path(tmp) / "pipeline.db"):
                conn = pipeline.connect()
                pipeline.upsert_jobs(
                    conn,
                    "agent:exa",
                    "Agent: Exa semantic search",
                    [{**record, "external_id": "a", "url": "https://agent.example/1"}],
                )
                pipeline.upsert_jobs(
                    conn,
                    "greenhouse:acme",
                    "Acme Robotics",
                    [{**record, "external_id": "b", "url": "https://boards.example/1"}],
                )
                linked = conn.execute(
                    "SELECT COUNT(*) c FROM jobs WHERE duplicate_of IS NOT NULL"
                ).fetchone()["c"]
                self.assertEqual(linked, 1, "the two channels should have deduped")

                canonical = conn.execute(
                    "SELECT id, url FROM jobs WHERE duplicate_of IS NULL"
                ).fetchone()

                def fake_request_text(url, retries=2):
                    # Only the canonical's page is gone.
                    return (404, url, "Not found") if url == canonical["url"] else (200, url, "x" * 500)

                with unittest.mock.patch.object(pipeline, "request_text", fake_request_text):
                    pipeline.check_liveness(conn, check_all=True)

                survivors = conn.execute(
                    "SELECT id FROM jobs WHERE active=1 AND duplicate_of IS NULL"
                ).fetchall()
                self.assertEqual(
                    len(survivors), 1, "the surviving copy must be visible to report()"
                )
                self.assertNotEqual(survivors[0]["id"], canonical["id"])
                conn.close()

    def _one_agent_row(self, conn, source_key="agent:exa", status=None):
        pipeline.upsert_jobs(
            conn,
            source_key,
            "Agent",
            [
                {
                    "external_id": "1",
                    "company": "Acme",
                    "title": "Mechanical Intern",
                    "location": "Austin, TX",
                    "url": "https://example.com/jobs/1",
                    "description": "",
                }
            ],
        )
        if status:
            conn.execute("UPDATE jobs SET status=?", (status,))

    def test_liveness_never_retires_a_thin_page_from_a_channel_needing_rendering(self):
        """`agent:jina` and `agent:linkedin` pages are unreadable by a plain GET.

        They were imported through a renderer or a session, so re-fetching them
        with bare urllib returns an SPA shell or a login wall. Read naively that
        is `insufficient_content` -> expired, which would silently delete live
        postings on the first scheduled liveness run.
        """
        for source_key in ("agent:jina", "agent:linkedin"):
            with self.subTest(source_key=source_key):
                with TemporaryDirectory() as tmp:
                    with unittest.mock.patch.object(
                        pipeline, "DB_PATH", Path(tmp) / "pipeline.db"
                    ):
                        conn = pipeline.connect()
                        self._one_agent_row(conn, source_key)

                        def thin(url, retries=2):
                            return 200, url, "<div id='root'></div>"

                        with unittest.mock.patch.object(pipeline, "request_text", thin):
                            tally = pipeline.check_liveness(conn)
                        self.assertEqual(tally["retired"], 0)
                        self.assertEqual(tally["uncertain"], 1)
                        self.assertEqual(
                            conn.execute("SELECT active FROM jobs").fetchone()["active"], 1
                        )
                        conn.close()

    def test_liveness_still_trusts_hard_evidence_from_those_channels(self):
        # The downgrade is scoped to the weak heuristic only. A 404 is still a
        # 404 no matter how the posting was imported.
        with TemporaryDirectory() as tmp:
            with unittest.mock.patch.object(pipeline, "DB_PATH", Path(tmp) / "pipeline.db"):
                conn = pipeline.connect()
                self._one_agent_row(conn, "agent:jina")
                with unittest.mock.patch.object(
                    pipeline, "request_text", lambda url, retries=2: (404, url, "Gone")
                ):
                    tally = pipeline.check_liveness(conn)
                self.assertEqual(tally["retired"], 1)
                conn.close()

    def test_liveness_advances_the_queue_even_on_uncertain_verdicts(self):
        """Otherwise `--limit` restarts on the same stuck rows every day.

        The scheduled task runs `liveness --limit N` ordered by last_seen_at, so
        rows that can never resolve would monopolise every future run.
        """
        with TemporaryDirectory() as tmp:
            with unittest.mock.patch.object(pipeline, "DB_PATH", Path(tmp) / "pipeline.db"):
                conn = pipeline.connect()
                self._one_agent_row(conn)
                conn.execute("UPDATE jobs SET last_seen_at='2020-01-01T00:00:00+00:00'")
                conn.commit()

                with unittest.mock.patch.object(
                    pipeline,
                    "request_text",
                    lambda url, retries=2: (200, url, "Just a moment... Ray ID: 1"),
                ):
                    tally = pipeline.check_liveness(conn)

                self.assertEqual(tally["uncertain"], 1)
                self.assertGreater(
                    conn.execute("SELECT last_seen_at FROM jobs").fetchone()["last_seen_at"],
                    "2020-01-01T00:00:00+00:00",
                )
                conn.close()

    def test_liveness_shortlisted_postings_are_reported_but_kept(self):
        with TemporaryDirectory() as tmp:
            with unittest.mock.patch.object(pipeline, "DB_PATH", Path(tmp) / "pipeline.db"):
                conn = pipeline.connect()
                self._one_agent_row(conn, status="shortlisted")
                with unittest.mock.patch.object(
                    pipeline, "request_text", lambda url, retries=2: (404, url, "Gone")
                ):
                    tally = pipeline.check_liveness(conn)
                self.assertEqual(tally["expired"], 1)
                # You picked this one deliberately; it must not vanish silently.
                self.assertEqual(tally["retired"], 0)
                self.assertEqual(conn.execute("SELECT active FROM jobs").fetchone()["active"], 1)
                conn.close()

    def test_liveness_commits_each_row_so_a_later_crash_keeps_earlier_work(self):
        with TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "pipeline.db"
            with unittest.mock.patch.object(pipeline, "DB_PATH", db_path):
                conn = pipeline.connect()
                pipeline.upsert_jobs(
                    conn,
                    "agent:exa",
                    "Agent",
                    [
                        {
                            "external_id": str(n),
                            "company": "Acme",
                            "title": f"Intern {n}",
                            "location": "Austin, TX",
                            "url": f"https://example.com/jobs/{n}",
                            "description": "",
                        }
                        for n in (1, 2)
                    ],
                )
                seen = []

                def flaky(url, retries=2):
                    seen.append(url)
                    if len(seen) == 2:
                        raise KeyboardInterrupt("interrupted mid-run")
                    return 404, url, "Gone"

                with unittest.mock.patch.object(pipeline, "request_text", flaky):
                    with self.assertRaises(KeyboardInterrupt):
                        pipeline.check_liveness(conn)
                conn.close()

                # Reopening proves the first row's retirement was durable, not
                # rolled back with the interrupted transaction.
                reopened = pipeline.connect()
                retired = reopened.execute(
                    "SELECT COUNT(*) c FROM jobs WHERE active=0"
                ).fetchone()["c"]
                self.assertEqual(retired, 1)
                reopened.close()

    def test_check_liveness_skips_ats_rows_by_default(self):
        with TemporaryDirectory() as tmp:
            with unittest.mock.patch.object(pipeline, "DB_PATH", Path(tmp) / "pipeline.db"):
                conn = pipeline.connect()
                pipeline.upsert_jobs(
                    conn,
                    "greenhouse:spacex",
                    "SpaceX",
                    [
                        {
                            "external_id": "1",
                            "company": "SpaceX",
                            "title": "Mechanical Intern",
                            "location": "Austin, TX",
                            "url": "https://example.com/jobs/1",
                            "description": "",
                        }
                    ],
                )
                calls = []

                def fake_request_text(url, retries=2):
                    calls.append(url)
                    return 404, url, "Not found"

                with unittest.mock.patch.object(pipeline, "request_text", fake_request_text):
                    pipeline.check_liveness(conn)
                # The Greenhouse batch already retires its own rows.
                self.assertEqual(calls, [])

                with unittest.mock.patch.object(pipeline, "request_text", fake_request_text):
                    pipeline.check_liveness(conn, check_all=True)
                self.assertEqual(len(calls), 1)
                conn.close()

    def test_check_liveness_dry_run_changes_nothing(self):
        with TemporaryDirectory() as tmp:
            with unittest.mock.patch.object(pipeline, "DB_PATH", Path(tmp) / "pipeline.db"):
                conn = pipeline.connect()
                pipeline.upsert_jobs(
                    conn,
                    "agent:exa",
                    "Agent: Exa semantic search",
                    [
                        {
                            "external_id": "dead",
                            "company": "Gone Corp",
                            "title": "Mechanical Intern",
                            "location": "Austin, TX",
                            "url": "https://example.com/jobs/dead",
                            "description": "",
                        }
                    ],
                )

                def fake_request_text(url, retries=2):
                    return 404, url, "Not found"

                with unittest.mock.patch.object(pipeline, "request_text", fake_request_text):
                    tally = pipeline.check_liveness(conn, dry_run=True)

                self.assertEqual(tally["expired"], 1)
                self.assertEqual(tally["retired"], 0)
                self.assertEqual(
                    conn.execute("SELECT active FROM jobs").fetchone()["active"], 1
                )
                conn.close()

    def test_check_liveness_request_failure_never_retires(self):
        with TemporaryDirectory() as tmp:
            with unittest.mock.patch.object(pipeline, "DB_PATH", Path(tmp) / "pipeline.db"):
                conn = pipeline.connect()
                pipeline.upsert_jobs(
                    conn,
                    "agent:exa",
                    "Agent: Exa semantic search",
                    [
                        {
                            "external_id": "x",
                            "company": "Flaky Corp",
                            "title": "Mechanical Intern",
                            "location": "Austin, TX",
                            "url": "https://example.com/jobs/x",
                            "description": "",
                        }
                    ],
                )

                def fake_request_text(url, retries=2):
                    raise RuntimeError("network down")

                with unittest.mock.patch.object(pipeline, "request_text", fake_request_text):
                    tally = pipeline.check_liveness(conn)

                self.assertEqual(tally["error"], 1)
                self.assertEqual(tally["retired"], 0)
                self.assertEqual(
                    conn.execute("SELECT active FROM jobs").fetchone()["active"], 1
                )
                conn.close()


if __name__ == "__main__":
    unittest.main()
