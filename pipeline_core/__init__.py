"""The legacy pipeline's code, and the framework-neutral read model it shares with the web app.

``pipeline.py`` at the repository root is only the command-line entry point. The pipeline itself is split by concern into
flat modules, all standard library only (tests/test_dependency_boundary.py):

    paths       every file and directory it touches; read as ``paths.DB_PATH`` at call time, never copied
    clock       now_iso and the one timestamp parser
    config      the .env file, the profile and the layered source catalog
    text        HTML stripping, URL canonicalisation and the posting fingerprints
    http        the HTTP client: retries, the per-host rate limiter, the curl fallback
    sources     the job-board adapters and their fetch table
    store       the database: schema, upserts, retirement and cross-listing dedup
    liveness    deciding whether a posting is still open
    retention   backups and the purge of expired postings
    discovery   ATS board discovery
    fetch       the fetch scheduler
    importers   CSV, email and agent importers, and description enrichment
    scoring     fit scoring, with every adjustment recorded as a reason
    reports     the shortlist files, the dashboard, status and the doctor check
    artifacts   the tailored resume and cover letter
    cli         the argument parser and ``main``

The web app imports these through ``opportunity_app/legacy.py`` and nowhere else.
"""

from .read_model import (
    MAX_PER_COMPANY,
    RANKED_VIEW_PER_COMPANY,
    OpportunityFilters,
    OpportunityRepository,
)
from .visibility import CAPTURE_SOURCE_KEY, capture_visible_sql

__all__ = [
    "CAPTURE_SOURCE_KEY",
    "MAX_PER_COMPANY",
    "OpportunityFilters",
    "OpportunityRepository",
    "RANKED_VIEW_PER_COMPANY",
    "capture_visible_sql",
]
