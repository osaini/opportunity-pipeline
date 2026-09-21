"""Resume draft parsing: section headings, structured entries, skills, and document links."""

import io
import unittest
import zipfile
from pathlib import Path

from pypdf import PdfWriter
from pypdf.annotations import Link

from opportunity_app.outreach_drafting import outreach_proof
from opportunity_app.resumes import (
    extract_docx_links,
    extract_pdf_links,
    extract_resume_links,
    parse_resume_draft,
)


FIXTURES = Path(__file__).parent / "fixtures"


def sample_text() -> str:
    return (FIXTURES / "resume_sample.txt").read_text(encoding="utf-8")


class ResumeSectionTests(unittest.TestCase):
    def setUp(self):
        self.parsed = parse_resume_draft(sample_text())

    def test_headings_ending_in_a_known_alias_open_their_sections(self):
        sections = self.parsed["sections"]
        self.assertEqual(sections["projects"][0], "Desktop CNC Plotter Jan 2025 - Mar 2025")
        self.assertEqual(sections["activities"][0], "Longhorn Salsa Club Aug 2023 - Present")
        self.assertNotIn("Engineering Projects", sections["experience"])
        self.assertTrue(all("Salsa" not in line for line in sections["skills"]))

    def test_skills_come_only_from_the_skills_section(self):
        self.assertEqual(
            self.parsed["skills"],
            [
                "SolidWorks", "Fusion 360", "AutoCAD",
                "CNC", "Laser Cutting", "FDM 3D Printing",
                "Python for data processing", "C++ for embedded firmware", "JavaScript for front-end dashboards",
            ],
        )
        joined = " ".join(self.parsed["skills"])
        for leaked in ("Salsa", "Captain", "rehearsal", "Coursework", "Statics"):
            self.assertNotIn(leaked, joined)

    def test_experience_entries_split_name_dates_role_and_joined_bullets(self):
        self.assertEqual(
            self.parsed["experience"],
            [
                {
                    "organization": "Rotorworks Robotics",
                    "role": "Engineering Intern",
                    "dates": "Nov 2025 - Present",
                    "highlights": [
                        "Designed a 7-inch quadcopter frame in SolidWorks, cutting airframe weight from 1.2 kg to 700 g "
                        "across a pilot build of 12 units",
                        "Wrote bench test procedures that shortened motor qualification from 3 days to 1 day",
                    ],
                },
                {
                    "organization": "Longhorn Data Lab",
                    "role": "Software Intern",
                    "dates": "Jun 2024 - Aug 2025",
                    "highlights": [
                        "Automated cleanup of 80,000 sensor records in Python with zero data loss, improving report "
                        "turnaround by 25%",
                        "Built a dashboard for 4 lab teams",
                    ],
                },
            ],
            "U+FFFD bullets and en-dash date ranges parse like the bullet character and hyphen",
        )

    def test_projects_and_activities_become_entries(self):
        self.assertEqual(
            self.parsed["projects"],
            [
                {
                    "title": "Desktop CNC Plotter",
                    "dates": "Jan 2025 - Mar 2025",
                    "highlights": [
                        "Built a two-axis pen plotter from 3D-printed parts and an Arduino Uno, drawing at 0.2 mm "
                        "repeatability over a 300 mm bed",
                        "Documented the build for 15 club members",
                    ],
                }
            ],
        )
        self.assertEqual(
            self.parsed["activities"],
            [
                {
                    "organization": "Longhorn Salsa Club",
                    "role": "Captain",
                    "dates": "Aug 2023 - Present",
                    "highlights": [
                        "Led a 30-member dance team through 150+ rehearsal hours and 6 campus performances",
                        "Managed choreography, costumes, and social media for the team",
                    ],
                }
            ],
        )

    def test_entries_are_offered_for_confirmation_without_an_outreach_mark(self):
        suggestions = self.parsed["profile_suggestions"]
        self.assertEqual(set(suggestions), {"name", "contact", "skills", "experience", "projects", "activities"})
        self.assertNotIn("awards", suggestions, "an empty section is not offered as a fact")
        for field in ("experience", "projects", "activities"):
            self.assertEqual(suggestions[field], self.parsed[field])
            for entry in suggestions[field]:
                self.assertNotIn("outreach", entry)
        proof, lead = outreach_proof(suggestions)
        self.assertEqual(lead, [], "unmarked entries are support, never lead")
        self.assertEqual(len(proof["experience"]), 2)

    def test_awards_listed_as_bullets_become_one_entry_each(self):
        parsed = parse_resume_draft(
            "Test Student\nHonors and Awards\n• Dean's List Fall 2024\n• FIRE Research Scholar 2025\n"
        )
        self.assertEqual(
            parsed["awards"],
            [
                {"title": "Dean's List", "dates": "Fall 2024"},
                {"title": "FIRE Research Scholar", "dates": "2025"},
            ],
        )

    def test_unparsed_headings_stop_the_previous_section(self):
        parsed = parse_resume_draft(
            "Test Student\nTechnical Skills\nCAD: SolidWorks, Fusion 360\nRelevant Coursework\nStatics, Dynamics\n"
        )
        self.assertEqual(parsed["skills"], ["SolidWorks", "Fusion 360"])

    def test_content_lines_are_not_read_as_headings(self):
        parsed = parse_resume_draft(
            "Test Student\nExperience\nProjects Lab Jan 2024 - May 2024\nResearch Assistant\n"
            "• Led 3 projects\nSkills\nTools: Python, Projects\n"
        )
        self.assertEqual(parsed["experience"][0]["organization"], "Projects Lab")
        self.assertEqual(parsed["experience"][0]["highlights"], ["Led 3 projects"])
        self.assertEqual(parsed["skills"], ["Python", "Projects"])


