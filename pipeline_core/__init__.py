"""Framework-neutral read model shared by the legacy CLI and web app.

Legacy pipeline functions live in :mod:`pipeline`.
"""

from .read_model import OpportunityFilters, OpportunityRepository
from .visibility import CAPTURE_SOURCE_KEY, capture_visible_sql

__all__ = ["CAPTURE_SOURCE_KEY", "OpportunityFilters", "OpportunityRepository", "capture_visible_sql"]
