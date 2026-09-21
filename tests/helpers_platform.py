"""Shared fixtures for platform-side unit tests."""

import json
import sqlite3
from pathlib import Path

from opportunity_app.schema import migrate_legacy_database

LEGACY_SCHEMA = """
CREATE TABLE jobs (
    id TEXT PRIMARY KEY,
    source_key TEXT NOT NULL,
    source_name TEXT NOT NULL,
    external_id TEXT NOT NULL,
    company TEXT NOT NULL,
    title TEXT NOT NULL,
    location TEXT NOT NULL DEFAULT '',
    role_type TEXT NOT NULL DEFAULT 'other',
    url TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    posted_at TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    fingerprint TEXT NOT NULL,
    content_fingerprint TEXT NOT NULL DEFAULT '',
    duplicate_of TEXT,
    score INTEGER NOT NULL DEFAULT 0,
    score_explanation TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'discovered',
    notes TEXT NOT NULL DEFAULT '',
    applied_at TEXT,
    follow_up_at TEXT
);
"""

JOBS = [
    (
        "job-a",
        "greenhouse:acme",
        "Acme Greenhouse",
        "a-1",
        "Acme Robotics",
        "Mechanical Engineering Intern",
        "Austin, TX",
        "internship",
        "https://example.com/jobs/a",
        "Design mechanisms using SolidWorks. Apply by September 1, 2026.",
        "2026-08-08T00:00:00+00:00",
        "2026-08-08T01:00:00+00:00",
        "2026-08-09T01:00:00+00:00",
        1,
        "fp-a",
        "cfp-a",
        None,
        91,
        '["35 base", "SolidWorks matches profile skills", "Austin target region"]',
        "shortlisted",
        "Strong fit",
        None,
        "2026-08-14",
    ),
    (
        "job-b",
        "lever:orbit",
        "Orbit Lever",
        "b-1",
        "Orbit Systems",
        "Controls Co-op",
        "Remote",
        "co-op",
        "https://example.com/jobs/b",
        "Summer 2027 controls role with Python.",
        "2026-08-07T00:00:00+00:00",
        "2026-08-07T01:00:00+00:00",
        "2026-08-09T02:00:00+00:00",
        1,
        "fp-b",
        "cfp-b",
        None,
        76,
        '["35 base"]',
        "applied",
        "Applied on employer site",
        "2026-08-09T03:00:00+00:00",
        "2026-08-16",
    ),
]


def build_profile(root: Path) -> Path:
    profile_path = root / "profile.json"
    profile_path.write_text(
        json.dumps(
            {
                "name": "Test Student",
                "regions": [
                    {
                        "name": "Austin",
                        "radius": "close",
                        "bonus": 15,
                        "state_markers": ["tx", "texas"],
                        "aliases": ["greater austin"],
                        "places": ["austin", "round rock"],
                    }
                ],
                "remote_ok": True,
            }
        ),
        encoding="utf-8",
    )
    return profile_path


def build_and_migrate(root: Path) -> tuple[Path, Path]:
    """Create a minimal legacy pipeline database and migrate it to the product schema."""
    legacy_path = root / "pipeline.db"
    platform_path = root / "platform.db"
    conn = sqlite3.connect(legacy_path)
    try:
        conn.executescript(LEGACY_SCHEMA)
        conn.executemany(
            "INSERT INTO jobs VALUES(" + ",".join("?" * 23) + ")",
            JOBS,
        )
        conn.commit()
    finally:
        conn.close()
    migrate_legacy_database(legacy_path, platform_path, build_profile(root))
    return legacy_path, platform_path
