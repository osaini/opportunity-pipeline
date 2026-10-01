"""The one door from the web app into the legacy pipeline (``pipeline.py``).

``pipeline.py`` is the dependency-free CLI the product grew from: it fetches the
sources, scores postings and owns ``data/pipeline.db``. The web app still needs a
few things from it (the source catalog, the ``.env`` loader, the scorer, the
backup helper and the default paths). Every one is imported here, and web modules
import ``pipeline`` through this module and nowhere else, so what the web app
depends on is this list and splitting ``pipeline.py`` later changes this file only.
``tests/test_leaf_modules.py`` fails if another web module imports ``pipeline``.

Pure functions that moved into ``pipeline_core`` (``identity_tokens``,
``normalized``, ``region_label``, ...) are not re-exported: import them from there.
"""

from __future__ import annotations

from pathlib import Path

import pipeline
from pipeline import (
    backup_sqlite,
    degree_levels,
    discover_ats,
    score_job,
    write_discovered_sources,
)
from pipeline_core.config import load_env_file, load_sources, source_key
from pipeline_core.http import USER_AGENT
from pipeline_core.paths import PROFILE_PATH, SOURCES_LOCAL_PATH, SOURCES_PATH

__all__ = [
    "PROFILE_PATH",
    "SOURCES_LOCAL_PATH",
    "SOURCES_PATH",
    "USER_AGENT",
    "backup_sqlite",
    "create_database",
    "degree_levels",
    "discover_ats",
    "load_env_file",
    "load_sources",
    "score_job",
    "source_key",
    "write_discovered_sources",
]


def create_database(path: Path) -> None:
    """Create the legacy pipeline database at ``path`` (schema included) and close it.

    ``pipeline.connect`` takes the path, so nothing swaps the module-global
    ``pipeline.DB_PATH`` and a concurrent reader of that global never sees it move.
    """
    pipeline.connect(path).close()
