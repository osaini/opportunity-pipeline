"""Every file and directory the legacy pipeline reads or writes, in one module.

Read these as attributes at call time (``paths.DB_PATH``), never with ``from .paths import DB_PATH``: one name
then has one patch target, and a test or the web worker that repoints it moves every reader at once.
"""

from __future__ import annotations

import os
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
# PIPELINE_DB lets tests and the web worker target a hermetic database copy;
# the default remains the operator's canonical pipeline database.
DB_PATH = Path(os.environ.get("PIPELINE_DB", str(ROOT / "data" / "pipeline.db")))
# profile.json and sources.local.json are personal and gitignored; setup copies
# config/profile.example.json into place. sources.json is the shared, tracked
# catalog, and sources.local.json layers one student's searches on top of it.
# PIPELINE_PROFILE, like PIPELINE_DB, lets tests and the web worker point a
# subprocess at a hermetic profile instead of the student's own.
PROFILE_PATH = Path(os.environ.get("PIPELINE_PROFILE") or ROOT / "config" / "profile.json")
PROFILE_EXAMPLE_PATH = ROOT / "config" / "profile.example.json"
SOURCES_PATH = ROOT / "config" / "sources.json"
SOURCES_LOCAL_PATH = ROOT / "config" / "sources.local.json"
ENV_PATH = ROOT / ".env"
MANUAL_PATH = ROOT / "data" / "manual_jobs.csv"
EMAIL_IMPORT_PATH = ROOT / "data" / "linkedin_emails.json"
DISCOVERED_IMPORT_PATH = ROOT / "data" / "discovered_jobs.json"
ENRICHMENT_PATH = ROOT / "data" / "enrichment.json"
OUTPUT_MD = ROOT / "output" / "shortlist.md"
OUTPUT_CSV = ROOT / "output" / "shortlist.csv"
OUTPUT_DASHBOARD = ROOT / "output" / "dashboard.html"

RESUME_PATH = ROOT / "config" / "resume.json"
RESUME_EXAMPLE_PATH = ROOT / "config" / "resume.example.json"
TEMPLATE_DIR = ROOT / "templates"
ARTIFACT_DIR = ROOT / "output" / "applications"


def display_path(path: Path) -> str:
    """Project-relative when the file lives here, absolute when it does not.

    Import and enrichment paths are user-supplied and may point anywhere on
    disk. `Path.relative_to` raises for those, and formatting a success message
    must never be what fails a command whose database work already committed.
    """
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)
