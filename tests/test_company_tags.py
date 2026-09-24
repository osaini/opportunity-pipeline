"""Company tags: generated from postings, filterable, and removable per student."""

import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi.testclient import TestClient

from opportunity_app import STATIC_DIR
from opportunity_app.api import create_app
from opportunity_app.company_tags import RULES_FINGERPRINT, classify_company, ensure_company_tags_current, normalize_tag
from opportunity_app.operations import export_account
from opportunity_app.schema import LOCAL_USER_ID, connect_product, ensure_product_schema, migrate_legacy_database

from helpers_platform import build_and_migrate, build_profile

OWNER = {"Authorization": "Bearer owner-static-token"}


def tags_of(result):
    return [entry["tag"] for entry in result]


class ClassifyCompanyTests(unittest.TestCase):
    def test_the_company_name_alone_is_enough(self):
        found = classify_company("Acme Robotics", [("Mechanical Engineering Intern", "Design mechanisms.")])
        self.assertEqual(tags_of(found), ["robotics"])
        self.assertIn("company name", found[0]["evidence"])
        self.assertIn("robotics", found[0]["evidence"])

    def test_a_title_match_is_enough(self):
        found = classify_company("Northwind", [("Propulsion Engineering Intern", "")])
        self.assertEqual(tags_of(found), ["aerospace"])
        self.assertIn("job titles", found[0]["evidence"])

    def test_one_keyword_repeated_in_every_posting_is_not_enough(self):
        # Boilerplate is copied into every posting a company publishes; ten
        # copies of one word must not outvote the actual work.
        postings = [("Intern", "Our customers include payments teams.")] * 10
        self.assertEqual(classify_company("Northwind", postings), [])

    def test_two_different_description_keywords_are_enough(self):
        found = classify_company("Northwind", [("Intern", "Machine learning and computer vision research.")])
        self.assertEqual(tags_of(found), ["ai"])

    def test_benefits_and_eeo_boilerplate_tag_nothing(self):
        boilerplate = (
            "We offer health insurance, dental, and vision. We are an equal opportunity "
            "employer and consider protected veterans and military status. Pursuing a "
            "degree in Mechanical, Chemical, or Civil Engineering."
        )
        self.assertEqual(classify_company("Northwind", [("Intern", boilerplate)] * 3), [])

    def test_at_most_three_tags_strongest_first(self):
        found = classify_company(
            "Orbit Space Robotics Energy Bank",
            [("Robotics Intern", "Robots that service satellites and rockets.")],
        )
        self.assertEqual(len(found), 3)
        self.assertEqual(found[0]["tag"], "robotics")

    def test_spellings_of_one_keyword_count_once(self):
        # "fpga" and "FPGAs" once tagged a humanoid-robot maker semiconductors.
        self.assertEqual(classify_company("Northwind", [("Intern", "FPGA work. Our FPGAs are fast.")]), [])

    def test_drone_is_its_own_tag_not_robotics(self):
        found = classify_company("Northwind", [("Drone Test Intern", "")])
        self.assertEqual(tags_of(found), ["drone"])

    def test_a_drone_company_describing_itself_in_every_posting_is_tagged(self):
        about = "Northwind is the world's largest drone delivery service."
        postings = [(f"Role {n}", about + " Great benefits.") for n in range(5)]
        found = classify_company("Northwind", postings)
        self.assertEqual(tags_of(found), ["drone"])
        self.assertIn("in 5 of 5 postings", found[0]["evidence"])

    def test_one_passing_drone_mention_is_not_enough(self):
        postings = [("Intern", "Experience on a rocket, UAV, or design team.")] + [("Intern", "Composites.")] * 9
        self.assertEqual(classify_company("Northwind", postings), [])
        # Two postings, but only a tenth of them: still a side mention.
        postings = [("Intern", "Supersonic unmanned aircraft.")] * 2 + [("Intern", "Composites.")] * 18
        self.assertEqual(classify_company("Northwind", postings), [])

    def test_a_niche_tag_is_not_crowded_out_by_broad_ones(self):
        postings = [
            ("Aircraft Manufacturing Engineer", "Firmware and supply chain for our drones."),
            ("Avionics Hardware Engineer", "Machining and logistics for our drones."),
        ]
        found = tags_of(classify_company("Northwind", postings))
        self.assertIn("drone", found)
        self.assertEqual(len([tag for tag in found if tag != "drone"]), 3)

    def test_tags_are_one_word(self):
        self.assertEqual(normalize_tag("  #Climate-Tech "), "climate-tech")
        for bad in ("two words", "", "-leading", "a" * 25, "emoji🙂"):
            with self.assertRaises(ValueError, msg=bad):
                normalize_tag(bad)


class CompanyTagApiTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        for name in ("resumes", "captures", "mock-interviews"):
            (self.root / name).mkdir()
        self.legacy_path, self.platform_path = build_and_migrate(self.root)
        app = create_app(
            db_path=self.platform_path,
            access_token="owner-static-token",
            admin_token="admin-tags",
            static_dir=STATIC_DIR,
            resume_storage=self.root / "resumes",
            capture_storage=self.root / "captures",
            interview_storage=self.root / "mock-interviews",
        )
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        self.tempdir.cleanup()

    def listing(self, headers=OWNER, **params):
        response = self.client.get("/api/v1/opportunities", headers=headers, params=params)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def tags_by_company(self, headers=OWNER):
        return {item["company"]: item["tags"] for item in self.listing(headers)["items"]}

    def put_tag(self, company, tag, present, headers=OWNER):
        return self.client.put(
            "/api/v1/company-tags",
            headers=headers,
            json={"company": company, "tag": tag, "present": present},
        )

    def resync(self):
        migrate_legacy_database(self.legacy_path, self.platform_path, build_profile(self.root))

    def test_generated_tags_arrive_on_the_list_and_detail_marked_as_inferred(self):
        tags = self.tags_by_company()
        self.assertEqual(tags_of(tags["Acme Robotics"]), ["robotics"])
        self.assertEqual(tags["Acme Robotics"][0]["origin"], "auto")
        self.assertIn("Inferred", tags["Acme Robotics"][0]["evidence"])
        self.assertEqual(tags["Orbit Systems"], [])
        detail = self.client.get("/api/v1/opportunities/job-a", headers=OWNER).json()
        self.assertEqual(tags_of(detail["tags"]), ["robotics"])

    def test_the_tag_filter_narrows_the_list_and_facets_count_companies(self):
        filtered = self.listing(tag="robotics")
        self.assertEqual([item["company"] for item in filtered["items"]], ["Acme Robotics"])
        self.assertEqual(filtered["total"], 1)
        self.assertEqual(self.listing(tag="#Robotics")["total"], 1)
        self.assertEqual(self.listing(tag="aerospace")["total"], 0)
        facets = self.client.get("/api/v1/facets", headers=OWNER).json()
        self.assertEqual(facets["tags"], [{"tag": "robotics", "companies": 1}])

    def test_a_removed_tag_stays_removed_after_the_next_sync(self):
        removed = self.put_tag("Acme Robotics", "robotics", False)
        self.assertEqual(removed.status_code, 200, removed.text)
        self.assertEqual(removed.json()["tags"], [])
        self.assertEqual(self.tags_by_company()["Acme Robotics"], [])
        self.assertEqual(self.listing(tag="robotics")["total"], 0)

        self.resync()
        self.assertEqual(self.tags_by_company()["Acme Robotics"], [])
        with closing(connect_product(self.platform_path)) as conn:
            # The generated tag still exists; only this student hid it.
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM company_tags").fetchone()[0], 1)

        restored = self.put_tag("Acme Robotics", "robotics", True)
        self.assertEqual(tags_of(restored.json()["tags"]), ["robotics"])
        with closing(connect_product(self.platform_path)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM company_tag_choices").fetchone()[0], 0)

    def test_a_tag_the_student_adds_is_theirs_filterable_and_removable(self):
        added = self.put_tag("orbit systems", "Controls", True)
        self.assertEqual(added.status_code, 200, added.text)
        self.assertEqual(added.json()["tags"], [{"tag": "controls", "origin": "manual", "evidence": "Added by you."}])
        self.assertEqual([item["company"] for item in self.listing(tag="controls")["items"]], ["Orbit Systems"])

        self.resync()
        self.assertEqual(tags_of(self.tags_by_company()["Orbit Systems"]), ["controls"])

        self.put_tag("Orbit Systems", "controls", False)
        self.assertEqual(self.tags_by_company()["Orbit Systems"], [])
        with closing(connect_product(self.platform_path)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM company_tag_choices").fetchone()[0], 0)

    def test_changed_rules_rebuild_tags_on_startup_and_keep_choices(self):
        self.put_tag("Orbit Systems", "controls", True)
        with closing(connect_product(self.platform_path)) as conn:
            self.assertFalse(ensure_company_tags_current(conn))
            conn.execute("DELETE FROM company_tags")
            conn.execute("UPDATE company_tag_rules SET fingerprint = 'older-rules'")
            conn.commit()
            ensure_product_schema(conn)
            fingerprint = conn.execute("SELECT fingerprint FROM company_tag_rules").fetchone()[0]
        self.assertEqual(fingerprint, RULES_FINGERPRINT)
        tags = self.tags_by_company()
        self.assertEqual(tags_of(tags["Acme Robotics"]), ["robotics"])
        self.assertEqual(tags_of(tags["Orbit Systems"]), ["controls"])

    def test_choices_are_exported_with_the_account(self):
        self.put_tag("Acme Robotics", "robotics", False)
        with closing(connect_product(self.platform_path)) as conn:
            exported = export_account(conn, user_id=LOCAL_USER_ID)["company_tag_choices"]
        self.assertEqual([(row["tag"], row["choice"]) for row in exported], [("robotics", "removed")])

    def test_invalid_tags_and_unknown_companies_are_refused(self):
        self.assertEqual(self.put_tag("Acme Robotics", "two words", True).status_code, 422)
        self.assertEqual(self.put_tag("Nobody Inc", "robotics", True).status_code, 404)
        self.assertEqual(self.client.put(
            "/api/v1/company-tags", json={"company": "Acme Robotics", "tag": "x", "present": True}
        ).status_code, 401)

    def test_one_students_tag_edits_never_reach_another(self):
        with TestClient(self.client.app):
            enabled = self.client.put(
                "/api/v1/admin/feature-flags/allow_public_signup",
                headers={"Authorization": "Bearer admin-tags"},
                json={"enabled": True, "description": "test tags"},
            )
            self.assertEqual(enabled.status_code, 200, enabled.text)
        registered = self.client.post(
            "/api/v1/auth/register",
            json={"email": "b@example.com", "password": "PasswordB123", "display_name": "Student B"},
        )
        self.assertEqual(registered.status_code, 201, registered.text)
        other = {"Authorization": f"Bearer {registered.json()['api_token']}"}

        self.put_tag("Acme Robotics", "robotics", False)
        self.put_tag("Orbit Systems", "controls", True)
        tags = self.tags_by_company(other)
        self.assertEqual(tags_of(tags["Acme Robotics"]), ["robotics"])
        self.assertEqual(tags["Orbit Systems"], [])
        self.assertEqual(self.listing(other, tag="controls")["total"], 0)


if __name__ == "__main__":
    unittest.main()
