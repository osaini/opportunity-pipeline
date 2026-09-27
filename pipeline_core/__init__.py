"""Framework-neutral read model shared by the legacy CLI and web app.

Legacy pipeline functions live in :mod:`pipeline`.
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