def pdf_with_links(urls: list[str]) -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    # Added bottom-up, so reading order must come from position, not insertion.
    for index, url in enumerate(urls):
        top = 100 + index * 40
        writer.add_annotation(0, Link(rect=(72, top, 200, top + 12), url=url))
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


class ResumeLinkTests(unittest.TestCase):
    def test_pdf_link_annotations_fill_contact_links(self):
        data = pdf_with_links(
            [
                "https://project-demo.example.com/plotter",
                "https://jordan-portfolio.example.com/",
                "https://github.com/jordan-rivera",
                "https://www.linkedin.com/in/jordan-rivera/",
                "mailto:jordan.rivera@example.edu",
            ]
        )
        links = extract_resume_links(data, "application/pdf")
        self.assertEqual(
            links,
            [
                "https://www.linkedin.com/in/jordan-rivera/",
                "https://github.com/jordan-rivera",
                "https://jordan-portfolio.example.com/",
                "https://project-demo.example.com/plotter",
            ],
            "mailto is dropped and links read top of the page first",
        )
        contact = parse_resume_draft(sample_text(), links)["contact"]
        self.assertEqual(
            contact,
            {
                "email": "jordan.rivera@example.edu",
                "phone": "(512) 555-0142",
                "linkedin": "https://www.linkedin.com/in/jordan-rivera/",
                "github": "https://github.com/jordan-rivera",
                "portfolio": "https://jordan-portfolio.example.com/",
            },
        )

    def test_github_pages_site_is_a_portfolio_not_github(self):
        contact = parse_resume_draft("Test Student\nhttps://jordan.github.io | https://github.com/jordan\n")["contact"]
        self.assertEqual(contact["github"], "https://github.com/jordan")
        self.assertEqual(contact["portfolio"], "https://jordan.github.io")

    def test_unreadable_pdf_yields_no_links(self):
        self.assertEqual(extract_pdf_links(b"%PDF-1.4 not a real pdf"), [])

    def test_docx_hyperlink_relationships_fill_contact_links(self):
        relationships = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
            '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink" '
            'Target="https://www.linkedin.com/in/jordan-rivera/" TargetMode="External"/>'
            "</Relationships>"
        )
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            archive.writestr("word/_rels/document.xml.rels", relationships)
        self.assertEqual(extract_docx_links(output.getvalue()), ["https://www.linkedin.com/in/jordan-rivera/"])


if __name__ == "__main__":
    unittest.main()
